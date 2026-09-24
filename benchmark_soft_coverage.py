from __future__ import annotations

import collections
import hashlib
import json
import math
import re
import sys
import warnings
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
from lightgbm import LGBMRanker
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import Normalizer

# Portable paths: the benchmark can now be rerun directly from this modified project.
PROJ = Path(__file__).resolve().parent
BENCH_INPUT = PROJ / 'benchmarks' / 'evidence_facets_multiquery_20260913'
DATASET = BENCH_INPUT / 'realistic_student_queries_200.jsonl'
CHILDREN = PROJ / 'app' / 'knowledge' / 'handbook_v2' / 'children_v2.jsonl'
DOCS = BENCH_INPUT / 'documents.json'
OUT = PROJ / 'benchmark_soft_coverage_output'
OUT.mkdir(parents=True, exist_ok=True)
warnings.filterwarnings('ignore', message='X does not have valid feature names')

sys.path.insert(0, str(PROJ))
from app.services.knowledge_query import build_query_spec
from app.services.knowledge_scoring import KnowledgeTokenizer, reciprocal_rank_fusion

rows = [json.loads(line) for line in DATASET.read_text(encoding='utf-8').splitlines() if line.strip()]
children = [json.loads(line) for line in CHILDREN.read_text(encoding='utf-8').splitlines() if line.strip()]
documents = json.loads(DOCS.read_text(encoding='utf-8'))
doc_title = {item['document_id']: item['title'] for item in documents}
N = len(children)

texts, titles, sections, tags = [], [], [], []
for child in children:
    title = doc_title.get(child['document_id'], child['text'].splitlines()[0][:120])
    section = ' '.join(child.get('heading_path') or [])
    topic = child.get('topic') or ''
    article = ' '.join(child.get('article_labels') or [])
    tag = ' '.join(x for x in (topic, article, child.get('kind') or '') if x)
    texts.append(child['text'])
    titles.append(title)
    sections.append(section)
    tags.append(tag)

# ---------------- project-style BM25F ----------------
tok = KnowledgeTokenizer()
field_weights = {'tags': 3.4, 'section': 3.0, 'title': 2.4, 'content': 1.0}
corpus_field_lens, corpus_df, corpus_avg, corpus_postings = {}, {}, {}, {}
for field_name, values in {'tags': tags, 'section': sections, 'title': titles, 'content': texts}.items():
    lens, df, postings = [], collections.Counter(), collections.defaultdict(list)
    for index, value in enumerate(values):
        counts = collections.Counter(tok.tokenize(value))
        lens.append(sum(counts.values()))
        df.update(counts.keys())
        for term, freq in counts.items():
            postings[term].append((index, freq))
    corpus_field_lens[field_name] = np.asarray(lens, dtype=float)
    corpus_df[field_name] = df
    corpus_avg[field_name] = float(np.mean(lens)) or 1.0
    corpus_postings[field_name] = postings


@lru_cache(maxsize=8192)
def bm25_scores(query: str):
    query_counts = collections.Counter(tok.tokenize(query))
    scores = np.zeros(N, dtype=np.float32)
    if not query_counts:
        return scores
    for field_name, weight in field_weights.items():
        lens = corpus_field_lens[field_name]
        df = corpus_df[field_name]
        avg = corpus_avg[field_name]
        postings = corpus_postings[field_name]
        for term, query_freq in query_counts.items():
            posting = postings.get(term)
            if not posting:
                continue
            dft = df[term]
            idf = math.log(1.0 + (N - dft + 0.5) / (dft + 0.5))
            mult = idf * (1.0 + math.log(query_freq)) * 2.5 * weight
            for index, freq in posting:
                denom = freq + 1.5 * (1.0 - 0.75 + 0.75 * lens[index] / avg)
                scores[index] += mult * freq / denom
    return scores


# ---------------- offline dense proxy ----------------
corpus_dense = [f'{titles[i]} {sections[i]} {tags[i]} {texts[i]}' for i in range(N)]
vectorizer = TfidfVectorizer(
    analyzer='char', ngram_range=(2, 4), min_df=2, max_df=0.985,
    sublinear_tf=True, max_features=80000,
)
X = vectorizer.fit_transform(corpus_dense)
svd_dim = min(128, max(32, X.shape[1] - 1), N - 1)
svd = TruncatedSVD(n_components=svd_dim, random_state=42, n_iter=7)
normalizer = Normalizer(copy=False)
Xd = normalizer.fit_transform(svd.fit_transform(X)).astype(np.float32)


@lru_cache(maxsize=8192)
def dense_scores(query: str):
    q = vectorizer.transform([query])
    qd = normalizer.transform(svd.transform(q)).astype(np.float32)[0]
    return Xd @ qd


def rank_from_scores(scores, top=80):
    return [int(i) for i in np.argsort(-scores)[:top]]


def rrf_rank(rankings, weights=None, top=80):
    weights = weights or [1.0] * len(rankings)
    scores = reciprocal_rank_fusion([(ranking, weight) for ranking, weight in zip(rankings, weights)], k=60)
    ordered = sorted(scores, key=lambda item: (-scores[item], item))[:top]
    return ordered, scores


# ---------------- query-only upstream evidence-facet planner ----------------
# This planner deliberately uses ONLY the user's question text and a project-background
# concept lexicon.  It never reads expected_document_ids, chunks, titles, or gold evidence.
FACET_RULES = [
    (("国家奖学金",), "国家奖学金资格、兼得与评审规则"),
    (("励志奖学金", "国家励志"), "国家励志奖学金资格与兼得规则"),
    (("国家助学金", "助学金"), "国家助学金资格、发放与兼得规则"),
    (("社会助学金",), "社会助学金申请与兼得规则"),
    (("困难生认定", "困难认定", "家庭经济困难"), "家庭经济困难学生认定规则"),
    (("临时困难补助", "临时补助"), "临时困难补助申请规则"),
    (("重大疾病", "疾病救助"), "重大疾病救助申请与资助规则"),
    (("助学贷款", "贷款"), "助学贷款申请、还款或代偿规则"),
    (("基层", "学费补偿", "贷款代偿"), "基层就业学费补偿与助学贷款代偿规则"),
    (("参军", "入伍", "服兵役", "退役"), "应征入伍、退役复学与学生资助规则"),
    (("休学",), "休学条件、学籍状态与相关待遇规则"),
    (("复学", "恢复学籍"), "复学与恢复学籍规则"),
    (("转学", "转入", "转出"), "转学与学籍衔接规则"),
    (("毕业", "毕业证", "结业"), "毕业、结业与毕业资格规则"),
    (("学分", "课程认定", "学分认定"), "课程学分与成绩认定规则"),
    (("选课",), "选课与课程修读规则"),
    (("重修",), "课程重修规则"),
    (("补考",), "补考资格、成绩与办理规则"),
    (("缓考",), "缓考申请、条件与成绩处理规则"),
    (("缺考", "旷考"), "缺考、旷考与课程考核处理规则"),
    (("考试", "作弊", "手机", "考场", "监考"), "课程考核纪律与考试违规认定规则"),
    (("点名", "答到", "旷课", "迟到", "早退", "课堂"), "课堂出勤与课堂学习规范"),
    (("处分", "违纪", "纪律"), "学生纪律处分、影响与处理规则"),
    (("申诉", "不认可", "事实认定不对"), "学生处分申诉与复核流程"),
    (("宿舍", "寝室", "楼管", "违规电器"), "学生宿舍住宿、安全与用电规则"),
    (("电动车", "电池", "电瓶"), "校园电动自行车通行、充电与记分规则"),
    (("机动车",), "校园机动车通行管理规则"),
    (("校园网", "网络"), "校园网络使用与信息发布规则"),
    (("图书馆", "借书", "图书证"), "图书馆借阅、归还与读者规则"),
    (("档案",), "学生人事档案转递与管理规则"),
    (("就业", "签约", "就业去向", "实习"), "学生就业流程、签约与去向登记规则"),
    (("医保", "门诊", "住院", "转诊", "报销"), "大学生医保参保、门诊住院与报销规则"),
    (("体测", "体质", "免测"), "学生体质健康测试、免测与毕业相关规则"),
    (("体育免修",), "体育课程免修与课程修读规则"),
    (("社团",), "学生社团活动审批与管理规则"),
    (("无人机", "飞行器"), "校园飞行器使用、审批与安全规则"),
    (("科技活动",), "大学生科技活动认定与奖励规则"),
    (("社会实践",), "大学生社会实践活动与认定规则"),
    (("志愿", "志愿服务"), "志愿服务时长认定与相关规则"),
    (("美育",), "美育实践活动学时认定规则"),
    (("安全隐患", "安全要求", "事故"), "校园学生安全管理与报告规则"),
    (("学费", "缴费", "缓交"), "学生学费收缴、缓交与相关规则"),
]


def _nearby(query: str, terms: tuple[str, ...], width: int = 28) -> str:
    positions = [query.find(term) for term in terms if query.find(term) >= 0]
    if not positions:
        return query[:56]
    pos = min(positions)
    return query[max(0, pos - width): min(len(query), pos + width)]


def evidence_facets_from_query(query: str) -> list[str]:
    facets = []
    for terms, label in FACET_RULES:
        if any(term in query for term in terms):
            facet = f'{label}；语境：{_nearby(query, terms)}'
            if facet not in facets:
                facets.append(facet)
        if len(facets) >= 3:
            break
    if not facets:
        # Generic fallback: the WorkItem still has one evidence goal, but does not invent policy facts.
        facets.append(f'查证当前学生问题对应的校方规则与办理依据；语境：{query[:80]}')
    return facets[:3]


# ---------------- rewrite variants ----------------
FACET_CANON = {
    'ELIGIBILITY': '申请条件', 'MATERIALS': '申请材料', 'STEPS': '办理流程',
    'CHANNEL': '办理入口', 'DEADLINE': '申请截止时间', 'PROCESSING_TIME': '办理时长',
    'CONTACT': '联系方式', 'COST': '费用', 'SCOPE': '适用范围', 'POLICY_BASIS': '政策依据',
}


def safe_rewrite(query: str) -> str:
    """Project-taxonomy rewrite proxy: preserve query and append controlled concepts/facets."""
    try:
        spec = build_query_spec(query)
    except Exception:
        return query
    additions = []
    if getattr(spec, 'concepts', None):
        additions.extend(item.canonical for item in spec.concepts)
    additions.extend(FACET_CANON.get(item.value, item.value) for item in spec.required_facets)
    suffix = ' '.join(dict.fromkeys(x for x in additions if x))
    return f'{query} {suffix}'.strip() if suffix else query


def single_rewrite_variants(query: str):
    rewritten = safe_rewrite(query)
    if rewritten == query:
        return [(query, 1.0)]
    return [(query, 1.0), (rewritten, 0.95)]


def facet_queries(query: str, *, rewrite: bool):
    facets = evidence_facets_from_query(query)
    values = [(query, 1.0, 'ORIGINAL')]
    for i, facet in enumerate(facets, 1):
        q = safe_rewrite(facet) if rewrite else facet
        values.append((q, 0.85, f'FACET_{i}'))
    # normalized de-dup
    out, seen = [], set()
    for text, weight, kind in values:
        key = re.sub(r'\W+', '', text).lower()
        if key and key not in seen:
            seen.add(key)
            out.append((text, weight, kind))
    return facets, out


def multi_retrieve(query: str, *, use_facets=False, rewrite=False, channel='hybrid'):
    if use_facets:
        facets, variants = facet_queries(query, rewrite=rewrite)
    elif rewrite:
        facets = []
        variants = [(q, w, 'REWRITE') for q, w in single_rewrite_variants(query)]
    else:
        facets = []
        variants = [(query, 1.0, 'ORIGINAL')]
    rankings, weights = [], []
    for q, qweight, _kind in variants:
        if channel in ('bm25', 'hybrid'):
            rankings.append(rank_from_scores(bm25_scores(q), 80)); weights.append(qweight)
        if channel in ('dense', 'hybrid'):
            rankings.append(rank_from_scores(dense_scores(q), 80)); weights.append(qweight)
    rank, score = rrf_rank(rankings, weights, top=80)
    return rank, score, facets, variants


# ---------------- reranker proxy ----------------
def char_bigrams(text):
    text = re.sub(r'\s+', '', text.lower())
    return set(text[i:i+2] for i in range(max(0, len(text)-1)))


def overlap(left, right):
    a, b = char_bigrams(left), char_bigrams(right)
    return len(a & b) / (len(a) + 1e-9)


def numbers(text):
    return set(re.findall(r'\d+(?:\.\d+)?', text))


def rank_map(values):
    return {value: index + 1 for index, value in enumerate(values)}


cache = {}
for qi, row in enumerate(rows):
    q = row['query']
    b = bm25_scores(q); d = dense_scores(q)
    br = rank_from_scores(b); dr = rank_from_scores(d)
    h0, h0s, _, _ = multi_retrieve(q, use_facets=False, rewrite=False, channel='hybrid')
    sb, sbs, _, _ = multi_retrieve(q, use_facets=False, rewrite=True, channel='bm25')
    sd, sds, _, _ = multi_retrieve(q, use_facets=False, rewrite=True, channel='dense')
    sh, shs, _, _ = multi_retrieve(q, use_facets=False, rewrite=True, channel='hybrid')
    fnb, fnbs, facets, raw_variants = multi_retrieve(q, use_facets=True, rewrite=False, channel='bm25')
    fnh, fnhs, _, _ = multi_retrieve(q, use_facets=True, rewrite=False, channel='hybrid')
    frb, frbs, _, rewritten_variants = multi_retrieve(q, use_facets=True, rewrite=True, channel='bm25')
    frd, frds, _, _ = multi_retrieve(q, use_facets=True, rewrite=True, channel='dense')
    frh, frhs, _, _ = multi_retrieve(q, use_facets=True, rewrite=True, channel='hybrid')
    cache[qi] = {
        'b': b, 'd': d, 'br': br, 'dr': dr,
        'h0': h0, 'h0s': h0s,
        'sb': sb, 'sd': sd, 'sh': sh, 'shs': shs,
        'fnb': fnb, 'fnh': fnh, 'fnhs': fnhs,
        'frb': frb, 'frd': frd, 'frh': frh, 'frhs': frhs,
        'facets': facets,
        'raw_variants': raw_variants,
        'rewritten_variants': rewritten_variants,
    }
    if (qi + 1) % 25 == 0:
        print('precomputed', qi + 1, flush=True)


@lru_cache(maxsize=65536)
def feat(qi, idx, mode='original'):
    row, child, ca = rows[qi], children[idx], cache[qi]
    q = row['query']
    if mode == 'facet':
        fused, fused_scores = ca['frh'], ca['frhs']
    elif mode == 'single':
        fused, fused_scores = ca['sh'], ca['shs']
    else:
        fused, fused_scores = ca['h0'], ca['h0s']
    brm, drm, frm = rank_map(ca['br']), rank_map(ca['dr']), rank_map(fused)
    title = doc_title.get(child['document_id'], '')
    section = ' '.join(child.get('heading_path') or [])
    tag = (child.get('topic') or '') + ' ' + ' '.join(child.get('article_labels') or [])
    qnums, cnums = numbers(q), numbers(child['text'])
    facet_scores = [max(overlap(f, child['text']), overlap(f, title), overlap(f, section)) for f in ca['facets']]
    return [
        math.log1p(max(float(ca['b'][idx]), 0)), float(ca['d'][idx]), float(fused_scores.get(idx, 0)),
        1/(60+brm.get(idx,999)), 1/(60+drm.get(idx,999)), 1/(60+frm.get(idx,999)),
        overlap(q, child['text']), overlap(q, title), overlap(q, section), overlap(q, tag),
        max(facet_scores, default=0.0), sum(1 for x in facet_scores if x >= 0.08),
        1.0 if qnums and qnums.issubset(cnums) else 0.0,
        1.0 if qnums and qnums.intersection(cnums) else 0.0,
        math.log1p(child.get('token_count') or 0),
        1.0 if child.get('kind') == 'ARTICLE' else 0.0,
        1.0 if child.get('topic') else 0.0,
    ]


train_q = [i for i in range(len(rows)) if i % 4 == 0]
test_q = [i for i in range(len(rows)) if i % 4 != 0]

# Relevance-only features intentionally exclude the two facet-match dimensions
# (max facet overlap and number of matched facets).  Facet-aware uses all features.
RELEVANCE_FEATURE_INDEXES = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16]


def feature_vector(qi, idx, mode, facet_aware: bool):
    values = feat(qi, idx, mode)
    if facet_aware:
        return values
    return [values[i] for i in RELEVANCE_FEATURE_INDEXES]


def train_ranker(candidate_key, mode, seed, *, facet_aware: bool):
    Xtr, ytr, groups = [], [], []
    for qi in train_q:
        cand = cache[qi][candidate_key][:60]
        gold = set(rows[qi]['expected_document_ids'])
        groups.append(len(cand))
        for idx in cand:
            Xtr.append(feature_vector(qi, idx, mode, facet_aware))
            ytr.append(1 if children[idx]['document_id'] in gold else 0)
    ranker = LGBMRanker(
        objective='lambdarank', metric='ndcg', n_estimators=220, learning_rate=0.035,
        num_leaves=15, max_depth=5, min_child_samples=20, reg_lambda=1.2,
        random_state=seed, verbosity=-1,
    )
    ranker.fit(np.asarray(Xtr), np.asarray(ytr), group=groups)
    return ranker


ranker_original_rel = train_ranker('h0', 'original', 42, facet_aware=False)
ranker_single_rel = train_ranker('sh', 'single', 43, facet_aware=False)
ranker_facet_no_rewrite_rel = train_ranker('fnh', 'facet', 44, facet_aware=False)
ranker_facet_no_rewrite_aware = train_ranker('fnh', 'facet', 45, facet_aware=True)
ranker_facet_rewrite_rel = train_ranker('frh', 'facet', 46, facet_aware=False)
ranker_facet_rewrite_aware = train_ranker('frh', 'facet', 47, facet_aware=True)


def rerank_list(qi, candidate_key, ranker, mode, *, facet_aware: bool):
    cand = cache[qi][candidate_key][:60]
    if not cand:
        return []
    pred = ranker.predict(np.asarray([feature_vector(qi, idx, mode, facet_aware) for idx in cand]))
    base = {idx: float(score) for idx, score in zip(cand, pred)}
    return sorted(cand, key=lambda idx: (-base[idx], idx))


def candidate_facet_scores(qi, idx):
    child = children[idx]
    title = doc_title.get(child['document_id'], '')
    section = ' '.join(child.get('heading_path') or [])
    return [
        max(overlap(facet, child['text']), overlap(facet, title), overlap(facet, section))
        for facet in cache[qi]['facets']
    ]


def soft_coverage_select(qi, ranking, threshold, top_k=5, candidate_pool_limit=24):
    """Offline proxy of production soft coverage selection.

    No gold labels are used. Candidate->facet support is approximated by facet/text
    overlap, and only candidates already inside the first 24 reranked candidates are
    eligible, mirroring the production fused-candidate limit.
    """
    facets = cache[qi]['facets']
    base = list(dict.fromkeys(ranking))
    initial = base[:top_k]
    if not facets or top_k <= 0:
        return base, {'status': 'NOT_APPLICABLE', 'coverage_ratio': None, 'selection_applied': False}

    pool = base[:candidate_pool_limit]
    match_map = {}
    for idx in pool:
        scores = candidate_facet_scores(qi, idx)
        match_map[idx] = {fi for fi, score in enumerate(scores) if score >= threshold}

    reserved = []
    if len(facets) == 1:
        if not any(0 in match_map.get(idx, set()) for idx in initial):
            eligible = [idx for idx in pool if 0 in match_map.get(idx, set())]
            if eligible:
                reserved.append(eligible[0])
    else:
        uncovered = set(range(len(facets)))
        while uncovered and len(reserved) < top_k:
            eligible = [
                idx for idx in pool
                if idx not in reserved and match_map.get(idx, set()).intersection(uncovered)
            ]
            if not eligible:
                break
            rank_pos = {idx: pos for pos, idx in enumerate(base)}
            best = max(
                eligible,
                key=lambda idx: (
                    len(match_map[idx].intersection(uncovered)),
                    -rank_pos[idx],
                ),
            )
            reserved.append(best)
            uncovered.difference_update(match_map[best])

    selected_set = set(reserved)
    for idx in base:
        if len(selected_set) >= top_k:
            break
        selected_set.add(idx)
    rank_pos = {idx: pos for pos, idx in enumerate(base)}
    selected = sorted(selected_set, key=lambda idx: rank_pos[idx])[:top_k]
    selected_set = set(selected)
    covered = set()
    for idx in selected:
        covered.update(match_map.get(idx, set()))
    ratio = len(covered) / len(facets) if facets else None
    status = 'FULL' if ratio == 1.0 else ('PARTIAL' if covered else 'NONE')
    remainder = [idx for idx in base if idx not in selected_set]
    full_ranking = selected + remainder
    return full_ranking, {
        'status': status,
        'coverage_ratio': ratio,
        'selection_applied': selected != initial,
        'covered_count': len(covered),
        'facet_count': len(facets),
    }


for qi in range(len(rows)):
    cache[qi]['h0_rel_rerank'] = rerank_list(qi, 'h0', ranker_original_rel, 'original', facet_aware=False)
    cache[qi]['sh_rel_rerank'] = rerank_list(qi, 'sh', ranker_single_rel, 'single', facet_aware=False)
    cache[qi]['fnh_rel_rerank'] = rerank_list(qi, 'fnh', ranker_facet_no_rewrite_rel, 'facet', facet_aware=False)
    cache[qi]['fnh_facetaware_rerank'] = rerank_list(qi, 'fnh', ranker_facet_no_rewrite_aware, 'facet', facet_aware=True)
    cache[qi]['frh_rel_rerank'] = rerank_list(qi, 'frh', ranker_facet_rewrite_rel, 'facet', facet_aware=False)
    cache[qi]['frh_facetaware_rerank'] = rerank_list(qi, 'frh', ranker_facet_rewrite_aware, 'facet', facet_aware=True)


# Tune only the candidate->facet overlap threshold on the fixed 50-query dev split.
# Held-out 150 remains untouched.
def _dev_threshold_score(threshold):
    vals = []
    for qi in train_q:
        if len(rows[qi]['expected_document_ids']) <= 1:
            continue
        ranking, _ = soft_coverage_select(qi, cache[qi]['fnh_facetaware_rerank'], threshold)
        rec, hit, rr, _ = metric_for(ranking, rows[qi]['expected_document_ids']) if 'metric_for' in globals() else (None, None, None, None)
        if rec is not None:
            vals.append((rec, hit, rr))
    return vals

# metric_for is defined later, so threshold selection is performed after that definition.
COVERAGE_THRESHOLD_CANDIDATES = [0.06, 0.08, 0.10, 0.12, 0.15]


methods = {
    'BM25_no_rewrite': 'br',
    'Dense_LSA_no_rewrite': 'dr',
    'Hybrid_RRF_no_rewrite': 'h0',
    'Hybrid_RRF_RelevanceRerank_no_rewrite': 'h0_rel_rerank',
    'SingleRewrite_BM25': 'sb',
    'SingleRewrite_Dense_LSA': 'sd',
    'SingleRewrite_Hybrid_RRF': 'sh',
    'SingleRewrite_Hybrid_RRF_RelevanceRerank': 'sh_rel_rerank',
    'FacetMultiQuery_BM25_no_rewrite': 'fnb',
    'FacetMultiQuery_Hybrid_RRF_no_rewrite': 'fnh',
    'FacetMultiQuery_Hybrid_RRF_RelevanceRerank': 'fnh_rel_rerank',
    'FacetMultiQuery_Hybrid_RRF_FacetAwareRerank': 'fnh_facetaware_rerank',
    'FacetRewrite_BM25': 'frb',
    'FacetRewrite_Dense_LSA': 'frd',
    'FacetRewrite_Hybrid_RRF': 'frh',
    'FacetRewrite_Hybrid_RRF_RelevanceRerank': 'frh_rel_rerank',
    'FacetRewrite_Hybrid_RRF_FacetAwareRerank': 'frh_facetaware_rerank',
}


def metric_for(ranking, gold_docs, k=5):
    got_docs = [children[i]['document_id'] for i in ranking[:k]]
    gold = set(gold_docs)
    hitset = gold.intersection(got_docs)
    recall = len(hitset) / len(gold) if gold else 1.0
    hit = 1.0 if hitset else 0.0
    rr = 0.0
    for rank, doc in enumerate(got_docs, 1):
        if doc in gold:
            rr = 1.0 / rank
            break
    return recall, hit, rr, got_docs


def choose_coverage_threshold():
    best = None
    audit = []
    for threshold in COVERAGE_THRESHOLD_CANDIDATES:
        vals = []
        for qi in train_q:
            if len(rows[qi]['expected_document_ids']) <= 1:
                continue
            ranking, _ = soft_coverage_select(qi, cache[qi]['fnh_facetaware_rerank'], threshold)
            rec, hit, rr, _ = metric_for(ranking, rows[qi]['expected_document_ids'])
            vals.append((rec, hit, rr))
        arr = np.asarray(vals) if vals else np.zeros((1, 3))
        score = (float(arr[:,0].mean()), float(arr[:,2].mean()), float(arr[:,1].mean()))
        audit.append({'threshold': threshold, 'dev_multi_recall@5': score[0], 'dev_multi_mrr@5': score[1], 'dev_multi_hitrate@5': score[2]})
        if best is None or score > best[0]:
            best = (score, threshold)
    return best[1], audit


COVERAGE_MATCH_THRESHOLD, threshold_audit = choose_coverage_threshold()
for qi in range(len(rows)):
    cache[qi]['fnh_soft_coverage'], cache[qi]['fnh_coverage_info'] = soft_coverage_select(
        qi, cache[qi]['fnh_facetaware_rerank'], COVERAGE_MATCH_THRESHOLD
    )
    cache[qi]['frh_soft_coverage'], cache[qi]['frh_coverage_info'] = soft_coverage_select(
        qi, cache[qi]['frh_facetaware_rerank'], COVERAGE_MATCH_THRESHOLD
    )

methods.update({
    'FacetMultiQuery_Hybrid_RRF_FacetAwareRerank_SoftCoverage': 'fnh_soft_coverage',
    'FacetRewrite_Hybrid_RRF_FacetAwareRerank_SoftCoverage': 'frh_soft_coverage',
})


def evaluate(indices, label):
    metric_rows, per_rows = [], []
    for method, key in methods.items():
        values = []
        for qi in indices:
            row = rows[qi]
            rec, hit, rr, got = metric_for(cache[qi][key], row['expected_document_ids'])
            values.append((rec, hit, rr))
            per_rows.append({
                'split': label, 'query_id': row['id'], 'method': method,
                'recall@5': rec, 'hitrate@5': hit, 'mrr@5': rr,
                'query': row['query'], 'expected_document_ids': '|'.join(row['expected_document_ids']),
                'top5_document_ids': '|'.join(got),
                'top5_chunk_ids': '|'.join(children[i]['chunk_id'] for i in cache[qi][key][:5]),
                'facet_count': len(cache[qi]['facets']), 'facets': ' || '.join(cache[qi]['facets']),
                'policy_area': row['policy_area'], 'difficulty': row['difficulty'], 'query_style': row['query_style'],
            })
        array = np.asarray(values)
        metric_rows.append({
            'split': label, 'method': method, 'n_queries': len(indices),
            'recall@5': array[:,0].mean(), 'hitrate@5': array[:,1].mean(), 'mrr@5': array[:,2].mean(),
        })
    return metric_rows, per_rows


metrics_all, per_all = evaluate(list(range(len(rows))), 'all200')
metrics_test, per_test = evaluate(test_q, 'heldout150')
metrics = pd.DataFrame(metrics_all + metrics_test)
per_query = pd.DataFrame(per_all + per_test)
metrics.to_csv(OUT / 'metrics.csv', index=False)
per_query.to_csv(OUT / 'per_query.csv', index=False)

# Explicit single-vs-multi evidence slices on the untouched heldout set.
slices = []
heldout = per_query[per_query['split'] == 'heldout150']
query_meta = {row['id']: row for row in rows}
for method in methods:
    dm = heldout[heldout.method == method]
    for slice_name, predicate in (
        ('single_evidence', lambda r: len(r['expected_document_ids']) == 1),
        ('multi_evidence', lambda r: len(r['expected_document_ids']) > 1),
    ):
        ids = [qid for qid in dm.query_id if predicate(query_meta[qid])]
        group = dm[dm.query_id.isin(ids)]
        slices.append({
            'method': method, 'slice': slice_name, 'n': len(group),
            'recall@5': group['recall@5'].mean(), 'hitrate@5': group['hitrate@5'].mean(), 'mrr@5': group['mrr@5'].mean(),
        })
pd.DataFrame(slices).to_csv(OUT / 'evidence_slice_metrics.csv', index=False)

coverage_rows = []
for label, indices in [('all200', list(range(len(rows)))), ('heldout150', test_q)]:
    for method, info_key in [
        ('FacetMultiQuery_Hybrid_RRF_FacetAwareRerank_SoftCoverage', 'fnh_coverage_info'),
        ('FacetRewrite_Hybrid_RRF_FacetAwareRerank_SoftCoverage', 'frh_coverage_info'),
    ]:
        infos = [cache[qi][info_key] for qi in indices]
        ratios = [x['coverage_ratio'] for x in infos if x['coverage_ratio'] is not None]
        coverage_rows.append({
            'split': label,
            'method': method,
            'n_queries': len(indices),
            'avg_predicted_facet_coverage': float(np.mean(ratios)) if ratios else None,
            'full_coverage_rate': float(np.mean([x['status'] == 'FULL' for x in infos])),
            'selection_applied_rate': float(np.mean([x['selection_applied'] for x in infos])),
        })
pd.DataFrame(coverage_rows).to_csv(OUT / 'coverage_metrics.csv', index=False)
pd.DataFrame(threshold_audit).to_csv(OUT / 'coverage_threshold_dev_tuning.csv', index=False)

# Facet audit: generated only from query text; include expected docs later only for evaluation visibility.
with open(OUT / 'facet_plans.jsonl', 'w', encoding='utf-8') as handle:
    for qi, row in enumerate(rows):
        handle.write(json.dumps({
            'id': row['id'], 'query': row['query'], 'evidenceFacets': cache[qi]['facets'],
            'rawExecutedQueries': [x[0] for x in cache[qi]['raw_variants']],
            'rewrittenExecutedQueries': [x[0] for x in cache[qi]['rewritten_variants']],
        }, ensure_ascii=False) + '\n')

# Bootstrap CI heldout.
rng = np.random.default_rng(20260913)
ci = []
for method in methods:
    dm = heldout[heldout.method == method].reset_index(drop=True)
    n = len(dm); boots = []
    for _ in range(1500):
        ix = rng.integers(0, n, n); sample = dm.iloc[ix]
        boots.append([sample['recall@5'].mean(), sample['hitrate@5'].mean(), sample['mrr@5'].mean()])
    B = np.asarray(boots)
    for j, name in enumerate(['recall@5', 'hitrate@5', 'mrr@5']):
        ci.append({'method': method, 'metric': name, 'mean': float(dm[name].mean()),
                   'ci95_low': float(np.percentile(B[:,j], 2.5)), 'ci95_high': float(np.percentile(B[:,j], 97.5))})
pd.DataFrame(ci).to_csv(OUT / 'bootstrap_ci.csv', index=False)

# Detailed top-5 for strongest/new methods.
detail_methods = ['BM25_no_rewrite', 'Hybrid_RRF_RelevanceRerank_no_rewrite', 'FacetMultiQuery_Hybrid_RRF_RelevanceRerank', 'FacetMultiQuery_Hybrid_RRF_FacetAwareRerank', 'FacetRewrite_Hybrid_RRF_RelevanceRerank', 'FacetRewrite_Hybrid_RRF_FacetAwareRerank', 'FacetMultiQuery_Hybrid_RRF_FacetAwareRerank_SoftCoverage', 'FacetRewrite_Hybrid_RRF_FacetAwareRerank_SoftCoverage']
with open(OUT / 'heldout_detailed.jsonl', 'w', encoding='utf-8') as handle:
    for qi in test_q:
        row = rows[qi]; obj = {'query': row, 'evidenceFacets': cache[qi]['facets'], 'methods': {}}
        for method in detail_methods:
            ranking = cache[qi][methods[method]]
            rec, hit, rr, _ = metric_for(ranking, row['expected_document_ids'])
            obj['methods'][method] = {
                'recall@5': rec, 'hitrate@5': hit, 'mrr@5': rr,
                'top5': [{
                    'rank': rank + 1, 'chunk_id': children[idx]['chunk_id'], 'document_id': children[idx]['document_id'],
                    'document_title': doc_title.get(children[idx]['document_id'], ''),
                    'heading_path': children[idx].get('heading_path'), 'topic': children[idx].get('topic'),
                    'text': children[idx]['text'][:800],
                } for rank, idx in enumerate(ranking[:5])],
            }
        handle.write(json.dumps(obj, ensure_ascii=False) + '\n')

meta = {
    'dataset_sha256': hashlib.sha256(DATASET.read_bytes()).hexdigest(),
    'dataset_n': len(rows), 'children_n': N, 'heldout_n': len(test_q), 'reranker_train_n': len(train_q),
    'facet_planner': 'query-only deterministic project-background concept planner; DOES NOT read gold expected_document_ids or chunk text',
    'bm25': 'project KnowledgeTokenizer + BM25F field weights tags=3.4,section=3.0,title=2.4,content=1.0',
    'dense_proxy': f'char 2-4gram TFIDF -> TruncatedSVD({svd_dim}) -> L2 cosine; NOT BGE-M3',
    'single_rewrite': 'project retrieval_taxonomy safe expansion preserving original query',
    'facet_rewrite': 'per-facet safe retrieval_taxonomy expansion; original q0 retained as anchor; facet query weights=0.85',
    'fusion': 'RRF k=60',
    'reranker_proxy': 'LightGBM LambdaRank trained on fixed 50-query dev split. FacetAware adds max facet match and matched-facet-count features. This is a proxy for the production LLM reranker prompt/schema, not an LLM result.',
    'soft_coverage_selector': f'Adaptive soft coverage: 0 facet=no-op; 1 facet reserves at most one matched evidence if Top5 lacks support; 2-3 facets greedy set-cover within top24 reranked candidates, then fill by relevance and preserve rerank relative order. Candidate->facet proxy uses overlap threshold={COVERAGE_MATCH_THRESHOLD}, selected only on dev50; no heldout labels used.',
    'metrics': 'Recall@5 = unique expected logical-document coverage in top5 chunks; HitRate@5 = any gold document in top5; MRR@5 = reciprocal rank of first gold document',
}
(OUT / 'benchmark_meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')

print(metrics[metrics.split == 'heldout150'].to_string(index=False, float_format=lambda x: f'{x:.4f}'))
print('\n--- evidence slices ---')
print(pd.DataFrame(slices).to_string(index=False, float_format=lambda x: f'{x:.4f}'))
