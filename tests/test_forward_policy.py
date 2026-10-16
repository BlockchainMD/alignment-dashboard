"""Publication-scope invariants, independent of source transport implementations."""
from datetime import datetime, timezone
import unittest

from scripts.archive_store import active_archive_items, publication_scope
from scripts.collect import collect


START = "2026-10-16T23:48:48Z"
NOW = datetime(2026, 10, 17, 1, tzinfo=timezone.utc)
POLICY = {"mode": "forward_only", "publication_start": START}
SOURCE = {"id": "lab", "name": "Research lab", "url": "https://example.org/", "scope": "alignment"}


def record(**changes):
    return {"id": "paper", "title": "Testing model honesty", "url": "https://example.org/paper",
            "published_at": START, "date_precision": "instant", "first_seen": "2026-10-17T00:00:00Z",
            "relevance": "relevant", **changes}


class ForwardPolicyTests(unittest.TestCase):
    def test_exact_cutoff_is_inclusive_and_old_publications_stay_out(self):
        self.assertEqual(publication_scope(record(), POLICY), (True, "published_after_start"))
        self.assertEqual(publication_scope(record(published_at="2026-10-16T23:48:47Z"), POLICY),
                         (False, "before_collection_start"))

    def test_newly_found_cutoff_day_date_requires_review_without_changing_date(self):
        article = record(published_at="2026-10-16T00:00:00Z", date_precision="day")
        active = active_archive_items({"items": [article], "coverage_policy": POLICY})
        self.assertEqual(active[0]["temporal_status"], "publication_time_uncertain")
        self.assertEqual(active[0]["relevance"], "candidate")
        self.assertEqual(active[0]["published_at"], article["published_at"])
        self.assertEqual(article["relevance"], "relevant")

    def test_cutoff_day_record_seen_earlier_cannot_reappear(self):
        article = record(published_at="2026-10-16T00:00:00Z", date_precision="day", first_seen="2026-10-16T12:00:00Z")
        self.assertFalse(publication_scope(article, POLICY)[0])

    def test_undated_and_year_only_records_need_new_discovery_and_review(self):
        for changes in ({"published_at": None}, {"published_at": None, "publication_year": 2026}):
            with self.subTest(changes=changes):
                active = active_archive_items({"items": [record(**changes)], "coverage_policy": POLICY})
                self.assertEqual(active[0]["temporal_status"], "publication_date_unverified")
                self.assertEqual(active[0]["relevance"], "candidate")
                self.assertIsNone(active[0]["published_at"])
                old = record(**{**changes, "first_seen": "2026-10-16T12:00:00Z"})
                self.assertFalse(publication_scope(old, POLICY)[0])
        self.assertFalse(publication_scope(record(published_at=None, publication_year=2004), POLICY)[0])

    def test_missing_or_legacy_discovery_date_does_not_become_new(self):
        for article in (record(published_at=None, first_seen=None),
                        record(published_at=None, legacy_before_forward_scope=True)):
            with self.subTest(article=article):
                self.assertFalse(publication_scope(article, POLICY)[0])

    def test_no_policy_keeps_existing_archive_behavior(self):
        articles = [record(published_at="2004-01-01"), record(published_at=None, first_seen=None)]
        self.assertEqual(active_archive_items({"items": articles}), articles)

    def test_invalid_forward_policy_fails_before_fetching(self):
        requests = []
        with self.assertRaisesRegex(ValueError, "publication_start"):
            collect([SOURCE], now=NOW, coverage_policy={"mode": "forward_only"},
                    fetcher=lambda source: requests.append(source))
        self.assertEqual(requests, [])

    def test_known_urls_and_forward_policy_reach_transport(self):
        previous = collect([SOURCE], now=NOW, fetcher=lambda _: [record()])
        requests = []
        def fetch(source):
            requests.append(source)
            return {"items": [], "state": {}, "coverage": {"validated_empty": True}}
        collect([SOURCE], previous=previous, now=NOW, coverage_policy=POLICY, backfill_pages=0, fetcher=fetch)
        self.assertTrue(requests[0]["forward_only"])
        self.assertEqual(requests[0]["publication_start"], START)
        self.assertIn("https://example.org/paper", requests[0]["known_urls"])

    def test_trusted_source_news_and_opinion_remain_review_candidates(self):
        for title in ("Our AI safety newsletter", "Weekly alignment news roundup", "We are hiring",
                      "Thoughts on AI safety", "Opinion: alignment research priorities", "Introducing our new office"):
            with self.subTest(title=title):
                archive = collect([SOURCE], now=NOW, coverage_policy=POLICY,
                                  fetcher=lambda _: [record(title=title)])
                self.assertEqual(archive["stats"]["total_items"], 1)
                self.assertEqual(archive["items"][0]["relevance"], "candidate")
                self.assertIn("needs review", archive["items"][0]["relevance_reason"])


if __name__ == "__main__":
    unittest.main()
