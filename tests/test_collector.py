"""Offline checks for the dashboard's research collection boundaries.

Run with ``python3 -m unittest discover -s tests -v``.  Network access is never
needed: collection tests provide their own fetcher and a fixed clock.
"""

from datetime import datetime, timedelta, timezone
from contextlib import chdir, redirect_stderr, redirect_stdout
from copy import deepcopy
import importlib.util
import hashlib
import gzip
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


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
    "scope": "alignment",
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

    def test_supported_feed_is_not_silently_capped_at_two_hundred_fifty_entries(self):
        items = "".join(
            f"<item><title>Research {index}</title><link>https://research.example/{index}</link></item>"
            for index in range(301)
        )
        parsed = collector.parse_feed(f"<rss version='2.0'><channel>{items}</channel></rss>".encode())
        self.assertEqual(len(parsed), 301)
        self.assertEqual(parsed[-1]["url"], "https://research.example/300")

    def test_feed_rejects_embedded_entity_declarations(self):
        payload = b'<!DOCTYPE rss [<!ENTITY x "hidden">]><rss><channel><title>&x;</title></channel></rss>'
        with self.assertRaises(ValueError):
            collector.parse_feed(payload)

    def test_arxiv_api_error_entry_is_not_a_research_record(self):
        payload = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <id>http://arxiv.org/api/errors#incorrect_id_format</id><title>Error</title>
          <summary>Invalid identifier</summary></entry></feed>'''
        with self.assertRaises(ValueError):
            collector.parse_feed(payload)


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
    def test_citation_year_does_not_invent_a_publication_day(self):
        for year in (2004, "2004", NOW.year):
            with self.subTest(year=year):
                item = collector.normalize_item(raw_item(published_at=None, publication_year=year), SOURCE, NOW)
                self.assertIsNone(item["published_at"])
                self.assertEqual(item["publication_year"], int(year))
                self.assertEqual(item["date_precision"], "year")
                self.assertNotIn("published_date", item)

    def test_invalid_future_or_boolean_citation_year_stays_unknown(self):
        for year in (None, NOW.year + 1, 999, 10000, "unknown", True, 2020.0):
            with self.subTest(year=year):
                item = collector.normalize_item(raw_item(published_at=None, publication_year=year), SOURCE, NOW)
                self.assertIsNone(item["published_at"])
                self.assertEqual(item["date_precision"], "unknown")
                self.assertNotIn("publication_year", item)
                self.assertNotIn("published_date", item)

    def test_exact_publication_date_takes_precedence_over_a_conflicting_citation_year(self):
        item = collector.normalize_item(raw_item(publication_year=2004), SOURCE, NOW)
        self.assertEqual(item["publication_year"], 2026)
        self.assertEqual(item["date_precision"], "instant")
        self.assertEqual(item["published_at"], "2026-10-15T14:30:00Z")

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

    def test_unknown_and_future_dates_are_preserved_without_inventing_publication_time(self):
        for value in (None, "unknown", "2026-10-17T12:00:00Z"):
            with self.subTest(value=value):
                item = collector.normalize_item(raw_item(published_at=value), SOURCE, NOW)
                self.assertIsNotNone(item)
                self.assertIsNone(item["published_at"])
                self.assertEqual(item["date_precision"], "unknown")
                self.assertEqual(collector.parse_datetime(item["first_seen"]), NOW)

    def test_historical_publications_are_collected_without_a_retention_cutoff(self):
        old_date = (NOW - timedelta(days=900)).isoformat()
        item = collector.normalize_item(raw_item(published_at=old_date), SOURCE, NOW)
        self.assertIsNotNone(item)
        self.assertEqual(collector.parse_datetime(item["published_at"]), collector.parse_datetime(old_date))

    def test_trusted_alignment_sources_do_not_need_current_keyword_matches(self):
        item = collector.normalize_item(raw_item(
            title="A surprising new result", summary="Our latest technical report.",
        ), SOURCE, NOW)
        self.assertIsNotNone(item)
        self.assertEqual(item["relevance"], "relevant")

    def test_unsafe_link_is_rejected_even_if_the_research_text_matches(self):
        self.assertIsNone(collector.normalize_item(raw_item(url="javascript:alert(1)"), SOURCE, NOW))

    def test_broad_arxiv_search_retains_ambiguous_research_for_review(self):
        item = collector.normalize_item(raw_item(
            title="Model alignment for MRI registration",
            url="https://arxiv.org/abs/2601.11111",
            summary="Neural networks improve image alignment and robustness to training under registration shifts.",
        ), {**SOURCE, "id": "arxiv", "scope": "mixed"}, NOW)
        self.assertIsNotNone(item)
        self.assertEqual(item["relevance"], "candidate")

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
        self.assertEqual(edition["items"][0]["last_seen"], previous["items"][0]["last_seen"])
        self.assertEqual(edition["last_successful_update"], previous["last_successful_update"])
        self.assertNotEqual(edition["sources"][0]["status"], "ok")
        self.assertEqual(collector.parse_datetime(edition["generated_at"]), NOW)

    def test_newly_empty_source_keeps_history_and_reports_a_coverage_warning(self):
        previous = self.previous_edition()
        edition = collector.collect([SOURCE], previous=previous, now=NOW, fetcher=lambda _source: [])
        self.assertEqual([item["id"] for item in edition["items"]], [previous["items"][0]["id"]])
        self.assertEqual(edition["sources"][0]["status"], "warning")
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

    def test_archive_survives_an_outage_after_ninety_days(self):
        previous = self.previous_edition(now=NOW - timedelta(days=92))
        self.assertEqual(len(previous["items"]), 1)
        edition = collector.collect([SOURCE], previous=previous, now=NOW, fetcher=self.offline_fetcher)
        self.assertEqual(len(edition["items"]), 1)
        self.assertEqual(edition["items"][0]["id"], previous["items"][0]["id"])
        self.assertEqual(edition["items"][0]["published_at"], previous["items"][0]["published_at"])
        self.assertNotEqual(edition["sources"][0]["status"], "ok")

    def test_full_archive_is_not_silently_truncated_to_a_thousand_records(self):
        rows = [raw_item(
            title=f"Research report {index}",
            url=f"https://research.example/reports/{index}",
            published_at=(NOW - timedelta(days=200)).isoformat(),
        ) for index in range(1103)]
        previous = collector.collect([SOURCE], now=NOW, fetcher=lambda _source: rows)
        self.assertEqual(len(previous["items"]), len(rows))
        retained = collector.collect(
            [SOURCE], previous=previous, now=NOW + timedelta(days=200), fetcher=self.offline_fetcher,
        )
        self.assertEqual({item["id"] for item in retained["items"]}, {item["id"] for item in previous["items"]})

    def test_one_failed_source_does_not_discard_its_history_or_other_sources_updates(self):
        other = {**SOURCE, "id": "other-lab", "name": "Other Lab", "url": "https://other.example/feed"}
        previous = collector.collect([SOURCE], now=NOW - timedelta(days=200), fetcher=lambda _source: [raw_item(
            published_at=(NOW - timedelta(days=201)).isoformat(),
        )])

        def fetch(source):
            if source["id"] == SOURCE["id"]:
                raise RuntimeError("temporary outage")
            return [raw_item(url="https://other.example/new-paper")]

        edition = collector.collect([SOURCE, other], previous=previous, now=NOW, fetcher=fetch)
        self.assertEqual(len(edition["items"]), 2)
        health = {row["id"]: row for row in edition["sources"]}
        self.assertEqual(health[SOURCE["id"]]["status"], "error")
        self.assertEqual(health[other["id"]]["status"], "ok")
        self.assertEqual(health[SOURCE["id"]]["last_success_at"], previous["sources"][0]["last_success_at"])
        self.assertEqual(collector.parse_datetime(edition["last_successful_update"]), NOW)

    def test_malformed_fetch_result_cannot_look_like_a_healthy_empty_feed(self):
        previous = self.previous_edition()
        for result in (None, "<html>upstream down</html>", {"unexpected": []}):
            with self.subTest(result=result):
                edition = collector.collect([SOURCE], previous=previous, now=NOW, fetcher=lambda _source: result)
                self.assertEqual(len(edition["items"]), 1)
                self.assertNotEqual(edition["sources"][0]["status"], "ok")
                self.assertEqual(edition["last_successful_update"], previous["last_successful_update"])

    def test_legacy_edition_migrates_without_losing_old_or_unmatched_research(self):
        # Version one had no first-seen timestamp, relevance label or provenance.
        legacy_item = {
            "id": "legacy-saved-id", "title": "The surprising result",
            "url": "https://research.example/legacy-paper", "source": SOURCE["name"],
            "source_url": SOURCE["url"], "published_at": "2022-01-01T00:00:00Z",
            "summary": "A technical report.", "summary_kind": "publisher excerpt", "score": 0, "topics": [],
        }
        previous = {"generated_at": "2026-10-01T12:00:00Z", "items": [legacy_item], "sources": []}
        edition = collector.collect([SOURCE], previous=previous, now=NOW, fetcher=self.offline_fetcher)
        self.assertEqual(len(edition["items"]), 1)
        item = edition["items"][0]
        self.assertEqual(item["url"], legacy_item["url"])
        self.assertEqual(item["published_at"], legacy_item["published_at"])
        self.assertTrue(item.get("provenance"))

    def test_missing_date_remains_unknown_across_refreshes(self):
        previous = collector.collect([SOURCE], now=NOW, fetcher=lambda _source: [raw_item(published_at=None)])
        edition = collector.collect(
            [SOURCE], previous=previous, now=NOW + timedelta(days=40), fetcher=self.offline_fetcher,
        )
        self.assertEqual(len(edition["items"]), 1)
        self.assertIsNone(edition["items"][0]["published_at"])
        self.assertEqual(edition["items"][0]["date_precision"], "unknown")

    def test_repeated_empty_responses_cannot_clear_a_source_coverage_warning(self):
        previous = self.previous_edition()
        for index in range(3):
            previous = collector.collect(
                [SOURCE], previous=previous, now=NOW + timedelta(hours=index), fetcher=lambda _source: [],
            )
            self.assertEqual(previous["sources"][0]["status"], "warning")
            self.assertEqual(len(previous["items"]), 1)

    def test_validated_sitemap_without_new_or_stale_documents_is_a_healthy_empty_delta(self):
        source = {**SOURCE, "kind": "sitemap", "feed_url": SOURCE["url"], "include_pattern": "/research/"}
        article_url = "https://research.example/research/known"
        previous = collector.collect([source], now=NOW - timedelta(hours=1), fetcher=lambda _source: [raw_item(url=article_url)])
        state = {"documents": {article_url: "2026-10-01"}, "checked_at": {article_url: collector.iso(NOW)}}
        sitemap = f'''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url>
          <loc>{article_url}</loc><lastmod>2026-10-01</lastmod></url></urlset>'''.encode()
        edition = collector.collect(
            [source], previous=previous, source_states={source["id"]: state}, now=NOW,
            fetcher=lambda _source: collector.fetch_source(_source, state, now=NOW, request=lambda _url: sitemap),
        )
        self.assertEqual(edition["sources"][0]["status"], "ok")
        self.assertEqual(len(edition["items"]), 1)
        self.assertEqual(edition["source_states"][source["id"]]["documents"], state["documents"])


class IdentityAndRevisionTests(unittest.TestCase):
    def test_shared_license_or_related_link_cannot_merge_distinct_papers(self):
        common = {"url": "https://creativecommons.org/licenses/by/4.0/", "rel": "license"}
        first = collector.normalize_item(raw_item(url="https://research.example/paper-a", links=[common]), SOURCE, NOW)
        second = collector.normalize_item(raw_item(url="https://research.example/paper-b", links=[common]), SOURCE, NOW)
        self.assertEqual(len(collector.deduplicate([first, second])), 2)

    def test_shared_doi_merges_publisher_and_arxiv_provenance(self):
        first = collector.normalize_item(raw_item(
            url="https://arxiv.org/abs/2601.12345v1", doi="10.1234/Alignment.2026",
        ), SOURCE, NOW - timedelta(days=1))
        other = {**SOURCE, "id": "publisher", "name": "Publisher", "url": "https://publisher.example/"}
        second = collector.normalize_item(raw_item(
            url="https://publisher.example/paper", doi="https://doi.org/10.1234/alignment.2026",
        ), other, NOW)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        merged = collector.deduplicate([first, second])
        self.assertEqual(len(merged), 1)
        item = merged[0]
        names = {record.get("name", record.get("source")) for record in item["provenance"]}
        self.assertTrue({SOURCE["name"], other["name"]}.issubset(names))
        self.assertEqual(collector.parse_datetime(item["first_seen"]), NOW - timedelta(days=1))
        self.assertEqual(collector.parse_datetime(item["last_seen"]), NOW)

    def test_correction_retains_identity_observation_dates_and_revision_history(self):
        first_time = NOW - timedelta(days=3)
        previous = collector.collect([SOURCE], now=first_time, fetcher=lambda _source: [raw_item(
            title="Original AI control result", published_at="2026-10-01T00:00:00Z",
            updated_at="2026-10-01T00:00:00Z", summary="Original oversight finding.",
        )])
        prior = previous["items"][0]
        edition = collector.collect([SOURCE], previous=previous, now=NOW, fetcher=lambda _source: [raw_item(
            title="Corrected AI control result", published_at="2026-10-01T00:00:00Z",
            updated_at="2026-10-15T00:00:00Z", summary="Corrected oversight finding with a limitation.",
        )])
        self.assertEqual(len(edition["items"]), 1)
        updated = edition["items"][0]
        self.assertEqual(updated["id"], prior["id"])
        self.assertEqual(updated["title"], "Corrected AI control result")
        self.assertEqual(updated["published_at"], prior["published_at"])
        self.assertEqual(updated["first_seen"], prior["first_seen"])
        self.assertEqual(collector.parse_datetime(updated["last_seen"]), NOW)
        self.assertNotEqual(updated["content_hash"], prior["content_hash"])
        self.assertTrue(any(revision.get("content_hash") == prior["content_hash"] for revision in updated["revisions"]))

    def test_no_change_does_not_append_a_new_revision_each_hour(self):
        previous = collector.collect([SOURCE], now=NOW, fetcher=lambda _source: [raw_item()])
        edition = collector.collect(
            [SOURCE], previous=previous, now=NOW + timedelta(hours=1), fetcher=lambda _source: [raw_item()],
        )
        self.assertEqual(edition["items"][0]["content_hash"], previous["items"][0]["content_hash"])
        self.assertEqual(edition["items"][0].get("revisions", []), previous["items"][0].get("revisions", []))

    def test_stale_arxiv_version_cannot_overwrite_a_newer_revision_due_to_input_order(self):
        original = collector.normalize_item(raw_item(
            url="https://arxiv.org/abs/2601.12345v1", title="Original AI control finding",
            published_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        ), SOURCE, NOW)
        revised = collector.normalize_item(raw_item(
            url="https://arxiv.org/abs/2601.12345v2", title="Corrected AI control finding",
            published_at="2026-01-01T00:00:00Z", updated_at="2026-10-15T00:00:00Z",
        ), SOURCE, NOW)
        for rows in ([original, revised], [revised, original]):
            with self.subTest(newer_first=rows[0] is revised):
                items = collector.deduplicate(rows)
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["title"], "Corrected AI control finding")
                self.assertEqual(items[0]["published_at"], "2026-01-01T00:00:00Z")


class SourceCheckpointTests(unittest.TestCase):
    def test_degraded_fallback_retains_research_and_api_checkpoint_without_claiming_full_source_health(self):
        checkpoint = {"backfill_year": 2025, "backfill_start": 20}
        edition = collector.collect([SOURCE], source_states={SOURCE["id"]: checkpoint}, now=NOW, fetcher=lambda _source: {
            "items": [raw_item(published_at=None)], "state": checkpoint,
            "coverage": {"mode": "rss_fallback", "degraded": True, "backfill_complete": False,
                         "pending_backfill": True, "primary_error": "search API timed out"},
            "warnings": ["Search unavailable; current RSS window only."],
        })
        self.assertEqual(edition["sources"][0]["status"], "degraded")
        self.assertEqual(edition["source_states"][SOURCE["id"]], checkpoint)
        self.assertEqual(len(edition["items"]), 1)
        self.assertIsNone(edition["items"][0]["published_at"])
        self.assertEqual(edition["stats"]["successful_sources"], 1)

    def test_success_commits_cursor_and_failure_keeps_last_committed_cursor(self):
        other = {**SOURCE, "id": "other", "name": "Other Lab"}
        states = {SOURCE["id"]: {"cursor": "page-3"}, other["id"]: {"cursor": "page-8"}}
        original = json.loads(json.dumps(states))

        def fetch(source):
            if source["id"] == SOURCE["id"]:
                return {
                    "items": [raw_item()], "state": {"cursor": "page-5"},
                    "coverage": {"pending_backfill": True, "backfill_complete": False}, "warnings": [],
                }
            raise TimeoutError("fixture: failed after requesting history page")

        edition = collector.collect([SOURCE, other], source_states=states, now=NOW, fetcher=fetch)
        self.assertEqual(edition["source_states"][SOURCE["id"]]["cursor"], "page-5")
        self.assertEqual(edition["source_states"][other["id"]]["cursor"], "page-8")
        self.assertEqual(states, original)

    def test_unusable_adapter_result_cannot_advance_a_checkpoint(self):
        states = {SOURCE["id"]: {"cursor": "page-3"}}
        edition = collector.collect([SOURCE], source_states=states, now=NOW, fetcher=lambda _source: {
            "items": "upstream maintenance", "state": {"cursor": "page-5"},
        })
        self.assertEqual(edition["source_states"][SOURCE["id"]]["cursor"], "page-3")
        self.assertEqual(edition["sources"][0]["status"], "error")

    def test_suspicious_empty_page_does_not_mark_historical_backfill_complete(self):
        previous = collector.collect([SOURCE], now=NOW - timedelta(days=1), fetcher=lambda _source: [raw_item(
            published_at="2026-10-01T00:00:00Z",
        )])
        states = {SOURCE["id"]: {"cursor": "page-3", "backfill_complete": False}}
        edition = collector.collect([SOURCE], previous=previous, source_states=states, now=NOW, fetcher=lambda _source: {
            "items": [], "state": {"cursor": None, "backfill_complete": True},
            "coverage": {"backfill_complete": True, "pending_backfill": False},
        })
        self.assertEqual(edition["sources"][0]["status"], "warning")
        self.assertEqual(edition["source_states"][SOURCE["id"]], states[SOURCE["id"]])
        self.assertEqual(len(edition["items"]), 1)

    def test_supported_empty_source_is_a_success_before_any_publications_are_known(self):
        edition = collector.collect([SOURCE], now=NOW, fetcher=lambda _source: [])
        self.assertEqual(edition["sources"][0]["status"], "ok")
        self.assertEqual(edition["items"], [])


class AtomicPersistenceTests(unittest.TestCase):
    def test_invalid_serialization_cannot_destroy_the_last_good_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "archive.json"
            original = {"items": [{"id": "retained"}]}
            collector.atomic_write(path, original)
            with self.assertRaises(ValueError):
                collector.atomic_write(path, {"items": [], "invalid": float("nan")})
            self.assertEqual(json.loads(path.read_text()), original)
            self.assertEqual([entry.name for entry in path.parent.iterdir()], ["archive.json"])

    def test_failed_atomic_replacement_leaves_the_last_good_archive_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "archive.json"
            original = {"items": [{"id": "retained"}]}
            collector.atomic_write(path, original)
            with patch.object(Path, "replace", side_effect=OSError("fixture: disk unavailable")):
                with self.assertRaises(OSError):
                    collector.atomic_write(path, {"items": []})
            self.assertEqual(json.loads(path.read_text()), original)

    def test_successful_write_replaces_complete_json_and_leaves_no_temporary_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "archive.json"
            collector.atomic_write(path, {"items": [{"id": "old"}]})
            replacement = {"items": [{"id": "new", "title": "Interpretability – new finding"}]}
            collector.atomic_write(path, replacement)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), replacement)
            self.assertEqual([entry.name for entry in path.parent.iterdir()], ["archive.json"])


class DurableArchiveTests(unittest.TestCase):
    def test_year_only_research_survives_retention_and_is_sharded_by_its_known_year(self):
        previous = collector.collect([SOURCE], now=NOW - timedelta(days=1), fetcher=lambda _source: [raw_item(
            published_at=None, publication_year=2004,
        )])
        archive = collector.collect([SOURCE], previous=previous, now=NOW,
                                    fetcher=CollectionHealthTests.offline_fetcher)
        self.assertEqual(len(archive["items"]), 1)
        item = archive["items"][0]
        self.assertEqual(item["publication_year"], 2004)
        self.assertIsNone(item["published_at"])
        self.assertEqual(archive["stats"]["year_only_items"], 1)
        with tempfile.TemporaryDirectory() as temporary:
            edition = collector.write_public_archive(archive, Path(temporary) / "data/edition.json")
            manifest = json.loads((Path(temporary) / edition["library_index"]["manifest"]).read_text())
            self.assertEqual([chunk["year"] for chunk in manifest["chunks"]], ["2004"])
            self.assertEqual(edition["items"][0]["publication_year"], 2004)
            self.assertIsNone(edition["items"][0]["published_at"])

    def test_corrupt_archive_is_an_error_instead_of_an_implicit_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "archive.json"
            path.write_text('{"items": [', encoding="utf-8")
            with self.assertRaises(ValueError):
                collector.load_archive(path, previous={"items": []})
            self.assertEqual(path.read_text(), '{"items": [')

    def test_future_archive_schema_is_not_overwritten_by_an_older_collector(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "archive.json"
            collector.atomic_write(path, {"schema_version": 999, "items": []})
            with self.assertRaises(ValueError):
                collector.load_archive(path)

    def test_public_manifest_covers_every_record_even_with_a_small_recent_edition(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "data" / "edition.json"
            archive = collector.collect([SOURCE], now=NOW, fetcher=lambda _source: [raw_item(
                url=f"https://research.example/archive/{index}", published_at=None if index == 5 else "2020-01-01",
            ) for index in range(6)])
            archive["items"][0]["private_raw_response"] = "must not be published"
            edition = collector.write_public_archive(archive, output, recent_limit=2, chunk_size=2)
            self.assertEqual(len(edition["items"]), 2)
            self.assertEqual(edition["library_index"]["total_items"], 6)
            self.assertFalse(edition["library_index"]["complete"])
            self.assertNotIn("source_states", edition)
            manifest_path = Path(temporary) / edition["library_index"]["manifest"]
            self.assertEqual(hashlib.sha256(manifest_path.read_bytes()).hexdigest(), edition["library_index"]["sha256"])
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["total_items"], 6)
            all_items = []
            for chunk in manifest["chunks"]:
                path = Path(temporary) / chunk["url"]
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), chunk["sha256"])
                rows = json.loads(path.read_text())["items"]
                self.assertLessEqual(len(rows), 2)
                self.assertEqual(chunk["count"], len(rows))
                self.assertTrue(all("private_raw_response" not in row and "revisions" not in row for row in rows))
                all_items.extend(rows)
            self.assertEqual({item["id"] for item in all_items}, {item["id"] for item in archive["items"]})
            self.assertTrue(any(chunk["year"] == "undated" for chunk in manifest["chunks"]))
            # A new generation must leave the old edition's manifest and chunks
            # readable, so browser caches cannot combine incompatible snapshots.
            old_files = {path: path.read_bytes() for path in output.parent.rglob("*.json")
                         if path != output and path.name not in {"manifest.json", "generations.json"}}
            archive["items"][0]["summary"] = "A corrected publisher excerpt."
            collector.write_public_archive(archive, output, recent_limit=2, chunk_size=2)
            for path, body in old_files.items():
                self.assertEqual(path.read_bytes(), body)

    def test_hourly_observation_timestamps_do_not_duplicate_unchanged_public_shards(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "data" / "edition.json"
            archive = collector.collect([SOURCE], now=NOW, fetcher=lambda _source: [raw_item()])
            first = collector.write_public_archive(archive, output)
            first_manifest = json.loads((Path(temporary) / first["library_index"]["manifest"]).read_text())
            archive["generated_at"] = collector.iso(NOW + timedelta(hours=1))
            archive["items"][0]["last_seen"] = archive["generated_at"]
            for provenance in archive["items"][0]["provenance"]:
                provenance["last_seen"] = archive["generated_at"]
            second = collector.write_public_archive(archive, output)
            second_manifest = json.loads((Path(temporary) / second["library_index"]["manifest"]).read_text())
            self.assertEqual(first_manifest["chunks"], second_manifest["chunks"])
            self.assertNotIn("last_seen", second["items"][0])


class CommandLineArchiveTests(unittest.TestCase):
    def test_default_and_relative_site_outputs_keep_the_master_archive_outside_the_served_site(self):
        for output_args in ([], ["--output", "site/data/edition.json"],
                            ["--output", "site/../site/data/edition.json"]):
            with self.subTest(output_args=output_args), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                config = root / "config" / "sources.json"
                config.parent.mkdir(parents=True)
                config.write_text(json.dumps({"sources": [SOURCE]}), encoding="utf-8")
                result = {
                    "items": [raw_item()], "state": {"backfill_page": 3, "private_checkpoint": "history-page-3"},
                    "coverage": {"pending_backfill": True, "backfill_complete": False}, "warnings": [],
                }
                with patch.object(collector, "ROOT", root), patch.object(collector, "fetch_source", return_value=result), \
                        chdir(root), redirect_stdout(io.StringIO()):
                    status = collector.main(["--now", collector.iso(NOW), *output_args])
                self.assertEqual(status, 0)
                master = root / "data" / "archive.json"
                self.assertTrue(master.is_file())
                manifest = json.loads(master.read_text())
                self.assertEqual(manifest["storage_format"], "sharded-v1")
                self.assertNotIn("items", manifest)
                archive = collector.load_archive(master)
                self.assertEqual(archive["source_states"][SOURCE["id"]]["backfill_page"], 3)
                self.assertFalse((root / "site/data/collector-archive.json").exists())
                self.assertTrue((root / "site/data/edition.json").is_file())
                for public_path in (root / "site").rglob("*.json"):
                    public = json.loads(public_path.read_text())
                    self.assertNotIn("source_states", public, str(public_path))
                    self.assertNotIn("private_checkpoint", public_path.read_text(), str(public_path))


class CanonicalShardTests(unittest.TestCase):
    @staticmethod
    def archive(count=3, cursor="history-page-3"):
        result = collector.collect([SOURCE], now=NOW, fetcher=lambda _source: [raw_item(
            url=f"https://research.example/durable/{index}",
            title=f"AI control research {index}",
            published_at=None if index % 3 else "2010-01-01T00:00:00Z",
        ) for index in range(count)])
        result["source_states"] = {SOURCE["id"]: {"cursor": cursor, "nested": {"years": [2010, 2026]}}}
        result["pipeline_metadata"] = {"stage": "collection", "extra": [None, True, "λ"]}
        for index, item in enumerate(result["items"]):
            item["private_metadata"] = {"nested": [index, {"note": "Preserve every field"}]}
            item["revisions"] = [{"content_hash": f"historical-content-{index}", "observed_at": "2026-10-01T00:00:00Z"}]
        return result

    def test_more_than_a_thousand_records_and_all_metadata_round_trip_through_bounded_shards(self):
        archive = self.archive(count=1103)
        original = deepcopy(archive)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "data/archive.json"
            master = collector.save_archive(path, archive)
            self.assertEqual(archive, original, "Saving must not strip or mutate caller metadata")
            self.assertEqual(master["storage_format"], "sharded-v1")
            self.assertNotIn("items", master)
            self.assertEqual(master["items_count"], 1103)
            self.assertEqual([chunk["count"] for chunk in master["storage_chunks"]], [500, 500, 103])
            self.assertEqual(master["source_states"], original["source_states"])
            for reference in master["storage_chunks"]:
                body = (path.parent / reference["path"]).read_bytes()
                self.assertLessEqual(len(body), 8 * 1024 * 1024)
                self.assertEqual(hashlib.sha256(body).hexdigest(), reference["sha256"])
                self.assertEqual(len(json.loads(body)["items"]), reference["count"])
            self.assertEqual(collector.load_archive(path), original)

    def test_corrupt_or_missing_shards_refuse_loading_instead_of_resetting_history(self):
        for failure in ("corrupt", "missing"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "archive.json"
                master = collector.save_archive(path, self.archive())
                original_master = path.read_bytes()
                chunk = path.parent / master["storage_chunks"][0]["path"]
                if failure == "corrupt":
                    chunk.write_bytes(chunk.read_bytes() + b" ")
                else:
                    chunk.unlink()
                with self.assertRaises((ValueError, OSError)):
                    collector.load_archive(path, previous={"items": [], "source_states": {}})
                self.assertEqual(path.read_bytes(), original_master)

    def test_failed_master_replacement_preserves_the_previous_records_and_checkpoint_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "archive.json"
            previous = self.archive(cursor="before-refresh")
            collector.save_archive(path, previous)
            original_master = path.read_bytes()
            updated = self.archive(count=4, cursor="after-refresh")
            original_replace = Path.replace

            def replace(source, target):
                if Path(target) == path:
                    raise OSError("fixture: authoritative master replacement failed")
                return original_replace(source, target)

            with patch.object(Path, "replace", autospec=True, side_effect=replace):
                with self.assertRaises(OSError):
                    collector.save_archive(path, updated)
            self.assertEqual(path.read_bytes(), original_master)
            self.assertEqual(collector.load_archive(path), previous)
            self.assertEqual(list(path.parent.glob(".archive-*.tmp")), [])


class ArchiveProcessLockTests(unittest.TestCase):
    child_script = '''import sys
sys.path.insert(0, sys.argv[3])
from collect import archive_lock, ArchiveBusyError
try:
    with archive_lock(sys.argv[1], lock_dir=sys.argv[2]):
        print("acquired")
except ArchiveBusyError:
    print("busy")
    sys.exit(17)
'''

    def child(self, path, lock_dir):
        return subprocess.run(
            [sys.executable, "-c", self.child_script, str(path), str(lock_dir), str(ROOT / "scripts")],
            capture_output=True, text=True, timeout=10, check=False,
        )

    def test_another_process_cannot_lock_the_same_archive_until_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "archive.json"
            lock_dir = Path(temporary) / "locks"
            with collector.archive_lock(path, lock_dir=lock_dir):
                blocked = self.child(path, lock_dir)
                self.assertEqual(blocked.returncode, 17, blocked.stderr)
                self.assertEqual(blocked.stdout.strip(), "busy")
                independent = self.child(Path(temporary) / "other-archive.json", lock_dir)
                self.assertEqual(independent.returncode, 0, independent.stderr)
            released = self.child(path, lock_dir)
            self.assertEqual(released.returncode, 0, released.stderr)
            self.assertEqual(released.stdout.strip(), "acquired")
            self.assertEqual(len(list(lock_dir.glob("*.lock"))), 2, "Stable lock files remain reusable after release")

    def test_busy_cli_returns_without_fetching_or_changing_archive_and_public_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = root / "config/sources.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"sources": [SOURCE]}), encoding="utf-8")
            path = root / "data/archive.json"
            previous = CanonicalShardTests.archive()
            collector.save_archive(path, previous)
            collector.write_public_archive(previous, root / "site/data/edition.json")
            before = {entry.relative_to(root): entry.read_bytes()
                      for parent in (root / "data", root / "site") for entry in parent.rglob("*.json")}
            with patch.object(collector, "ROOT", root), collector.archive_lock(path), \
                    patch.object(collector, "fetch_source", side_effect=AssertionError("Busy collector must not fetch")), \
                    chdir(root), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                status = collector.main(["--now", collector.iso(NOW), "--output", "site/data/edition.json"])
            self.assertEqual(status, 3)
            after = {entry.relative_to(root): entry.read_bytes()
                     for parent in (root / "data", root / "site") for entry in parent.rglob("*.json")}
            self.assertEqual(after, before)
            self.assertEqual(collector.load_archive(path), previous)


class FakeResponse:
    def __init__(self, body=b"fixture body", headers=None, url="https://research.example/feed"):
        self.body, self.headers, self.url = body, headers or {}, url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, count):
        return self.body[:count]


class FakeOpener:
    def __init__(self, *outcomes):
        self.outcomes, self.requests = list(outcomes), []

    def open(self, request, timeout=None):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class NetworkResilienceTests(unittest.TestCase):
    url = "https://research.example/feed"

    def test_gzip_is_decoded_before_caching_and_a_later_304_reuses_decoded_bytes(self):
        body = b'<rss><channel><title>Publisher research</title></channel></rss>'
        with tempfile.TemporaryDirectory() as temporary:
            first_opener = FakeOpener(FakeResponse(gzip.compress(body), {"Content-Encoding": "gzip", "ETag": '"gzip-one"'}))
            first = collector.CachedNetwork(temporary, opener=first_opener, sleep=lambda _: None)
            self.assertEqual(first(self.url), body)
            first.save()
            cached_bodies = list(Path(temporary).glob("*.body"))
            self.assertEqual(len(cached_bodies), 1)
            self.assertEqual(cached_bodies[0].read_bytes(), body)
            metadata = next(iter(json.loads((Path(temporary) / "index.json").read_text()).values()))
            self.assertEqual(metadata["cache_format"], 2)
            self.assertEqual(metadata["body_sha256"], hashlib.sha256(body).hexdigest())
            request_headers = {key.lower(): value for key, value in first_opener.requests[0].header_items()}
            self.assertEqual(request_headers["accept-encoding"], "identity")
            not_modified = HTTPError(self.url, 304, "not modified", {}, io.BytesIO())
            self.addCleanup(not_modified.close)
            second_opener = FakeOpener(not_modified)
            second = collector.CachedNetwork(temporary, opener=second_opener, sleep=lambda _: None)
            self.assertEqual(second(self.url), body)
            headers = {key.lower(): value for key, value in second_opener.requests[0].header_items()}
            self.assertEqual(headers["if-none-match"], '"gzip-one"')

    def test_gzip_expansion_is_bounded_and_cannot_replace_a_good_cached_response(self):
        compressed = gzip.compress(b"x" * 10000)
        self.assertLess(len(compressed), 80)
        with tempfile.TemporaryDirectory() as temporary:
            opener = FakeOpener(FakeResponse(b"good"), FakeResponse(compressed, {"Content-Encoding": "gzip"}))
            delays = []
            network = collector.CachedNetwork(temporary, opener=opener, sleep=delays.append)
            with patch.object(collector, "MAX_RESPONSE_BYTES", 80):
                self.assertEqual(network(self.url), b"good")
                with self.assertRaises(ValueError):
                    network(self.url)
            self.assertEqual(delays, [])
            self.assertEqual(len(opener.requests), 2)
            self.assertEqual(next(Path(temporary).glob("*.body")).read_bytes(), b"good")

    def test_transient_timeouts_retry_a_bounded_number_of_times(self):
        opener = FakeOpener(*(TimeoutError("fixture timeout") for _ in range(3)))
        delays = []
        network = collector.CachedNetwork(retries=3, opener=opener, sleep=delays.append)
        with self.assertRaises(TimeoutError):
            network(self.url)
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(delays, [1, 2])

    def test_rate_limit_honors_a_bounded_retry_after_and_then_succeeds(self):
        failure = HTTPError(self.url, 429, "rate limited", {"Retry-After": "120"}, io.BytesIO())
        self.addCleanup(failure.close)
        opener = FakeOpener(
            failure,
            FakeResponse(b"recovered"),
        )
        delays = []
        network = collector.CachedNetwork(opener=opener, sleep=delays.append)
        self.assertEqual(network(self.url), b"recovered")
        self.assertEqual(delays, [15])

    def test_permanent_http_error_does_not_retry(self):
        failure = HTTPError(self.url, 404, "not found", {}, io.BytesIO())
        self.addCleanup(failure.close)
        opener = FakeOpener(failure)
        delays = []
        network = collector.CachedNetwork(opener=opener, sleep=delays.append)
        with self.assertRaises(HTTPError):
            network(self.url)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(delays, [])

    def test_not_modified_reuses_the_corresponding_persisted_body_and_validators(self):
        with tempfile.TemporaryDirectory() as temporary:
            opener = FakeOpener(FakeResponse(b"cached publisher response", {
                "ETag": '"version-one"', "Last-Modified": "Wed, 14 Oct 2026 12:00:00 GMT",
            }))
            first = collector.CachedNetwork(temporary, opener=opener, sleep=lambda _: None)
            self.assertEqual(first(self.url), b"cached publisher response")
            first.save()
            not_modified = HTTPError(self.url, 304, "not modified", {}, io.BytesIO())
            self.addCleanup(not_modified.close)
            opener = FakeOpener(not_modified)
            second = collector.CachedNetwork(temporary, opener=opener, sleep=lambda _: None)
            self.assertEqual(second(self.url), b"cached publisher response")
            headers = {key.lower(): value for key, value in opener.requests[0].header_items()}
            self.assertEqual(headers["if-none-match"], '"version-one"')
            self.assertEqual(headers["if-modified-since"], "Wed, 14 Oct 2026 12:00:00 GMT")

    def test_oversize_response_does_not_retry_or_replace_good_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            opener = FakeOpener(FakeResponse(b"good"), FakeResponse(b"too much data"))
            delays = []
            network = collector.CachedNetwork(temporary, opener=opener, sleep=delays.append)
            with patch.object(collector, "MAX_RESPONSE_BYTES", 4):
                self.assertEqual(network(self.url), b"good")
                with self.assertRaises(ValueError):
                    network(self.url)
            self.assertEqual(len(opener.requests), 2)
            self.assertEqual(delays, [])
            bodies = list(Path(temporary).glob("*.body"))
            self.assertEqual(len(bodies), 1)
            self.assertEqual(bodies[0].read_bytes(), b"good")

if __name__ == "__main__":
    unittest.main()
