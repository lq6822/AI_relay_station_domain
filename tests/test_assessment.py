import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from relay_intel.assessment import apply_review, assess
from relay_intel.contracts import (
    Analysis, Annotation, Citation, Concern, FactSuggestion, Intelligence, Investigation,
    Material, Policy, Review, ToolCall, now,
)
from relay_intel.investigation import MaterialTools, merge_facts

TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


class AssessmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = Policy.model_validate_json((Path(__file__).parents[1]/"config/policy.json").read_text(encoding="utf-8"))

    def material(self, facts=(), **overrides):
        values = dict(material_id="m", domain="relay.example.com", source_url="https://relay.example.com/",
                      collected_at=TIME, access_status="ok", evidence_state="current",
                      excerpt="We are an independent relay; provide model access; proxy requests upstream.",
                      annotations=[Annotation(fact=f, value="supported", quote="relay") for f in facts])
        values.update(overrides)
        return Material(**values)

    def investigation(self, materials, suggestions=(), concerns=()):
        citation = Citation(material_id=materials[0].material_id,
                            quote=materials[0].excerpt or materials[0].failure_reason)
        analysis = Analysis(facts=list(suggestions), suggested_label="确认", reason="Model suggestion only",
                            quality_reason="Traceable materials", quality_citations=[citation], concerns=list(concerns),
                            related_links=[], limitations=[])
        tools = MaterialTools("relay.example.com", materials)
        tools.execute("read_materials", {"domain": "relay.example.com"})
        tools.validate_analysis(analysis)
        return Investigation(run_id="r", domain="relay.example.com", version=1, fingerprint="fp",
                             materials=materials, facts=merge_facts(materials, analysis), analysis=analysis,
                             tool_calls=[ToolCall(name="read_materials", material_ids=list(tools.read_ids), result="ok")],
                             requests=2, usage={}, execution="offline_test", created_at=now())

    def test_four_labels_program_overrides_model(self):
        for facts, label in [(('third_party', 'model_access', 'upstream_proxy'), "确认"),
                             (('relay_clue',), "疑似"), (('exclusion',), "排除"), ((), "证据不足")]:
            with self.subTest(label=label):
                actual = assess(self.investigation([self.material(facts)]), self.policy, 1)
                self.assertEqual(actual.label, label)
                self.assertEqual(actual.confidence, .9)

    def test_access_failure_is_high_confidence_insufficient_not_excluded(self):
        m = self.material(access_status="failed", excerpt=None, failure_reason="Connection timed out",
                          evidence_state="unknown")
        result = assess(self.investigation([m]), self.policy, 1)
        self.assertEqual((result.label, result.confidence, result.review_status), ("证据不足", .9, "not_required"))

    def test_unknown_is_not_refuted(self):
        inv = self.investigation([self.material()])
        self.assertTrue(all(f.value == "unknown" and not f.conflict for f in inv.facts))

    def test_conflict_is_not_majority_voted_away(self):
        m = self.material(("third_party", "model_access", "upstream_proxy"))
        opposite = FactSuggestion(fact="third_party", value="refuted", quote="relay", material_id="m")
        inv = self.investigation([m], [opposite])
        result = assess(inv, self.policy, 1)
        self.assertEqual((result.label, result.confidence, result.review_status), ("疑似", .55, "pending"))
        fact = next(f for f in inv.facts if f.fact == "third_party")
        self.assertEqual({e.origin for e in fact.evidence}, {"human", "model"})

    def test_mutually_inconsistent_directions_require_review(self):
        result = assess(self.investigation([self.material(("exclusion", "relay_clue"))]), self.policy, 1)
        self.assertEqual((result.label, result.confidence), ("疑似", .55))

    def test_historical_evidence_cannot_confirm_current_host(self):
        result = assess(self.investigation([self.material(("third_party", "model_access", "upstream_proxy"),
                                                         evidence_state="historical")]), self.policy, 1)
        self.assertEqual((result.label, result.confidence, result.review_status), ("疑似", .75, "pending"))

    def test_secondary_evidence_is_limited(self):
        result = assess(self.investigation([self.material(("exclusion",), source_kind="secondary")]), self.policy, 1)
        self.assertEqual((result.label, result.confidence), ("排除", .75))

    def test_subject_uncertainty_cannot_confirm(self):
        result = assess(self.investigation([self.material(("third_party", "model_access", "upstream_proxy"),
                                                         subject_relation="uncertain")]), self.policy, 1)
        self.assertEqual((result.label, result.confidence), ("疑似", .55))

    def test_irrelevant_unknowns_do_not_force_review_of_exclusion(self):
        m = self.material(("exclusion",))
        concern = Concern(kind="missing", reason="No upstream details", affects_decision=False,
                          citations=[Citation(material_id="m", quote="relay")])
        result = assess(self.investigation([m], concerns=[concern]), self.policy, 1)
        self.assertEqual(result.review_status, "not_required")

    def test_impactful_business_concern_is_conservative(self):
        concern = Concern(kind="business", reason="Ordinary app or API relay unclear", affects_decision=True,
                          citations=[Citation(material_id="m", quote="relay")])
        result = assess(self.investigation([self.material(("exclusion",))], concerns=[concern]), self.policy, 1)
        self.assertEqual((result.label, result.confidence), ("证据不足", .55))

    def test_bad_annotation_citation_and_failed_fact_rejected(self):
        with self.assertRaises(ValueError):
            self.material(annotations=[Annotation(fact="exclusion", value="supported", quote="not present")])
        with self.assertRaises(ValueError):
            self.material(("exclusion",), access_status="failed", excerpt=None, failure_reason="timeout")
        suggestion = FactSuggestion(fact="exclusion", value="supported", quote="missing", material_id="m")
        with self.assertRaisesRegex(ValueError, "quote cannot be located"):
            self.investigation([self.material()], [suggestion])

    def test_tool_scope_and_unread_counterevidence(self):
        materials = [self.material(), self.material(material_id="other")]
        inv = self.investigation(materials)
        tools = MaterialTools("relay.example.com", materials)
        with self.assertRaises(ValueError):
            tools.execute("read_materials", {"domain": "other.example.com"})
        with self.assertRaises(ValueError):
            tools.execute("read_materials", {"domain": "relay.example.com", "material_ids": ["outside"]})
        tools.execute("read_materials", {"domain": "relay.example.com", "material_ids": ["m"]})
        with self.assertRaisesRegex(ValueError, "read all"):
            tools.validate_analysis(inv.analysis)

    def test_exact_material_domain_required(self):
        with self.assertRaises(ValueError):
            MaterialTools("relay.example.com", [self.material(domain="other.example.com")])

    def test_cross_host_exact_relation_requires_locatable_target(self):
        cross = self.material(("exclusion",), source_url="https://unrelated.example.com/about")
        with self.assertRaisesRegex(ValueError, "subject"):
            MaterialTools("relay.example.com", [cross])
        cross.excerpt += " This description concerns https://relay.example.com/."
        MaterialTools("relay.example.com", [cross])

    def review_action(self, current, **updates):
        args = dict(run_id="r", domain=current.domain, assessment_version=current.version,
                    action="accept", reviewer="test reviewer", reviewed_at=now(), reason="Conservative result accepted",
                    citations=[Citation(material_id="m", quote="relay")])
        args.update(updates)
        return Review(**args)

    def test_accept_retains_unknowns_and_is_idempotent(self):
        inv = self.investigation([self.material(("relay_clue",))])
        current = assess(inv, self.policy, 1)
        action = self.review_action(current)
        result = apply_review(current, inv, action, self.policy)
        self.assertEqual((result.version, result.label, result.confidence, result.review_status), (2, "疑似", .9, "completed"))
        self.assertEqual(apply_review(result, inv, action, self.policy), result)
        self.assertEqual(next(f.value for f in inv.facts if f.fact == "third_party"), "unknown")

    def test_revision_recomputes_confidence_and_preserves_original(self):
        m = self.material(("exclusion",))
        suggestion = FactSuggestion(fact="relay_clue", value="supported", quote="relay", material_id="m")
        inv = self.investigation([m], [suggestion])
        current = assess(inv, self.policy, 1)
        action = self.review_action(current, action="revise", new_label="排除",
                                   fact_revisions=[suggestion.model_copy(update={"value": "unknown"})])
        result = apply_review(current, inv, action, self.policy)
        self.assertEqual((result.label, result.confidence, result.review_status), ("排除", .9, "completed"))
        self.assertEqual(inv.analysis.facts[0].value, "supported")
        self.assertEqual(current.confidence, .55)

    def test_unsupported_review_label_and_stale_review_rejected(self):
        inv = self.investigation([self.material(("relay_clue",))])
        current = assess(inv, self.policy, 1)
        for update in [{"action": "revise", "new_label": "确认"}, {"assessment_version": 9},
                       {"reviewed_at": now() - timedelta(days=1)}]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                apply_review(current, inv, self.review_action(current, **update), self.policy)

    def test_confidence_finite_range_and_type(self):
        from pydantic import TypeAdapter
        from relay_intel.contracts import Score
        adapter = TypeAdapter(Score)
        for value in [True, "0.9", float("nan"), float("inf"), -0.1, 1.1, None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                adapter.validate_python(value)
        self.assertEqual(adapter.validate_python(.9), .9)


if __name__ == "__main__":
    unittest.main()
