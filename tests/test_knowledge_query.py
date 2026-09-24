import tempfile
import unittest
from pathlib import Path

from app.services.knowledge_query import (
    ConceptMode,
    KnowledgeFacet,
    KnowledgeTaxonomy,
    build_query_spec,
    get_knowledge_taxonomy,
)


class KnowledgeQueryContractTests(unittest.TestCase):
    def test_dorm_change_aliases_share_one_canonical_concept(self):
        concepts = {
            build_query_spec(text).concepts[0].concept_id
            for text in ("调宿怎么办", "换宿舍需要什么", "想换寝", "宿舍调整流程", "调寝申请")
        }

        self.assertEqual(concepts, {"DORM_CHANGE"})

    def test_generic_material_and_process_terms_are_facets_only(self):
        spec = build_query_spec("处分申诉需要哪些材料，怎么办理")

        self.assertEqual(spec.concept_mode, ConceptMode.CONTROLLED)
        self.assertEqual([item.concept_id for item in spec.concepts], ["STUDENT_APPEAL"])
        self.assertIn(KnowledgeFacet.MATERIALS, spec.required_facets)
        self.assertIn(KnowledgeFacet.STEPS, spec.required_facets)
        self.assertTrue(spec.requires_authoritative_local_info)
        self.assertLessEqual(len(spec.variants), 3)

    def test_site_is_only_set_when_explicit(self):
        unknown = build_query_spec("调宿怎么办")
        explicit = build_query_spec("南望山校区调宿怎么办")

        self.assertIsNone(unknown.site)
        self.assertEqual(explicit.site, "NANWANGSHAN")

    def test_unknown_concept_uses_open_contract_not_empty_controlled_match(self):
        spec = build_query_spec("校园打印机押金怎么办")

        self.assertEqual(spec.concept_mode, ConceptMode.OPEN)
        self.assertFalse(spec.concepts)
        self.assertTrue(spec.open_concept_text)

    def test_career_concepts_alone_do_not_require_authoritative_local_info(self):
        decision = build_query_spec("我在考研、就业和实习之间拿不定主意")
        deadline = build_query_spec("今年学校推免报名截止时间是什么时候")

        self.assertFalse(decision.requires_authoritative_local_info)
        self.assertTrue(deadline.requires_authoritative_local_info)
        self.assertTrue(deadline.freshness_required)

    def test_follow_up_can_reuse_context_concept_without_overwriting_original(self):
        taxonomy = get_knowledge_taxonomy()
        spec = taxonomy.build_query_spec(
            "那需要哪些材料",
            domain_hint="CAMPUS_SERVICE",
            context_question="我想申请调宿",
        )

        self.assertEqual(spec.original_question, "那需要哪些材料")
        self.assertIn("我想申请调宿", spec.normalized_question)
        self.assertEqual(spec.concepts[0].concept_id, "DORM_CHANGE")

    def test_deadline_and_processing_time_are_distinct_facets(self):
        deadline = build_query_spec("调宿申请截止到哪一天")
        processing = build_query_spec("调宿通常几个工作日办完")
        online = build_query_spec("调宿线上怎么办")
        window = build_query_spec("五月份集中办理调宿")

        self.assertIn(KnowledgeFacet.DEADLINE, deadline.required_facets)
        self.assertNotIn(KnowledgeFacet.PROCESSING_TIME, deadline.required_facets)
        self.assertIn(KnowledgeFacet.PROCESSING_TIME, processing.required_facets)
        self.assertNotIn(KnowledgeFacet.DEADLINE, processing.required_facets)
        self.assertIn(KnowledgeFacet.CHANNEL, online.required_facets)
        self.assertIn(KnowledgeFacet.STEPS, online.required_facets)
        self.assertNotIn(KnowledgeFacet.PROCESSING_TIME, online.required_facets)
        self.assertIn(KnowledgeFacet.DEADLINE, window.required_facets)
        self.assertNotIn(KnowledgeFacet.PROCESSING_TIME, window.required_facets)

    def test_processing_time_coverage_requires_time_semantics_not_an_arbitrary_number(self):
        taxonomy = get_knowledge_taxonomy()
        spec = build_query_spec("调宿通常几个工作日办完")

        self.assertNotIn(
            KnowledgeFacet.PROCESSING_TIME,
            taxonomy.covered_facets(spec, "宿舍共有 7 栋楼，可在线提交调宿申请。"),
        )
        self.assertIn(
            KnowledgeFacet.PROCESSING_TIME,
            taxonomy.covered_facets(spec, "申请通常在三个工作日内办结。"),
        )

    def test_invalid_alias_collision_fails_loading(self):
        content = """\
version: 1
sites:
  ALL: {aliases: [全校]}
facet_terms:
  ELIGIBILITY: {query_terms: [条件], evidence_patterns: [申请条件]}
  MATERIALS: {query_terms: [材料], evidence_patterns: [申请表]}
  STEPS: {query_terms: [流程], evidence_patterns: [办理]}
  CHANNEL: {query_terms: [入口], evidence_patterns: [服务中心]}
  DEADLINE: {query_terms: [期限], evidence_patterns: [截止]}
  PROCESSING_TIME: {query_terms: [几个工作日], evidence_patterns: [工作日内办结]}
  CONTACT: {query_terms: [电话], evidence_patterns: [联系电话]}
  COST: {query_terms: [费用], evidence_patterns: [收费]}
  SCOPE: {query_terms: [范围], evidence_patterns: [适用于]}
  POLICY_BASIS: {query_terms: [依据], evidence_patterns: [根据]}
concepts:
  ONE: {canonical: 事项一, domain: CAMPUS_SERVICE, aliases: [同义词]}
  TWO: {canonical: 事项二, domain: CAMPUS_SERVICE, aliases: [同义词]}
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "taxonomy.yaml"
            path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "alias 冲突"):
                KnowledgeTaxonomy(path)


if __name__ == "__main__":
    unittest.main()
