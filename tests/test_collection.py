"""公开取证边界：脚本不是正文，摘录不能伪造，访问失败不能制造业务事实。"""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_inputs import build_inputs
from scripts.collect_sources import PageText


class CollectionTests(unittest.TestCase):
    def test_scripts_are_not_evidence_and_only_observed_links_are_kept(self):
        parser = PageText()
        parser.feed('<meta name="description" content="Public&nbsp; API\u202f page">'
                    '<title>Public title</title><script>fake relay claim</script>'
                    '<style>.hidden{}</style><svg><text>fake</text></svg>'
                    '<p>Public API documentation</p><a href="/docs">Docs</a>')
        self.assertEqual(parser.parts, ["Public API page", "Public title", "Public API documentation", "Docs"])
        self.assertEqual(parser.links, ["/docs"])

    def test_selection_rejects_invented_quote_or_link_and_keeps_failure(self):
        record = dict(source_id="page", requested_url="https://relay.example.com/",
                      collected_at="2026-09-01T00:00:00+00:00", text="Public API page",
                      links=["https://relay.example.com/docs"])
        item = dict(domain="relay.example.com", discovery_source=record["requested_url"],
                    discovery_method="public_source", evidence=[dict(source_id="page", quotes=["Public API"])])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "page.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            _, materials = build_inputs([item], Path(folder))
            self.assertEqual(materials[0].excerpt, "Public API")
            item["evidence"][0]["quotes"] = ["invented"]
            with self.assertRaisesRegex(ValueError, "quote not found"):
                build_inputs([item], Path(folder))
            item["evidence"][0].update(quotes=["Public API"], links=["https://invented.example.com/"])
            with self.assertRaisesRegex(ValueError, "unobserved link"):
                build_inputs([item], Path(folder))
            # 一旦采集失败，残留摘录或链接都不能转成成功证据。
            record["error"] = "HTTP 403"
            path.write_text(json.dumps(record), encoding="utf-8")
            _, materials = build_inputs([item], Path(folder))
            self.assertEqual(materials[0].access_status, "failed")
            self.assertIsNone(materials[0].excerpt)
            self.assertEqual(materials[0].annotations, [])
