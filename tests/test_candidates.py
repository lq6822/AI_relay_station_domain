import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from relay_intel.candidates import expand_seed, merge_ids, normalize, prepare, registered_domain
from relay_intel.contracts import Lead

TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


class CandidateTests(unittest.TestCase):
    def lead(self, identity="l1", raw="Api.Example.COM."):
        return Lead(lead_id=identity, raw_value=raw, source_url="https://source.example.com/list",
                    discovery_method="manual", discovered_at=TIME)

    def test_normalization_and_idna(self):
        for raw, wanted in [("  API.Example.COM.  ", "api.example.com"),
                            ("https://API.Example.COM:443/path?q=a", "api.example.com"),
                            ("https://例子.中国/", "xn--fsqu00a.xn--fiqs8s")]:
            with self.subTest(raw=raw):
                self.assertEqual(normalize(raw), wanted)

    def test_invalid_hosts(self):
        for raw in ["", "*.example.com", "127.0.0.1", "https://[::1]/", "https://u:p@example.com",
                    "bad_host.com", "example.com..", "localhost", "ftp://example.com", "a/b.com",
                    "https://example.com:bad", "example.com\\evil", "a.unknownsuffix", "co.uk", "1.2.3.4"]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize(raw)

    def test_bundled_suffixes_no_network(self):
        with patch("requests.Session.get", side_effect=AssertionError("network forbidden")):
            self.assertEqual(registered_domain("a.b.example.co.uk"), "example.co.uk")
            self.assertEqual(registered_domain("a.owner.github.io"), "owner.github.io")

    def test_sources_merge_and_full_hostname_uniqueness(self):
        candidates, issues = prepare("r", [self.lead(), self.lead("l2", "https://api.example.com/a"),
                                          self.lead("l3", "chat.example.com")])
        self.assertEqual(len(candidates), 2)
        self.assertEqual(len(candidates[0].sources), 2)
        self.assertEqual(issues, [])

    def test_same_id_idempotence_and_conflict(self):
        row = self.lead()
        rows, issues = merge_ids([row], [row, self.lead(raw="different.example.com")], "lead_id", "r")
        self.assertEqual(rows, [row])
        self.assertEqual(len(issues), 1)

    def test_bad_candidate_does_not_stop_other_rows(self):
        candidates, issues = prepare("r", [self.lead(), self.lead("bad", "127.0.0.1")])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(issues[0].location, "bad")

    def test_source_credentials_and_timezone_rejected(self):
        for update in [{"source_url": "https://u:p@example.com/"}, {"discovered_at": "2026-01-01T00:00:00"}]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                Lead.model_validate(self.lead().model_dump() | update)

    def test_seed_fields_must_be_complete(self):
        with self.assertRaises(ValueError):
            Lead.model_validate(self.lead().model_dump() | {"seed_domain": "seed.example.com"})

    def test_expansion_provenance_and_no_inherited_assessment(self):
        # This lightweight fixture isolates candidate expansion; Agent behavior lives in workflow tests.
        from types import SimpleNamespace
        from relay_intel.contracts import Link, Material, ToolCall
        candidate = prepare("r", [self.lead(raw="seed.example.com")])[0][0]
        material = Material(material_id="m", domain=candidate.domain,
                            source_url="https://seed.example.com/", collected_at=TIME,
                            access_status="ok", evidence_state="current",
                            excerpt="Chat service: https://chat.example.com/")
        link = Link(material_id="m", url="https://chat.example.com/", context=material.excerpt, relation="chat service")
        inv = SimpleNamespace(version=1, materials=[material], created_at=TIME,
                              requests=3, usage={"input_tokens": 30}, execution="offline_test",
                              tool_calls=[ToolCall(name="related_links", material_ids=["m"], result="ok")],
                              analysis=SimpleNamespace(related_links=[link]))
        assessment = SimpleNamespace(label="确认", confidence=.9, review_status="not_required", investigation_version=1)
        leads, record = expand_seed(candidate, inv, assessment, [candidate], 5)
        added, issues = prepare("r", leads, [candidate])
        child = next(c for c in added if c.domain == "chat.example.com")
        self.assertFalse(hasattr(child, "label"))
        self.assertEqual(child.sources[0].seed_domain, candidate.domain)
        self.assertEqual(child.sources[0].material_id, "m")
        self.assertEqual(record.added_domains, ["chat.example.com"])
        self.assertEqual(issues, [])
        assessment.review_status = "pending"
        with self.assertRaises(ValueError):
            expand_seed(candidate, inv, assessment, [candidate], 5)
        assessment.review_status = "completed"
        inv.analysis.related_links = []
        _, empty = expand_seed(candidate, inv, assessment, [candidate], 5)
        self.assertEqual(empty.added_domains, [])
        self.assertEqual(empty.material_ids, ["m"])
        inv.tool_calls = []
        with self.assertRaises(ValueError):
            expand_seed(candidate, inv, assessment, [candidate], 5)


if __name__ == "__main__":
    unittest.main()
