"""候选与材料测试：关注规范化、去重、主体和引用边界，不访问网络。"""

import unittest
from unittest.mock import patch

from helpers import sample
from relay_intel.candidates import normalize, prepare_candidates
from relay_intel.contracts import Citation, Lead, Link, Material
from relay_intel.investigation import MaterialTools, prepare_materials


class CandidateTests(unittest.TestCase):
    def test_normalization_and_full_host_deduplication(self):
        for raw, expected in [
            ("  API.Example.COM. ", "api.example.com"),
            ("https://API.Example.COM:443/path?q=1", "api.example.com"),
            ("https://例子.中国/", "xn--fsqu00a.xn--fiqs8s"),
        ]:
            self.assertEqual(normalize(raw), expected)
        source = sample()[0].sources[0]
        extra = source.model_copy(update={"lead_id": "another", "raw_value": "https://RELAY.example.com/"})
        sibling = source.model_copy(update={"lead_id": "sibling", "raw_value": "chat.example.com"})
        candidates, issues = prepare_candidates([source, source, extra, sibling])
        self.assertEqual(len(candidates), 2)
        self.assertEqual(len(next(c for c in candidates if c.domain == "relay.example.com").sources), 2)
        self.assertEqual(issues, [])

    def test_invalid_hosts_and_offline_suffix_snapshot(self):
        for raw in ["127.0.0.1", "*.example.com", "https://u:p@example.com", "bad_host.com",
                    "localhost", "a.unknownsuffix", "co.uk", "https://[::1]/", "../escape"]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize(raw)
        with patch("requests.Session.get", side_effect=AssertionError("network forbidden")):
            self.assertEqual(normalize("owner.github.io"), "owner.github.io")

    def test_conflicting_ids_require_input_correction(self):
        source = sample()[0].sources[0]
        changed = source.model_copy(update={"raw_value": "other.example.com"})
        with self.assertRaisesRegex(ValueError, "conflicting"):
            prepare_candidates([source, changed])
        _, material, _ = sample()
        with self.assertRaisesRegex(ValueError, "conflicting"):
            prepare_materials([material, material.model_copy(update={"excerpt": "Changed"})], [sample()[0]])

    def test_bad_candidate_is_reported_without_losing_valid_rows(self):
        source = sample()[0].sources[0]
        bad = source.model_copy(update={"lead_id": "bad", "raw_value": "127.0.0.1"})
        candidates, issues = prepare_candidates([source, bad])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(issues[0].location, "bad")

    def test_input_requires_public_source_timezone_and_complete_seed_relation(self):
        source = sample()[0].sources[0]
        for update in [{"source_url": "https://u:p@example.com/"}, {"discovered_at": "2026-01-01"},
                       {"seed_domain": "seed.example.com"}]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                Lead.model_validate(source.model_dump() | update)

    def test_material_subject_and_annotation_checks(self):
        candidate, material, _ = sample()
        with self.assertRaises(ValueError):
            Material.model_validate(material.model_dump() | {"annotations": [
                {"fact": "exclusion", "value": "supported", "quote": "invented"}]})
        cross = material.model_copy(update={"source_url": "https://other.example.com/"})
        grouped, issues = prepare_materials([cross], [candidate])
        self.assertEqual(grouped[candidate.domain], [])
        self.assertIn("exact subject", issues[0].reason)
        cross.excerpt += " About relay.example.com."
        grouped, issues = prepare_materials([cross], [candidate])
        self.assertEqual(len(grouped[candidate.domain]), 1)
        self.assertEqual(issues, [])

    def test_tools_cannot_read_other_domains_or_skip_counterevidence(self):
        _, material, analysis = sample()
        other = material.model_copy(update={"material_id": "other"})
        toolkit = MaterialTools(material.domain, [material, other])
        for name, args in [
            ("shell", {"domain": material.domain}),
            ("read_materials", {"domain": "other.example.com"}),
            ("read_materials", {"domain": material.domain, "material_ids": ["missing"]}),
        ]:
            with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                toolkit.execute(name, args)
        toolkit.execute("read_materials", {"domain": material.domain, "material_ids": ["m"]})
        with self.assertRaisesRegex(ValueError, "read all"):
            toolkit.validate_analysis(analysis)

    def test_links_and_quotes_must_be_observed(self):
        _, material, analysis = sample()
        material.excerpt += " Chat: https://chat.example.com/"
        toolkit = MaterialTools(material.domain, [material])
        for name in ("read_materials", "related_links"):
            toolkit.execute(name, {"domain": material.domain})
        analysis.related_links = [Link(material_id="m", url="https://chat.example.com/",
                                       context=material.excerpt, relation="chat service")]
        toolkit.validate_analysis(analysis)
        analysis.related_links[0].url = "https://invented.example.com/"
        with self.assertRaisesRegex(ValueError, "context"):
            toolkit.validate_analysis(analysis)
        analysis.related_links = []
        analysis.citations = [Citation(material_id="m", quote="invented")]
        with self.assertRaisesRegex(ValueError, "quote"):
            toolkit.validate_analysis(analysis)
