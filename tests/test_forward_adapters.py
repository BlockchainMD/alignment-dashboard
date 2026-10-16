"""Forward monitoring never resumes an archive cursor or floods old indexes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from scripts.source_adapters import fetch


START = "2026-10-16T23:48:48Z"
NOW = datetime(2026, 10, 17, 3, tzinfo=timezone.utc)
BASE = {"forward_only": True, "publication_start": START,
        "url": "https://research.example/", "feed_url": "https://research.example/feed",
        "page_size": 2, "catchup_pages": 1, "article_budget": 1,
        "backfill_pages": 0}


def article(title="A new paper", date="2026-10-17T00:00:00Z"):
    return (f'<html><head><meta name="citation_title" content="{title}">'
            f'<meta name="citation_publication_date" content="{date}"></head></html>').encode()


def sitemap(entries):
    body = "".join(f'<url><loc>{url}</loc>{"<lastmod>" + date + "</lastmod>" if date else ""}</url>'
                   for url, date in entries)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'.encode()


class ForwardAdapterTests(unittest.TestCase):
    def test_arxiv_queries_activation_window_and_resumes_new_burst_only(self):
        source = {**BASE, "kind": "arxiv", "search_query": 'all:"AI alignment"', "request_interval_seconds": 0}
        old = {"backfill_year": 2020, "backfill_start": 100, "incremental_start": 80}
        requests = []

        def request(url):
            q = parse_qs(urlsplit(url).query)
            requests.append(q)
            start = int(q["start"][0])
            count = min(2, 5 - start)
            rows = "".join(f'<entry><id>https://arxiv.org/abs/2610.{i:05d}</id><title>Alignment {i}</title>'
                           '<published>2026-10-17T00:00:00Z</published><updated>2026-10-17T00:00:00Z</updated></entry>'
                           for i in range(start, start + count))
            return (f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/">'
                    f'<o:totalResults>5</o:totalResults><o:startIndex>{start}</o:startIndex>{rows}</feed>').encode()

        first = fetch(source, old, NOW, request)
        second = fetch(source, first["state"], NOW + timedelta(hours=1), request)
        third = fetch(source, second["state"], NOW + timedelta(hours=2), request)
        self.assertEqual([int(q["start"][0]) for q in requests], [0, 2, 4])
        self.assertTrue(all(q["sortBy"] == ["submittedDate"] for q in requests))
        self.assertTrue(all("submittedDate:[202610162348 TO 202610170300]" in q["search_query"][0] for q in requests))
        self.assertEqual(third["state"]["backfill_year"], 2020)
        self.assertEqual(old, {"backfill_year": 2020, "backfill_start": 100, "incremental_start": 80})
        self.assertTrue(first["coverage"]["pending_incremental"])
        self.assertFalse(third["coverage"]["pending_incremental"])
        self.assertFalse(third["coverage"]["pending_backfill"])

    def test_forum_starts_at_cutoff_and_ignores_old_backfill_cursor(self):
        requests = []
        old = {"backfill_before": "2020-01-01T00:00:00Z", "watermark": "2019-01-01T00:00:00Z"}
        def request(url):
            requests.append(parse_qs(urlsplit(url).query)["query"][0])
            return b'{"data":{"posts":{"results":[]}}}'
        result = fetch({**BASE, "kind": "forum_graphql"}, old, NOW, request)
        self.assertEqual(len(requests), 1)
        self.assertIn('after:"2026-10-16T23:48:48.000Z"', requests[0])
        self.assertNotIn("2020", requests[0])
        self.assertEqual(result["state"]["backfill_before"], old["backfill_before"])
        self.assertFalse(result["coverage"]["pending_incremental"])

    def test_offset_initial_new_burst_is_resumable_without_old_history_cursor(self):
        entries = [{"title": f"Paper {i}", "link": f"https://research.example/{i}",
                    "published_at": "2026-10-17T00:00:00Z" if i < 7 else "2020-01-01T00:00:00Z"}
                   for i in range(10)]
        requests = []
        def request(url):
            page = int(parse_qs(urlsplit(url).query)["page"][0]); requests.append(page)
            return json.dumps({"data": entries[(page - 1) * 2:page * 2]}).encode()
        source = {**BASE, "kind": "alignment_feed"}
        old = {"backfill_page": 900, "head_id": "old-history-head"}
        state, collected = old, set()
        for _ in range(3):
            result = fetch(source, state, NOW, request)
            state = result["state"]
            collected.update(row["url"] for row in result["items"])
        self.assertEqual(collected, {f"https://research.example/{i}" for i in range(7)})
        self.assertEqual(requests, [1, 2, 1, 3, 1, 4])
        self.assertEqual(state["backfill_page"], 900)
        self.assertFalse(result["coverage"]["pending_incremental"])
        self.assertFalse(result["coverage"]["pending_backfill"])

    def test_offset_restarts_recent_segment_when_head_changes_during_pending_scan(self):
        entries = [{"title": str(i), "link": f"https://research.example/{i}",
                    "published_at": "2026-10-17T00:00:00Z" if i < 8 else "2020-01-01T00:00:00Z"}
                   for i in range(10)]
        def request(url):
            p = int(parse_qs(urlsplit(url).query)["page"][0])
            return json.dumps({"data": entries[(p - 1) * 2:p * 2]}).encode()
        source = {**BASE, "kind": "alignment_feed"}
        result = fetch(source, {}, NOW, request)
        observed = {row["url"] for row in result["items"]}
        entries[:0] = [{"title": f"Burst {i}", "link": f"https://research.example/burst-{i}", "published_at": "2026-10-17T01:00:00Z"} for i in range(6)]
        for _ in range(10):
            result = fetch(source, result["state"], NOW, request)
            observed.update(row["url"] for row in result["items"])
            if not result["coverage"]["pending_incremental"]:
                break
        self.assertEqual(observed, {row["link"] for row in entries if row["published_at"] != "2020-01-01T00:00:00Z"})

    def test_offset_overlap_discovers_late_indexed_publication_below_unchanged_head(self):
        entries = [{"title": str(i), "link": f"https://research.example/{i}",
                    "published_at": "2026-10-17T00:00:00Z" if i < 3 else "2020-01-01T00:00:00Z"}
                   for i in range(4)]
        def request(url):
            p = int(parse_qs(urlsplit(url).query)["page"][0])
            return json.dumps({"data": entries[(p - 1) * 2:p * 2]}).encode()
        source = {**BASE, "kind": "alignment_feed"}
        first = fetch(source, {}, NOW, request)
        self.assertFalse(first["coverage"]["pending_incremental"])
        entries.insert(3, {"title": "Late indexed", "link": "https://research.example/late",
                           "published_at": "2026-10-16T23:55:00Z"})
        second = fetch(source, first["state"], NOW + timedelta(hours=1), request)
        self.assertEqual(first["items"][0]["url"], second["items"][0]["url"])
        self.assertIn("https://research.example/late", {row["url"] for row in second["items"]})
        self.assertTrue(second["coverage"]["pending_incremental"])
        third = fetch(source, second["state"], NOW + timedelta(hours=2), request)
        self.assertFalse(third["coverage"]["pending_incremental"])

    def test_offset_tail_advances_during_steady_head_arrivals(self):
        entries = [{"title": str(i), "link": f"https://research.example/{i}",
                    "published_at": "2026-10-17T00:00:00Z" if i < 8 else "2020-01-01T00:00:00Z"}
                   for i in range(9)]
        pages = []
        def request(url):
            p = int(parse_qs(urlsplit(url).query)["page"][0]); pages.append(p)
            return json.dumps({"data": entries[(p - 1) * 2:p * 2]}).encode()
        source = {**BASE, "kind": "alignment_feed"}
        result = fetch(source, {}, NOW, request)
        observed = {row["url"] for row in result["items"]}
        for i in range(6):
            entries.insert(0, {"title": f"New {i}", "link": f"https://research.example/new-{i}",
                               "published_at": "2026-10-17T01:00:00Z"})
            result = fetch(source, result["state"], NOW, request)
            observed.update(row["url"] for row in result["items"])
        self.assertTrue({f"https://research.example/{i}" for i in range(8)} <= observed)
        self.assertIn(6, pages)

    def test_offset_unknown_dates_do_not_trigger_a_historical_sweep(self):
        requested = []
        def request(url):
            p = int(parse_qs(urlsplit(url).query)["page"][0]); requested.append(p)
            return json.dumps({"data": [{"title": f"Unknown {i}", "link": f"https://research.example/{i}"}
                                        for i in range((p - 1) * 2, p * 2)]}).encode()
        source = {**BASE, "kind": "alignment_feed"}
        result = fetch(source, {}, NOW, request)
        second = fetch(source, result["state"], NOW, request)
        self.assertEqual(requested, [1, 1])
        self.assertTrue(second["coverage"]["degraded"])
        self.assertTrue(second["coverage"]["pending_incremental"])
        self.assertEqual(len(second["items"]), 2, "Current undated rows remain temporal-review candidates")

    def test_offset_anchor_probe_moves_tail_during_three_arrivals_per_poll(self):
        entries = [{"title": str(i), "link": f"https://research.example/{i}",
                    "published_at": "2026-10-17T00:00:00Z" if i < 8 else "2020-01-01T00:00:00Z"}
                   for i in range(9)]
        requested = []
        def request(url):
            p = int(parse_qs(urlsplit(url).query)["page"][0]); requested.append(p)
            return json.dumps({"data": entries[(p - 1) * 2:p * 2]}).encode()
        source = {**BASE, "kind": "alignment_feed"}
        result = fetch(source, {}, NOW, request)
        observed = {row["url"] for row in result["items"]}
        for i in range(6):
            entries[:0] = [{"title": f"New {i}-{j}", "link": f"https://research.example/new-{i}-{j}",
                            "published_at": "2026-10-17T01:00:00Z"} for j in range(3)]
            requested.clear()
            result = fetch(source, result["state"], NOW, request)
            observed.update(row["url"] for row in result["items"])
            self.assertLessEqual(len(requested), 3, "At most head + one front probe + catch-up budget")
        self.assertTrue({f"https://research.example/{i}" for i in range(8)} <= observed)

    def test_offset_unlocated_large_burst_remains_pending_with_bounded_requests(self):
        entries = [{"title": str(i), "link": f"https://research.example/{i}",
                    "published_at": "2026-10-17T00:00:00Z"} for i in range(10)]
        requested = []
        def request(url):
            p = int(parse_qs(urlsplit(url).query)["page"][0]); requested.append(p)
            return json.dumps({"data": entries[(p - 1) * 2:p * 2]}).encode()
        source = {**BASE, "kind": "alignment_feed"}
        result = fetch(source, {}, NOW, request)
        entries[:0] = [{"title": str(i), "link": f"https://research.example/new-{i}",
                        "published_at": "2026-10-17T01:00:00Z"} for i in range(20)]
        requested.clear()
        result = fetch(source, result["state"], NOW, request)
        self.assertTrue(result["coverage"]["pending_incremental"])
        self.assertLessEqual(len(requested), 3)
        self.assertEqual(len(result["state"]["forward_state"]["scan_segments"]), 2)

    def test_sitemap_baselines_unknown_old_index_and_fetches_recent_metadata_only(self):
        entries = [("https://research.example/old", "2020-01-01"),
                   ("https://research.example/undated", ""),
                   ("https://research.example/new", "2026-10-17T01:00:00Z")]
        requests = []
        def request(url):
            requests.append(url)
            return sitemap(entries) if url == BASE["feed_url"] else article()
        old = {"documents": {"https://research.example/old": "2019-01-01"}, "failures": {"https://research.example/old": {"attempts": 5}}}
        result = fetch({**BASE, "kind": "sitemap"}, old, NOW, request)
        self.assertEqual(requests, [BASE["feed_url"], "https://research.example/new"])
        self.assertEqual(result["state"]["documents"], old["documents"])
        self.assertTrue(result["coverage"]["baseline_boundary_uncertain"])
        requests.clear()
        quiet = fetch({**BASE, "kind": "sitemap"}, result["state"], NOW, request)
        self.assertEqual(requests, [BASE["feed_url"]])
        self.assertTrue(quiet["coverage"]["validated_empty"])

    def test_new_undated_sitemap_urls_after_baseline_form_resumable_queue(self):
        entries = [("https://research.example/old", "")]
        requested = []
        def request(url):
            requested.append(url)
            return sitemap(entries) if url == BASE["feed_url"] else article(date="")
        source = {**BASE, "kind": "sitemap"}
        result = fetch(source, {}, NOW, request)
        entries += [("https://research.example/new-a", ""), ("https://research.example/new-b", "")]
        first = fetch(source, result["state"], NOW, request)
        second = fetch(source, first["state"], NOW, request)
        self.assertTrue(first["coverage"]["pending_incremental"])
        self.assertFalse(second["coverage"]["pending_incremental"])
        self.assertEqual({row["url"] for row in first["items"] + second["items"]}, {url for url, _ in entries[1:]})
        self.assertTrue(all(not row["published_at"] for row in first["items"] + second["items"]))
        self.assertNotIn(entries[0][0], requested)

    def test_anthropic_does_not_recheck_stable_old_leading_card(self):
        source = {**BASE, "kind": "anthropic"}
        index = b'<a class="paper" href="/old">Old</a><a class="paper" href="/older">Older</a>'
        requested = []
        def request(url):
            requested.append(url)
            return index if url == source["url"] else article(date="2020-01-01")
        first = fetch(source, {}, NOW, request)
        self.assertEqual(requested, [source["url"], "https://research.example/old"])
        requested.clear()
        second = fetch(source, first["state"], NOW + timedelta(days=10), request)
        self.assertEqual(requested, [source["url"]])
        self.assertFalse(second["coverage"]["pending_incremental"])
        requested.clear()
        fetch({**source, "known_urls": ["https://research.example/old"]}, {}, NOW, request)
        self.assertEqual(requested, [source["url"]])

    def test_rss_unknown_first_run_requires_review_but_known_old_unknown_is_baselined(self):
        payload = b'<rss><channel><item><title>Old</title><link>https://research.example/old</link></item><item><title>New unknown</title><link>https://research.example/new</link></item></channel></rss>'
        source = {**BASE, "kind": "rss", "known_urls": ["https://research.example/old"]}
        result = fetch(source, {}, NOW, lambda _: payload)
        self.assertEqual([row["url"] for row in result["items"]], ["https://research.example/new"])
        self.assertIsNone(result["items"][0]["published_at"])

    def test_filtered_quiet_feed_is_valid_but_actually_empty_feed_is_not(self):
        source = {**BASE, "kind": "rss"}
        quiet = b'<rss><channel><item><title>Old</title><link>https://research.example/old</link><pubDate>2020-01-01</pubDate></item></channel></rss>'
        result = fetch(source, {}, NOW, lambda _: quiet)
        self.assertEqual(result["items"], [])
        self.assertTrue(result["coverage"]["validated_empty"])
        empty = fetch(source, {}, NOW, lambda _: b'<rss><channel></channel></rss>')
        self.assertFalse(empty["coverage"]["validated_empty"])

    def test_bibliography_baseline_never_downloads_old_publications(self):
        source = {**BASE, "kind": "bibliography"}
        body = '<p>A. Researcher. 2026. <a href="/old.pdf">Old alignment paper</a></p>'
        requested = []
        def request(url):
            requested.append(url)
            return body.encode()
        first = fetch(source, {}, NOW, request)
        self.assertEqual(first["items"], [])
        self.assertTrue(first["coverage"]["validated_empty"])
        body += '<p>A. Researcher. 2026. <a href="/new.pdf">New alignment paper</a></p>'
        second = fetch(source, first["state"], NOW, request)
        self.assertEqual([row["url"] for row in second["items"]], ["https://research.example/new.pdf"])
        self.assertEqual(requested, [source["feed_url"], source["feed_url"]])
        self.assertIsNone(second["items"][0]["published_at"])

    def test_openreview_initial_burst_resumes_recent_offset_and_preserves_archive(self):
        source = {**BASE, "kind": "openreview", "queries": ["AI alignment"]}
        cutoff_ms = int(datetime.fromisoformat(START.replace("Z", "+00:00")).timestamp() * 1000)
        notes = [{"id": f"paper-{i}", "tmdate": cutoff_ms + (10 - i) * 1000 if i < 7 else cutoff_ms - 1000,
                  "odate": cutoff_ms + 100 if i < 7 else cutoff_ms - 1000,
                  "content": {"title": f"Paper {i}"}} for i in range(9)]
        requested = []
        def request(url):
            q = parse_qs(urlsplit(url).query); offset = int(q["offset"][0]); requested.append(offset)
            self.assertEqual(q["sort"], ["tmdate:desc"])
            return json.dumps({"notes": notes[offset:offset + 2], "count": len(notes)}).encode()
        old = {"queries": {"AI alignment": {"backfill_offset": 800}}}
        state, observed = old, set()
        for _ in range(3):
            result = fetch(source, state, NOW, request); state = result["state"]
            observed.update(row["external_id"] for row in result["items"])
        self.assertEqual(requested, [0, 2, 0, 4, 0, 6])
        self.assertEqual(observed, {f"paper-{i}" for i in range(7)})
        self.assertFalse(result["coverage"]["pending_incremental"])
        self.assertFalse(result["coverage"]["publication_sort_available"])
        self.assertEqual(state["queries"], old["queries"])

    def test_openreview_unordered_results_are_degraded_without_historical_paging(self):
        source = {**BASE, "kind": "openreview", "queries": ["AI alignment"]}
        notes = [{"id": str(i), "tmdate": date, "content": {"title": f"Paper {i}"}}
                 for i, date in enumerate([1700000000000, 1800000000000])]
        requested = []
        def request(url):
            requested.append(url)
            return json.dumps({"notes": notes, "count": 1000}).encode()
        result = fetch(source, {}, NOW, request)
        self.assertEqual(len(requested), 1)
        self.assertTrue(result["coverage"]["degraded"])
        self.assertTrue(result["coverage"]["pending_incremental"])
        self.assertFalse(result["coverage"]["pending_backfill"])
        self.assertTrue(any("completeness cannot be established" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
