from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskSignalDecision:
    explicit_current_self_harm: bool
    indirect_current_danger: bool
    quoted_or_analytical_context: bool
    negated_current_intent: bool
    confidence: float
    reason_codes: tuple[str, ...]


_ANALYTICAL_CONTEXT_TERMS = (
    "论文",
    "文献",
    "学术",
    "研究",
    "政策",
    "新闻",
    "报道",
    "电影",
    "影视",
    "角色",
    "台词",
    "剧本",
    "翻译",
    "概括",
    "摘要",
    "引用",
    "第三人称",
)
_NEGATED_INTENT = re.compile(
    r"(?:没有|没|并无|从未|不曾|否认)"
    r"[^，,。！？!?；;\n]{0,12}"
    r"(?:自杀|自残|伤害自己|轻生|结束生命|不想活)"
    r"[^，,。！？!?；;\n]{0,8}"
    r"(?:想法|念头|打算|计划|意图)?",
    re.IGNORECASE,
)
_EXPLICIT_CURRENT_PATTERNS = (
    re.compile(r"(?:我|本人)(?:真的|现在|已经|最近|目前)?[^，,。！？!?；;\n]{0,8}(?:不想活|想死)", re.IGNORECASE),
    re.compile(
        r"(?:我|本人)?(?:现在|已经|准备好|准备|打算|决定|马上|真的)?"
        r"(?:想|要|要去|正在|准备要)"
        r"(?:结束生命|自杀|自残|伤害自己|轻生)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:kill myself|end my life|hurt myself|commit suicide)", re.IGNORECASE),
    re.compile(r"(?:我|本人)?(?:已经|现在)?[^，,。！？!?；;\n]{0,8}(?:准备好伤害自己|有明确的?自残计划|实施自残)", re.IGNORECASE),
    re.compile(r"(?:我|本人)?[^，,。！？!?；;\n]{0,12}(?:想从楼上跳下去|准备跳下去)", re.IGNORECASE),
)
_INDIRECT_CURRENT_PATTERNS = (
    re.compile(r"(?:我|本人)?[^\n]{0,12}(?:撑不下去|熬不下去)[^\n]{0,16}(?:彻底消失|消失|离开)", re.IGNORECASE),
    re.compile(r"(?:我|本人)[^，,。！？!?；;\n]{0,10}(?:活着没意思|不想醒来|没有活下去的理由)", re.IGNORECASE),
    re.compile(r"(?:我|本人)[^，,。！？!?；;\n]{0,10}(?:告别所有人|安排后事)", re.IGNORECASE),
    re.compile(r"(?:我|本人)?[^，,。！？!?；;\n]{0,12}(?:东西都交代好了|准备永远告别)", re.IGNORECASE),
    re.compile(r"(?:我|本人)?(?:已经|刚|正在)[^，,。！？!?；;\n]{0,12}(?:吞了很多药|割伤自己|流血很多)", re.IGNORECASE),
    re.compile(r"(?:我|本人)?(?:正在|已经到了?|现在在)[^，,。！？!?；;\n]{0,10}(?:天台边上|江边)[^，,。！？!?；;\n]{0,12}(?:跳下去|不想回去|很危险)", re.IGNORECASE),
    re.compile(r"(?:我|本人)?(?:马上|现在|准备)[^，,。！？!?；;\n]{0,10}(?:伤害别人|伤害那个人|攻击别人)", re.IGNORECASE),
    re.compile(r"(?:我|本人)?(?:已经|现在)?[^，,。！？!?；;\n]{0,8}准备[^，,。！？!?；;\n]{0,8}(?:今晚)?结束生命", re.IGNORECASE),
)
_CONTRAST_SEPARATOR = re.compile(r"(?:但(?:是)?|不过|可是|然而)")


def analyze_risk_signal(text: str) -> RiskSignalDecision:
    normalized = (text or "").strip().lower()
    if not normalized:
        return RiskSignalDecision(False, False, False, False, 0.0, ())

    analytical_context = any(term in normalized for term in _ANALYTICAL_CONTEXT_TERMS)
    negated_intent = bool(_NEGATED_INTENT.search(normalized))
    clauses = [
        clause.strip()
        for clause in re.split(r"[，,。！？!?；;\n]+", normalized)
        if clause.strip()
    ]

    explicit_current = False
    indirect_current = False
    for clause in clauses:
        if _NEGATED_INTENT.search(clause):
            continue
        if any(pattern.search(clause) for pattern in _EXPLICIT_CURRENT_PATTERNS):
            explicit_current = True
            break
        if any(pattern.search(clause) for pattern in _INDIRECT_CURRENT_PATTERNS):
            indirect_current = True
    if not explicit_current and any(
        pattern.search(normalized) for pattern in _INDIRECT_CURRENT_PATTERNS
    ):
        indirect_current = True

    # 引用或分析任务中的第一人称台词不能被当作用户本人表达。
    if analytical_context and _looks_like_quoted_task(normalized):
        explicit_current = False
        indirect_current = False

    reasons: list[str] = []
    if explicit_current:
        reasons.append("EXPLICIT_CURRENT_SELF_HARM")
    if indirect_current:
        reasons.append("INDIRECT_CURRENT_DANGER")
    if analytical_context:
        reasons.append("QUOTED_OR_ANALYTICAL_CONTEXT")
    if negated_intent:
        reasons.append("NEGATED_CURRENT_INTENT")

    confidence = 0.0
    if explicit_current:
        confidence = 0.99
    elif indirect_current:
        confidence = 0.9
    elif analytical_context or negated_intent:
        confidence = 0.9

    return RiskSignalDecision(
        explicit_current_self_harm=explicit_current,
        indirect_current_danger=indirect_current,
        quoted_or_analytical_context=analytical_context,
        negated_current_intent=negated_intent,
        confidence=confidence,
        reason_codes=tuple(reasons),
    )


def _looks_like_quoted_task(text: str) -> bool:
    task_terms = ("翻译", "概括", "摘要", "引用", "分析", "论文", "文献", "政策", "电影", "角色", "台词", "新闻", "报道")
    current_self_markers = (
        "但我现在",
        "但我真的",
        "我自己现在",
        "我本人现在",
        "对我来说现在",
    )
    if not any(term in text for term in task_terms):
        return False
    if any(marker in text for marker in current_self_markers):
        return False

    # 分析或引用任务之后出现转折时，只判断转折后的当前分句，避免前文语境
    # 覆盖“但我想自杀”这类明确的本人危险表达。
    for segment in _CONTRAST_SEPARATOR.split(text)[1:]:
        clause = re.split(r"[，,。！？!?；;\n]+", segment, maxsplit=1)[0].strip()
        if not clause or _NEGATED_INTENT.search(clause):
            continue
        if any(pattern.search(clause) for pattern in _EXPLICIT_CURRENT_PATTERNS):
            return False
        if any(pattern.search(clause) for pattern in _INDIRECT_CURRENT_PATTERNS):
            return False

    return True
