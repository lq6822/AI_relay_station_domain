"""判定测试：验证四标签、证据分档、人工复核及验证时间等业务含义。"""

import unittest
from datetime import datetime, timezone

from pydantic import TypeAdapter

from helpers import QUOTES, TIME, sample
from relay_intel.assessment import CORE, apply_review, assess
from relay_intel.contracts import (
    Citation, Concern, DomainResult, FactSuggestion, Investigation, Review, Score, now,
)
from relay_intel.delivery import to_intelligence
from relay_intel.investigation import merge_facts


def result_for(facts=(), **overrides):
    candidate, material, analysis = sample(facts=facts, **overrides)
    investigation = Investigation(analysis=analysis, tool_calls=[], requests=2, usage={})
    return DomainResult(candidate=candidate, materials=[material], investigation=investigation,
                        assessment=assess([material], analysis))


def review_for(result, **updates):
    values = dict(run_id="r", domain=result.candidate.domain, action="accept", reviewer="test reviewer",
                  reviewed_at=now(), reason="Checked evidence and accepted the conservative label",
                  citations=result.investigation.analysis.citations)
    values.update(updates)
    return Review(**values)


class AssessmentTests(unittest.TestCase):
    def test_four_labels(self):
        for facts, label in [(CORE, "确认"), (("relay_clue",), "疑似"), (("exclusion",), "排除"), ((), "证据不足")]:
            with self.subTest(label=label):
                result = result_for(facts)
                self.assertEqual((result.assessment.label, result.assessment.confidence), (label, .9))

    def test_actual_failure_is_insufficient_but_no_material_is_unfinished(self):
        result = result_for(access_status="failed", excerpt=None, failure_reason="timeout", evidence_state="unknown")
        self.assertEqual((result.assessment.label, result.assessment.review_status), ("证据不足", "not_required"))
        with self.assertRaisesRegex(ValueError, "no investigation"):
            assess([], result.investigation.analysis)

    def test_unknown_and_conflict_are_distinct(self):
        result = result_for(CORE)
        analysis = result.investigation.analysis
        self.assertEqual(merge_facts(result.materials, analysis)["exclusion"].value, "unknown")
        analysis.facts = [FactSuggestion(fact="third_party", value="refuted", material_id="m",
                                         quote=QUOTES["third_party"])]
        actual = assess(result.materials, analysis)
        self.assertEqual((actual.label, actual.confidence, actual.review_status), ("疑似", .55, "pending"))
        self.assertEqual({e.origin for e in merge_facts(result.materials, analysis)["third_party"].evidence},
                         {"human", "model"})

    def test_historical_secondary_and_uncertain_evidence(self):
        for kwargs, label, score in [
            ({"evidence_state": "historical"}, "疑似", .75),
            ({"source_kind": "secondary"}, "确认", .75),
            ({"subject_relation": "uncertain"}, "疑似", .55),
        ]:
            with self.subTest(kwargs=kwargs):
                result = result_for(CORE, **kwargs)
                self.assertEqual((result.assessment.label, result.assessment.confidence), (label, score))
        conflict = result_for((*CORE, "exclusion"))
        self.assertEqual((conflict.assessment.label, conflict.assessment.confidence), ("疑似", .55))

    def test_accept_conservative_result_preserves_unknowns(self):
        result = result_for(("relay_clue",), needs_review=True)
        updated = apply_review(result, review_for(result))
        self.assertEqual((updated.label, updated.review_status), ("疑似", "completed"))
        self.assertEqual(merge_facts(result.materials, result.investigation.analysis)["third_party"].value, "unknown")

    def test_reviewer_can_resolve_model_concern_and_confirm(self):
        result = result_for(CORE)
        analysis = result.investigation.analysis
        analysis.concerns = [Concern(kind="business", reason="Relay or ordinary app unclear", citations=analysis.citations)]
        result.assessment = assess(result.materials, analysis)
        self.assertEqual(result.assessment.label, "疑似")
        review = review_for(result, action="revise", new_label="确认", resolved_concerns=[1],
                            reason="Checked that the API is offered to users as a relay")
        updated = apply_review(result, review)
        self.assertEqual((updated.label, updated.confidence, updated.review_status), ("确认", .9, "completed"))
        self.assertEqual(len(analysis.concerns), 1, "original model concern must remain in the record")

    def test_review_replaces_model_interpretation_without_overwriting_human_facts(self):
        result = result_for(("exclusion",))
        analysis = result.investigation.analysis
        suggestion = FactSuggestion(fact="relay_clue", value="supported", material_id="m", quote=QUOTES["exclusion"])
        analysis.facts = [suggestion]
        result.assessment = assess(result.materials, analysis)
        revision = suggestion.model_copy(update={"value": "unknown"})
        review = review_for(result, action="revise", new_label="排除", fact_revisions=[revision])
        updated = apply_review(result, review)
        self.assertEqual((updated.label, updated.confidence), ("排除", .9))
        self.assertEqual(analysis.facts[0].value, "supported")
        self.assertEqual(merge_facts(result.materials, analysis, [revision])["relay_clue"].evidence[0].origin, "review")
        human_conflict = result_for(CORE)
        review = review_for(human_conflict, action="revise", new_label="确认",
                            fact_revisions=[FactSuggestion(fact="third_party", value="refuted",
                                                           material_id="m", quote=QUOTES["third_party"])])
        self.assertTrue(merge_facts(human_conflict.materials, human_conflict.investigation.analysis,
                                   review.fact_revisions)["third_party"].conflict)

    def test_invalid_review_cannot_force_a_label(self):
        result = result_for(("relay_clue",), needs_review=True)
        for update in [{"action": "revise", "new_label": "确认"},
                       {"reviewed_at": TIME}, {"action": "revise", "new_label": "疑似", "resolved_concerns": [7]},
                       {"citations": [Citation(material_id="m", quote="invented")]}]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                apply_review(result, review_for(result, **update))

    def test_failed_visit_does_not_refresh_confirmed_evidence_time(self):
        result = result_for(CORE)
        failed = sample(identity="failure", facts=(), access_status="failed", excerpt=None,
                        failure_reason="timeout", evidence_state="unknown",
                        collected_at=datetime(2026, 9, 1, tzinfo=timezone.utc))[1]
        result.materials.append(failed)
        result.assessment = assess(result.materials, result.investigation.analysis)
        row = to_intelligence(result)
        self.assertEqual((row.label, row.last_verified), ("确认", TIME))
        _, recent, _ = sample(identity="recent", facts=CORE, collected_at=failed.collected_at)
        result.materials.append(recent)
        result.assessment = assess(result.materials, result.investigation.analysis)
        self.assertEqual(to_intelligence(result).last_verified, recent.collected_at)

    def test_confidence_is_finite_number_and_pending_review_blocks_export(self):
        for value in [True, "0.9", float("nan"), float("inf"), -1, 1.1]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                TypeAdapter(Score).validate_python(value)
        with self.assertRaisesRegex(ValueError, "review pending"):
            to_intelligence(result_for(("relay_clue",), needs_review=True))

    def test_missing_confirmation_facts_do_not_force_review_of_suspected_label(self):
        result = result_for(("relay_clue",))
        self.assertEqual((result.assessment.label, result.assessment.review_status), ("疑似", "not_required"))
        self.assertEqual(to_intelligence(result).label, "疑似")

    def test_model_only_facts_keep_their_quotes_and_origin_in_export(self):
        result = result_for(CORE, annotations=[])
        analysis = result.investigation.analysis
        analysis.facts = [FactSuggestion(fact=name, value="supported", material_id="m", quote=QUOTES[name])
                          for name in CORE]
        result.assessment = assess(result.materials, analysis)
        row = to_intelligence(result)
        self.assertEqual(row.label, "确认")
        self.assertEqual(row.evidence.basis_material_ids, ["m"])
        materials = {m.material_id: m for m in row.evidence.materials}
        self.assertEqual(materials["m"].annotations, [])
        for fact in row.evidence.facts:
            if fact.fact in CORE:
                self.assertEqual(fact.value, "supported")
                self.assertEqual(fact.evidence[0].origin, "model")
                self.assertEqual(fact.evidence[0].quote, QUOTES[fact.fact])
                self.assertIn(fact.evidence[0].quote, materials[fact.evidence[0].material_id].excerpt)

    def test_export_uses_reviewed_facts_and_preserves_review_evidence(self):
        result = result_for(("exclusion",))
        analysis = result.investigation.analysis
        mistaken = FactSuggestion(fact="relay_clue", value="supported", material_id="m", quote=QUOTES["exclusion"])
        analysis.facts = [mistaken]
        result.assessment = assess(result.materials, analysis)
        revision = mistaken.model_copy(update={"value": "unknown"})
        review = review_for(result, action="revise", new_label="排除", fact_revisions=[revision])
        result.assessment = apply_review(result, review)
        result.review = review
        # 与实际导出一样，从落盘格式重新读取后检查当前事实及人工修订的对应关系。
        row = to_intelligence(DomainResult.model_validate_json(result.model_dump_json()))
        facts = {fact.fact: fact for fact in row.evidence.facts}
        self.assertEqual((row.label, facts["relay_clue"].value), ("排除", "unknown"))
        self.assertEqual(facts["relay_clue"].evidence[0].origin, "review")
        self.assertEqual(facts["exclusion"].evidence[0].origin, "human")
        self.assertEqual(row.evidence.review, review)
        self.assertEqual(analysis.facts[0].value, "supported", "原始模型输出仍保留在批次中")

    def test_export_keeps_original_concern_numbering_after_review(self):
        result = result_for(CORE, needs_review=True)
        review = review_for(result, action="revise", new_label="确认", resolved_concerns=[1])
        result.assessment = apply_review(result, review)
        result.review = review
        row = to_intelligence(result)
        self.assertEqual(row.label, "确认")
        index = row.evidence.review.resolved_concerns[0] - 1
        self.assertEqual(row.evidence.concerns[index], result.investigation.analysis.concerns[0])

    def test_failure_evidence_exports_without_inventing_business_facts(self):
        result = result_for(access_status="failed", excerpt=None, failure_reason="timeout", evidence_state="unknown")
        row = to_intelligence(result)
        self.assertEqual(row.label, "证据不足")
        self.assertEqual(row.evidence.materials[0].failure_reason, "timeout")
        self.assertEqual(row.evidence.citations[0].quote, "timeout")
        self.assertTrue(all(fact.value == "unknown" and not fact.evidence for fact in row.evidence.facts))

    def test_reviewer_can_correct_a_model_result_without_a_pending_flag(self):
        result = result_for(CORE, annotations=[])
        analysis = result.investigation.analysis
        analysis.facts = [FactSuggestion(fact=name, value="supported", material_id="m", quote=QUOTES[name])
                          for name in CORE]
        result.assessment = assess(result.materials, analysis)
        self.assertEqual(result.assessment.review_status, "not_required")
        # 引用存在但语义被高估，复核可以主动降级，无需等待模型自己提出疑点。
        review = review_for(result, action="revise", new_label="疑似", reviewer="AI test reviewer",
                            citations=[Citation(material_id="m", quote=QUOTES["third_party"])],
                            fact_revisions=[FactSuggestion(fact="third_party", value="unknown",
                                                           material_id="m", quote=QUOTES["third_party"])])
        result.assessment = apply_review(result, review)
        result.review = review
        self.assertEqual((result.assessment.label, result.assessment.review_status), ("疑似", "completed"))
        self.assertIn("AI test reviewer", result.assessment.reason)
        with self.assertRaisesRegex(ValueError, "already reviewed"):
            apply_review(result, review)
