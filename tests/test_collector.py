"""Offline checks for the dashboard's research collection boundaries.

Run with ``python3 -m unittest discover -s tests -v``.  Network access is never
needed: collection tests provide their own fetcher and a fixed clock.
"""

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("alignment_collector", ROOT / "scripts" / "collect.py")
collector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
SOURCE = {
    "id": "research-lab",
    "name": "Research Lab",
    "url": "https://research.example/feed.xml",
    "kind": "rss",
    "priority": 2,
    "summary_kind": "publisher excerpt",
}


def raw_item(**overrides):
    raw = {
        "title": "Evaluating AI control against scheming agents",
        "url": "https://research.example/ai-control",
        "published_at": "2026-10-15T14:30:00Z",
        "summary": "Empirical AI safety evaluations of language models examine monitoring and scalable oversight.",
    }
    raw.update(overrides)
    return raw


class DateAndTextTests(unittest.TestCase):
    def test_invalid_feed_response_cannot_look_like_an_empty_success(self):
        self.assertEqual(collector.parse_feed(b"<rss version='2.0'><channel><title>Research</title></channel></rss>"), [])
        with self.assertRaises(ValueError):
            collector.parse_feed(b"<html><body>Service temporarily unavailable</body></html>")

    def test_feed_dates_preserve_the_actual_publication_instant(self):
        expected = datetime(2026, 10, 15, 14, 30, tzinfo=timezone.utc)
        for value in (
            "2026-10-15T14:30:00Z",
            "2026-10-15T10:30:00-04:00",
            "Thu, 15 Oct 2026 14:30:00 GMT",
            "2026-10-15T14:30:00",
        ):
            with self.subTest(value=value):
                self.assertEqual(collector.parse_datetime(value, now=NOW), expected)

    def test_date_only_feed_is_midnight_utc(self):
        self.assertEqual(
            collector.parse_datetime("2026-10-15", now=NOW),
            datetime(2026, 10, 15, tzinfo=timezone.utc),
        )

    def test_invalid_and_future_dates_are_not_replaced_with_today(self):
        for value in (None, "not a date", "2026-02-30", "2026-10-17T12:00:00Z"):
            with self.subTest(value=value):
                self.assertIsNone(collector.parse_datetime(value, now=NOW))

    def test_markup_is_removed_without_including_embedded_code(self):
        value = "<p>AI <b>control</b> &amp; oversight.</p><script>bad()</script><style>.secret{}</style>"
        cleaned = collector.clean_text(value)
        self.assertIn("AI control & oversight.", cleaned)
        self.assertNotIn("<", cleaned)
        self.assertNotIn("bad()", cleaned)
        self.assertNotIn(".secret", cleaned)

    def test_long_summaries_are_bounded(self):
        cleaned = collector.clean_text("word " * 2000, max_length=120)
        self.assertLessEqual(len(cleaned), 120)
        self.assertTrue(cleaned.startswith("word"))


class UrlTests(unittest.TestCase):
    def test_primary_links_reject_executable_and_private_destinations(self):
        for value in (
            "javascript:alert(1)",
            "data:text/html,<h1>unsafe</h1>",
            "file:///etc/passwd",
            "https://reader:secret@research.example/paper",
            "http://localhost/paper",
            "http://127.0.0.1/paper",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/paper",
            "https://research.example/\nheader",
        ):
            with self.subTest(value=value):
                self.assertIsNone(collector.canonical_url(value))

    def test_tracking_does_not_make_a_second_article(self):
        self.assertEqual(
            collector.canonical_url("https://research.example/paper?id=12&utm_source=mail#results"),
            "https://research.example/paper?id=12",
        )

    def test_arxiv_versions_and_pdf_links_share_a_canonical_paper(self):
        expected = "https://arxiv.org/abs/2601.12345"
        for value in (
            "https://arxiv.org/abs/2601.12345v2",
            "https://arxiv.org/pdf/2601.12345v3",
            "http://arxiv.org/abs/2601.12345",
        ):
            with self.subTest(value=value):
                self.assertEqual(collector.canonical_url(value), expected)


class NormalizationTests(unittest.TestCase):
    def test_valid_research_keeps_its_publication_date_and_primary_link(self):
        item = collector.normalize_item(raw_item(), SOURCE, NOW)
        self.assertIsNotNone(item)
        self.assertEqual(item["url"], "https://research.example/ai-control")
        self.assertEqual(
            collector.parse_datetime(item["published_at"]),
            datetime(2026, 10, 15, 14, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(item["date_precision"], "instant")
        self.assertNotIn("published_date", item)

    def test_missing_future_and_stale_dates_cannot_enter_a_daily_edition(self):
        for value in (None, "unknown", "2026-10-17T12:00:00Z", (NOW - timedelta(days=91)).isoformat()):
            with self.subTest(value=value):
                self.assertIsNone(collector.normalize_item(raw_item(published_at=value), SOURCE, NOW))

    def test_unsafe_link_is_rejected_even_if_the_research_text_matches(self):
        self.assertIsNone(collector.normalize_item(raw_item(url="javascript:alert(1)"), SOURCE, NOW))

    def test_broad_arxiv_alignment_search_does_not_admit_image_registration(self):
        item = collector.normalize_item(raw_item(
            title="Model alignment for MRI registration",
            url="https://arxiv.org/abs/2601.11111",
            summary="Neural networks improve image alignment and robustness to training under registration shifts.",
        ), {**SOURCE, "id": "arxiv"}, NOW)
        self.assertIsNone(item)

    def test_repeated_paper_from_multiple_feeds_is_one_library_item(self):
        first = collector.normalize_item(raw_item(url="https://arxiv.org/abs/2601.12345v1"), SOURCE, NOW)
        second_source = {**SOURCE, "id": "aggregator", "name": "Aggregator", "summary_kind": "aggregator summary"}
        second = collector.normalize_item(raw_item(url="https://arxiv.org/pdf/2601.12345v2?utm_source=feed"), second_source, NOW)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        deduplicated = collector.deduplicate([first, second])
        self.assertEqual(len(deduplicated), 1)
        self.assertEqual(deduplicated[0]["url"], "https://arxiv.org/abs/2601.12345")
        self.assertEqual(deduplicated[0]["source"], SOURCE["name"])
        self.assertEqual(deduplicated[0]["summary_kind"], "publisher excerpt")


class CollectionHealthTests(unittest.TestCase):
    @staticmethod
    def offline_fetcher(_source):
        raise RuntimeError("Offline fixture: research source unavailable")

    def previous_edition(self, now=NOW - timedelta(days=1)):
        return collector.collect(
            [SOURCE], now=now,
            fetcher=lambda _source: [raw_item(published_at=(now - timedelta(hours=2)).isoformat())],
        )

    def test_source_outage_retains_research_without_claiming_a_fresh_success(self):
        previous = self.previous_edition()
        self.assertEqual(len(previous["items"]), 1)
        edition = collector.collect([SOURCE], previous=previous, now=NOW, fetcher=self.offline_fetcher)
        self.assertEqual(len(edition["items"]), 1)
        self.assertEqual(edition["items"][0]["id"], previous["items"][0]["id"])
        self.assertEqual(edition["items"][0]["published_at"], previous["items"][0]["published_at"])
        self.assertEqual(edition["last_successful_update"], previous["last_successful_update"])
        self.assertNotEqual(edition["sources"][0]["status"], "ok")
        self.assertEqual(collector.parse_datetime(edition["generated_at"]), NOW)

    def test_empty_successful_feed_keeps_history_and_reports_success(self):
        previous = self.previous_edition()
        edition = collector.collect([SOURCE], previous=previous, now=NOW, fetcher=lambda _source: [])
        self.assertEqual([item["id"] for item in edition["items"]], [previous["items"][0]["id"]])
        self.assertEqual(edition["sources"][0]["status"], "ok")
        self.assertEqual(collector.parse_datetime(edition["last_successful_update"]), NOW)

    def test_successful_refresh_can_correct_an_existing_article(self):
        previous = self.previous_edition()
        corrected_title = "Evaluating AI control against scheming agents: corrected findings"
        edition = collector.collect(
            [SOURCE], previous=previous, now=NOW,
            fetcher=lambda _source: [raw_item(
                title=corrected_title,
                published_at=previous["items"][0]["published_at"],
            )],
        )
        self.assertEqual(len(edition["items"]), 1)
        self.assertEqual(edition["items"][0]["id"], previous["items"][0]["id"])
        self.assertEqual(edition["items"][0]["title"], corrected_title)

    def test_calendar_publication_date_survives_normalization_and_retention(self):
        for publisher_date in ("2026-10-15", "October 15, 2026"):
            with self.subTest(publisher_date=publisher_date):
                previous = collector.collect(
                    [SOURCE], now=NOW,
                    fetcher=lambda _source: [raw_item(published_at=publisher_date)],
                )
                self.assertEqual(len(previous["items"]), 1)
                item = previous["items"][0]
                self.assertEqual(item["published_date"], "2026-10-15")
                self.assertEqual(item["date_precision"], "day")
                retained = collector.collect(
                    [SOURCE], previous=previous, now=NOW + timedelta(days=1), fetcher=self.offline_fetcher,
                )["items"][0]
                self.assertEqual(retained["published_date"], "2026-10-15")
                self.assertEqual(retained["date_precision"], "day")

    def test_retention_limit_also_applies_during_an_outage(self):
        previous = self.previous_edition(now=NOW - timedelta(days=92))
        self.assertEqual(len(previous["items"]), 1)
        edition = collector.collect([SOURCE], previous=previous, now=NOW, fetcher=self.offline_fetcher)
        self.assertEqual(edition["items"], [])
        self.assertNotEqual(edition["sources"][0]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
