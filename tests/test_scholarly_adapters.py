from datetime import datetime, timezone
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from scripts.scholarly_adapters import fetch_openreview


class ScholarlyDiscoveryTests(unittest.TestCase):
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    source = {"id": "openreview", "queries": ["AI alignment"], "page_size": 2, "backfill_pages": 1}

    def note(self, identifier, **dates):
        return {"id": identifier, "forum": identifier, "content": {"title": {"value": identifier}, "abstract": {"value": "A paper about AI alignment."}, "authors": {"value": ["A. Researcher"]}, "authorids": {"value": ["private@example.org"]}}, **dates}

    def test_unknown_public_date_does_not_become_submission_creation_date(self):
        body = json.dumps({"notes": [self.note("one", cdate=1000000000000)], "count": 1}).encode()
        result = fetch_openreview(self.source, {}, self.now, lambda url: body)
        self.assertIsNone(result["items"][0]["published_at"])
        self.assertNotIn("private@example.org", json.dumps(result))
        self.assertTrue(result["coverage"]["backfill_complete"])

    def test_public_date_and_exact_phrase_query_are_preserved(self):
        requested = []
        def request(url):
            requested.append(parse_qs(urlsplit(url).query))
            return json.dumps({"notes": [self.note("one", odate=1750000000000)], "count": 1}).encode()
        result = fetch_openreview(self.source, {}, self.now, request)
        self.assertEqual(requested[0]["type"], ["exact"])
        self.assertEqual(requested[0]["source"], ["forum"])
        self.assertTrue(result["items"][0]["published_at"].startswith("2025-"))

    def test_failed_page_does_not_mutate_callers_checkpoint(self):
        state = {"queries": {"AI alignment": {"backfill_offset": 4}}}
        original = json.loads(json.dumps(state))
        def request(url):
            offset = int(parse_qs(urlsplit(url).query)["offset"][0])
            if offset:
                raise OSError("outage")
            return json.dumps({"notes": [self.note("one"), self.note("two")], "count": 20}).encode()
        with self.assertRaises(OSError):
            fetch_openreview(self.source, state, self.now, request)
        self.assertEqual(state, original)

    def test_search_ceiling_is_a_visible_coverage_gap(self):
        body = json.dumps({"notes": [self.note("one")], "count": 10000}).encode()
        result = fetch_openreview(self.source, {}, self.now, lambda url: body)
        self.assertTrue(result["coverage"]["truncated"])
        self.assertFalse(result["coverage"]["backfill_complete"])
        self.assertTrue(any("ceiling" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
