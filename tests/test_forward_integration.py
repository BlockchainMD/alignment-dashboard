"""Offline checks for switching a populated archive to forward collection.

These exercise the CLI, real RSS parsing, durable storage, public library, browser
shortlist and video packet export together. No publisher requests are made.
"""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape

from scripts import collect as collector
from scripts import export_video_jobs


ROOT = Path(__file__).resolve().parents[1]
CUTOFF = "2026-10-16T23:48:48Z"
NOW = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)
BEFORE = datetime(2026, 10, 16, 22, 0, tzinfo=timezone.utc)
POLICY = {"mode": "forward_only", "publication_start": CUTOFF}
SOURCE = {
    "id": "integration-research",
    "name": "Integration Research",
    "url": "https://research.example/",
    "feed_url": "https://research.example/feed.xml",
    "kind": "rss",
    "scope": "mixed",
    "summary_kind": "publisher excerpt",
}


def research(slug, published_at):
    return {
        "title": f"Evaluating AI control against scheming agents: {slug}",
        "url": f"https://research.example/{slug}",
        "published_at": published_at,
        "summary": "We introduce an evaluation method and report empirical experiments on language model monitoring and scalable oversight.",
    }


def rss(rows):
    entries = []
    for row in rows:
        fields = [("title", row["title"]), ("link", row["url"]),
                  ("description", row["summary"])]
        if row.get("published_at"):
            fields.append(("pubDate", row["published_at"]))
        entries.append("<item>" + "".join(
            f"<{tag}>{escape(value)}</{tag}>" for tag, value in fields
        ) + "</item>")
    return ("<rss version='2.0'><channel>" + "".join(entries) + "</channel></rss>").encode()


class RssTransport:
    def __init__(self, rows):
        self.body = rss(rows)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if url != SOURCE["feed_url"]:
            raise AssertionError("Unexpected request outside the current RSS feed: " + url)
        return self.body

    def save(self):
        pass


class PublisherTransport:
    def __init__(self, bodies):
        self.bodies = bodies
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if url not in self.bodies:
            raise AssertionError("Historical publisher page must not be requested: " + url)
        return self.bodies[url]

    def save(self):
        pass


class ForwardCollectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "config.json"
        self.master = self.root / "data/archive.json"
        self.output = self.root / "site/data/edition.json"
        self.config.write_text(json.dumps({"coverage_policy": POLICY, "sources": [SOURCE]}))
        self.legacy = collector.collect([SOURCE], now=BEFORE, fetcher=lambda _source: [
            research("legacy-dated", "2020-02-03T10:00:00Z"),
            research("legacy-undated", None),
            research("legacy-cutoff-day", "2026-10-16"),
        ])
        self.legacy_ids = {row["id"] for row in self.legacy["items"]}
        collector.save_archive(self.master, self.legacy)
        # This catches a transition that leaves the old active manifest in place.
        collector.write_public_archive(self.legacy, self.output)

    def run_collection(self, rows, now=NOW, transport=None):
        transport = transport or RssTransport(rows)
        with patch.object(collector, "CachedNetwork", return_value=transport), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = collector.main([
                "--config", str(self.config), "--archive", str(self.master),
                "--output", str(self.output), "--cache-dir", str(self.root / "cache"),
                "--now", collector.iso(now),
            ])
        self.assertEqual(status, 0)
        if isinstance(transport, RssTransport):
            self.assertEqual(transport.urls, [SOURCE["feed_url"]])
        return collector.load_archive(self.master), json.loads(self.output.read_text())

    def public_library(self, edition):
        manifest_path = self.root / "site" / edition["library_index"]["manifest"]
        manifest = json.loads(manifest_path.read_text())
        rows = []
        for reference in manifest["chunks"]:
            shard = json.loads((self.root / "site" / reference["url"]).read_text())
            self.assertEqual(len(shard["items"]), reference["count"])
            rows.extend(shard["items"])
        self.assertEqual(manifest["total_items"], len(rows))
        self.assertEqual(edition["library_index"]["total_items"], len(rows))
        return rows

    def test_forward_transition_preserves_history_but_changes_all_active_consumers(self):
        rows = [
            research("legacy-dated", "2020-02-03T10:00:00Z"),
            research("legacy-undated", None),
            research("legacy-cutoff-day", "2026-10-16"),
            research("new-instant", "2026-10-17T02:00:00Z"),
            research("cutoff-day-uncertain", "2026-10-16"),
            research("new-undated", None),
            research("new-day", "2026-10-17"),
            research("old-day", "2026-10-15"),
            {
                "title": "AI alignment news roundup and opinion",
                "url": "https://research.example/news",
                "published_at": "2026-10-17T03:00:00Z",
                "summary": "A newsletter of policy news, commentary and opinions about AI alignment.",
            },
        ]
        archive, edition = self.run_collection(rows)
        public = self.public_library(edition)
        by_slug = {row["url"].rsplit("/", 1)[-1]: row for row in public}
        self.assertEqual(set(by_slug), {
            "new-instant", "cutoff-day-uncertain", "new-undated", "new-day", "news",
        })
        public_ids = {row["id"] for row in public}
        self.assertTrue(self.legacy_ids.isdisjoint(public_ids))
        self.assertEqual({row["id"] for row in edition["items"]}, public_ids)
        canonical_ids = {row["id"] for row in archive["items"]}
        self.assertTrue(self.legacy_ids <= canonical_ids)
        self.assertEqual(len(canonical_ids), 8)
        for old in self.legacy["items"]:
            retained = next(row for row in archive["items"] if row["id"] == old["id"])
            self.assertEqual(retained["first_seen"], old["first_seen"])
            self.assertEqual(retained["published_at"], old["published_at"])
        self.assertEqual(edition["stats"]["total_items"], 5)
        self.assertEqual(edition["stats"]["lifetime_total_items"], 8)
        self.assertEqual(edition["stats"]["relevant_items"], 2)
        self.assertEqual(edition["stats"]["candidate_items"], 3)
        for slug in ("cutoff-day-uncertain", "new-undated", "news"):
            self.assertEqual(by_slug[slug]["relevance"], "candidate", slug)
        self.assertEqual(by_slug["cutoff-day-uncertain"]["temporal_status"], "publication_time_uncertain")
        self.assertEqual(by_slug["new-undated"]["temporal_status"], "publication_date_unverified")
        coverage = edition["sources"][0]["coverage"]
        self.assertEqual(coverage["monitoring_since"], CUTOFF)
        self.assertFalse(coverage.get("pending_backfill"))
        self.assertEqual(edition["coverage"]["pending_backfill_sources"], 0)

        packets_path = self.root / "video-jobs.jsonl"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(export_video_jobs.main([
                "--archive", str(self.master), "--output", str(packets_path),
            ]), 0)
        packets = [json.loads(line) for line in packets_path.read_text().splitlines()]
        self.assertEqual({packet["article_id"] for packet in packets}, public_ids)
        self.assertTrue(all(packet["video_brief"]["maximum_duration_seconds"] <= 120 for packet in packets))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            export_video_jobs.main([
                "--archive", str(self.master), "--output", str(self.root / "legacy-video.jsonl"),
                "--article-id", next(iter(self.legacy_ids)),
            ])
        self.assertFalse((self.root / "legacy-video.jsonl").exists())

        node = shutil.which("node")
        if node:
            browser = subprocess.run([
                node, "--input-type=module", "-e",
                "import {readFileSync} from 'node:fs'; "
                "import {normalizeEdition,partitionBriefing} from './site/lib.mjs'; "
                "const e=normalizeEdition(JSON.parse(readFileSync(process.argv[1],'utf8'))); "
                "const b=partitionBriefing(e.items,e.generated_at); "
                "console.log(JSON.stringify([...b.top,...b.catchup].map(i=>i.id)));",
                str(self.output),
            ], cwd=ROOT, capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(browser.returncode, 0, browser.stderr)
            self.assertEqual(set(json.loads(browser.stdout)), {
                by_slug["new-instant"]["id"], by_slug["new-day"]["id"],
            })

        # An hourly rediscovery must not give an old undated item a new start date.
        again, second = self.run_collection(rows, now=datetime(2026, 10, 18, 1, tzinfo=timezone.utc))
        self.assertEqual({row["id"] for row in self.public_library(second)}, public_ids)
        self.assertEqual(len(again["items"]), len(archive["items"]))

    def test_failed_initial_poll_still_replaces_legacy_public_view_and_backfill_labels(self):
        with patch.object(collector, "fetch_source", side_effect=OSError("fixture publisher unavailable")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = collector.main([
                "--config", str(self.config), "--archive", str(self.master),
                "--output", str(self.output), "--cache-dir", str(self.root / "cache"),
                "--now", collector.iso(NOW),
            ])
        self.assertEqual(status, 1)
        edition = json.loads(self.output.read_text())
        self.assertEqual(self.public_library(edition), [])
        self.assertEqual(edition["stats"]["total_items"], 0)
        self.assertEqual(edition["stats"]["lifetime_total_items"], len(self.legacy_ids))
        self.assertEqual(edition["coverage"]["pending_backfill_sources"], 0)
        source = edition["sources"][0]
        self.assertEqual(source["status"], "error")
        self.assertEqual(source["coverage"]["monitoring_since"], CUTOFF)
        self.assertFalse(source["coverage"].get("pending_backfill"))
        self.assertEqual({row["id"] for row in collector.load_archive(self.master)["items"]}, self.legacy_ids)

    def test_successful_poll_with_only_old_feed_entries_is_a_healthy_empty_window(self):
        archive, edition = self.run_collection([
            research("legacy-dated", "2020-02-03T10:00:00Z"),
            research("legacy-undated", None),
        ])
        self.assertEqual(self.public_library(edition), [])
        health = edition["sources"][0]
        self.assertEqual(health["status"], "ok")
        self.assertFalse(health["coverage"].get("suspicious_empty", False))
        self.assertEqual(health["coverage"]["monitoring_since"], CUTOFF)
        self.assertIn("forward_state", archive["source_states"][SOURCE["id"]])

    def test_large_undated_publisher_index_is_baselined_then_new_links_enter_review(self):
        publisher = {**SOURCE, "id": "publisher", "kind": "sitemap",
                     "feed_url": "https://research.example/sitemap.xml"}
        self.config.write_text(json.dumps({"coverage_policy": POLICY, "sources": [publisher]}))
        old_urls = [f"https://research.example/old-index-{number}" for number in range(80)]

        def sitemap(urls):
            return ("<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>" + "".join(
                f"<url><loc>{url}</loc></url>" for url in urls
            ) + "</urlset>").encode()

        first_transport = PublisherTransport({publisher["feed_url"]: sitemap(old_urls)})
        _, first = self.run_collection([], transport=first_transport)
        self.assertEqual(first_transport.urls, [publisher["feed_url"]])
        self.assertEqual(self.public_library(first), [])
        health = next(row for row in first["sources"] if row["id"] == publisher["id"])
        self.assertTrue(any("baselin" in warning.casefold() for warning in health["warnings"]))
        self.assertFalse(health["coverage"].get("pending_backfill"))

        new_url = "https://research.example/publisher-new"
        second_transport = PublisherTransport({
            publisher["feed_url"]: sitemap([new_url, *old_urls]),
            new_url: b'<html><head><title>Evaluating AI control against scheming agents</title>'
                     b'<meta name="description" content="We report empirical experiments on language model monitoring and scalable oversight."></head></html>',
        })
        archive, second = self.run_collection([], now=datetime(2026, 10, 18, 1, tzinfo=timezone.utc), transport=second_transport)
        self.assertEqual(second_transport.urls, [publisher["feed_url"], new_url])
        active = self.public_library(second)
        self.assertEqual([row["url"] for row in active], [new_url])
        self.assertEqual(active[0]["relevance"], "candidate")
        self.assertEqual(active[0]["temporal_status"], "publication_date_unverified")
        self.assertEqual(len(archive["items"]), len(self.legacy_ids) + 1)


if __name__ == "__main__":
    unittest.main()
