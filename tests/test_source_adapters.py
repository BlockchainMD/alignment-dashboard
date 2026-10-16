"""Network-free pagination and structural-health tests for source adapters."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import re
import sys
import unittest
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("source_adapters", ROOT / "scripts" / "source_adapters.py")
adapters = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapters
SPEC.loader.exec_module(adapters)
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def alignment_page(page, size=2):
    return json.dumps({"data": [
        {"title": f"Report {index}", "link": f"https://research.example/report-{index}",
         "published_at": "2026-10-15T00:00:00Z", "summary": "A publisher excerpt."}
        for index in range((page - 1) * size, page * size)
    ]}).encode()


class AlignmentFeedPaginationTests(unittest.TestCase):
    source = {
        "id": "alignment-feed", "name": "Alignment Feed", "kind": "alignment_feed",
        "url": "https://alignmentfeed.org/", "feed_url": "https://api.alignmentfeed.org/v1/articles",
        "page_size": 2, "backfill_pages": 2,
    }

    def test_refresh_head_and_persisted_history_cursor_collect_more_than_first_page(self):
        requested = []

        def request(url):
            query = parse_qs(urlsplit(url).query)
            page = int(query["page"][0])
            self.assertEqual(int(query["page_size"][0]), 2)
            requested.append(page)
            return alignment_page(page)

        first = adapters.fetch(self.source, {}, NOW, request)
        self.assertEqual(requested, [1, 2])
        self.assertEqual(len(first["items"]), 4)
        self.assertEqual(first["state"]["backfill_page"], 3)
        self.assertFalse(first["coverage"]["backfill_complete"])
        self.assertTrue(first["coverage"]["pending_backfill"])
        state = deepcopy(first["state"])
        requested.clear()
        second = adapters.fetch(self.source, state, NOW, request)
        self.assertEqual(requested, [1, 3, 4])
        self.assertEqual(second["state"]["backfill_page"], 5)
        self.assertEqual(state, first["state"], "A fetch must not mutate the persisted input checkpoint")
        all_urls = {row["url"] for row in first["items"] + second["items"]}
        self.assertEqual(all_urls, {f"https://research.example/report-{index}" for index in range(8)})

    def test_failure_on_a_later_page_does_not_mutate_the_history_checkpoint(self):
        state = {"backfill_page": 3, "backfill_complete": False}
        original = deepcopy(state)
        requested = []

        def request(url):
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            requested.append(page)
            if page == 4:
                raise TimeoutError("fixture: later page timed out")
            return alignment_page(page)

        with self.assertRaises(TimeoutError):
            adapters.fetch(self.source, state, NOW, request)
        self.assertIn(3, requested)
        self.assertIn(4, requested)
        self.assertEqual(state, original)

    def test_exhaustion_is_reported_only_after_a_real_empty_page(self):
        requested = []

        def request(url):
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            requested.append(page)
            return alignment_page(page) if page < 3 else b'{"data": []}'

        result = adapters.fetch(self.source, {"backfill_page": 3}, NOW, request)
        self.assertIn(3, requested)
        self.assertTrue(result["coverage"]["backfill_complete"])
        self.assertFalse(result["coverage"]["pending_backfill"])
        self.assertTrue(result["items"], "Current head publications must still be collected")

    def test_unrecognized_api_schema_is_an_error_not_a_healthy_empty_result(self):
        for payload in (b'{}', b'{"data": {}}', b'<html>maintenance</html>'):
            with self.subTest(payload=payload):
                with self.assertRaises((ValueError, TypeError)):
                    adapters.fetch(self.source, {}, NOW, lambda _url: payload)


class FeedAdapterTests(unittest.TestCase):
    source = {
        "id": "lab", "name": "Research Lab", "kind": "rss",
        "url": "https://research.example/", "feed_url": "https://research.example/feed.xml",
    }

    def test_feed_window_is_explicitly_distinguished_from_complete_historical_coverage(self):
        payload = b'''<rss version="2.0"><channel><title>Lab</title><item>
          <title>Research</title><link>https://research.example/paper</link>
          <description>A result</description></item></channel></rss>'''
        result = adapters.fetch(self.source, {}, NOW, lambda _url: payload)
        self.assertEqual(len(result["items"]), 1)
        self.assertFalse(result["coverage"]["backfill_complete"])
        self.assertTrue(result["warnings"])

    def test_feed_has_no_silent_two_hundred_fifty_record_cap(self):
        entries = "".join(
            f"<item><title>Research {index}</title><link>https://research.example/{index}</link></item>"
            for index in range(301)
        )
        payload = f"<rss><channel>{entries}</channel></rss>".encode()
        result = adapters.fetch(self.source, {}, NOW, lambda _url: payload)
        self.assertEqual(len(result["items"]), 301)

    def test_html_failure_page_cannot_be_accepted_as_a_supported_feed(self):
        with self.assertRaises(ValueError):
            adapters.fetch(self.source, {}, NOW, lambda _url: b"<html><body>Blocked</body></html>")

    def test_unknown_source_kind_fails_instead_of_defaulting_to_an_empty_success(self):
        with self.assertRaises(ValueError):
            adapters.fetch({**self.source, "kind": "unsupported"}, {}, NOW, lambda _url: b"{}")


def arxiv_page(start=0, total=6, count=2):
    entries = "".join(f'''<entry><id>https://arxiv.org/abs/2601.{10000 + index}v2</id>
      <title>AI control report {index}</title><published>2026-01-01T00:00:00Z</published>
      <updated>2026-10-15T00:00:00Z</updated><summary>Oversight finding.</summary>
      <author><name>Research Author</name></author>
      <link rel="alternate" href="https://arxiv.org/abs/2601.{10000 + index}v2"/>
    </entry>''' for index in range(start, start + count))
    return f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
      <opensearch:totalResults>{total}</opensearch:totalResults>
      <opensearch:startIndex>{start}</opensearch:startIndex>{entries}</feed>'''.encode()


class ArxivPaginationTests(unittest.TestCase):
    source = {
        "id": "arxiv", "name": "arXiv", "kind": "arxiv", "url": "https://arxiv.org/",
        "feed_url": "https://export.arxiv.org/api/query", "search_query": 'all:"AI alignment"',
        "page_size": 2, "backfill_pages": 1, "catchup_pages": 1, "request_interval_seconds": 0,
    }

    def test_year_backfill_and_frozen_incremental_window_resume_independently(self):
        upper = "2026-10-15T12:00:00.000Z"
        state = {"incremental_upper": upper, "incremental_start": 2,
                 "backfill_year": 2025, "backfill_start": 2}
        before = deepcopy(state)
        requests = []

        def request(url):
            query = parse_qs(urlsplit(url).query)
            requests.append(query)
            start = int(query["start"][0])
            return arxiv_page(start, total=6 if query["sortBy"] == ["lastUpdatedDate"] else 4)

        result = adapters.fetch(self.source, state, NOW, request)
        self.assertEqual(state, before)
        self.assertEqual(result["state"]["incremental_start"], 4)
        self.assertEqual(result["state"]["incremental_upper"], upper)
        self.assertEqual(result["state"]["backfill_year"], 2024)
        self.assertEqual(result["state"]["backfill_start"], 0)
        self.assertIn("TO 202610151200]", requests[0]["search_query"][0])
        self.assertIn("submittedDate:[202501010000 TO 202512312359]", requests[1]["search_query"][0])
        self.assertTrue(result["coverage"]["pending_incremental"])
        requests.clear()
        second = adapters.fetch(self.source, result["state"], NOW + timedelta(days=1), request)
        self.assertEqual(int(requests[0]["start"][0]), 4)
        self.assertIn("TO 202610151200]", requests[0]["search_query"][0])
        self.assertNotIn("incremental_upper", second["state"])
        self.assertFalse(second["coverage"]["pending_incremental"])

    def test_reported_more_results_cannot_be_skipped_after_an_unexpected_short_page(self):
        result = adapters.fetch(self.source, {"backfill_year": 2025}, NOW,
                                lambda _url: arxiv_page(start=0, total=5, count=1))
        self.assertEqual(result["state"]["incremental_start"], 1)
        self.assertEqual(result["state"]["backfill_year"], 2025)
        self.assertEqual(result["state"]["backfill_start"], 1)
        self.assertTrue(result["coverage"]["pending_incremental"])

    def test_empty_page_before_reported_end_is_an_error_and_keeps_input_cursor(self):
        state = {"backfill_year": 2025, "backfill_start": 2}
        with self.assertRaises(ValueError):
            adapters.fetch(self.source, state, NOW, lambda _url: arxiv_page(total=5, count=0))
        self.assertEqual(state, {"backfill_year": 2025, "backfill_start": 2})

    def test_missing_pagination_metadata_is_not_a_healthy_completed_backfill(self):
        payload = b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        with self.assertRaises(ValueError):
            adapters.fetch(self.source, {}, NOW, lambda _url: payload)

    def test_rss_fallback_preserves_all_api_cursors_even_after_a_later_page_failure(self):
        source = {**self.source, "rss_fallback_url": "https://rss.arxiv.org/rss/cs.AI", "fallback_match": "AI control"}
        state = {"incremental_upper": "2026-10-15T12:00:00.000Z", "incremental_start": 2,
                 "backfill_year": 2025, "backfill_start": 4}
        original = deepcopy(state)
        feed = b'''<rss xmlns:arxiv="http://arxiv.org/schemas/atom"><channel><item>
          <title>AI control replacement finding</title><link>https://arxiv.org/abs/2601.12345v2</link>
          <pubDate>Thu, 15 Oct 2026 00:00:00 GMT</pubDate><arxiv:announce_type>replace</arxiv:announce_type>
          </item><item><title>Unrelated image registration</title><link>https://arxiv.org/abs/2601.98765</link>
          </item></channel></rss>'''
        for fail_call in (1, 2):
            with self.subTest(fail_call=fail_call):
                api_calls = []

                def request(url):
                    if url == source["rss_fallback_url"]:
                        return feed
                    query = parse_qs(urlsplit(url).query)
                    api_calls.append(query)
                    if len(api_calls) == fail_call:
                        raise TimeoutError("fixture: arXiv search API unavailable")
                    return arxiv_page(start=int(query["start"][0]), total=6)

                result = adapters.fetch(source, state, NOW, request)
                self.assertEqual(result["state"], original)
                self.assertEqual(state, original)
                self.assertTrue(result["coverage"]["degraded"])
                self.assertFalse(result["coverage"]["backfill_complete"])
                self.assertEqual(len(result["items"]), 1)
                self.assertIsNone(result["items"][0]["published_at"], "A replacement announcement is not a new publication")
                self.assertTrue(result["items"][0]["updated_at"])
                self.assertTrue(result["warnings"])
                resumed_queries = []

                def restored(url):
                    self.assertNotEqual(url, source["rss_fallback_url"])
                    query = parse_qs(urlsplit(url).query)
                    resumed_queries.append(query)
                    return arxiv_page(start=int(query["start"][0]), total=6)

                resumed = adapters.fetch(source, result["state"], NOW, restored)
                self.assertEqual([int(query["start"][0]) for query in resumed_queries], [2, 4])
                self.assertEqual(resumed["state"]["incremental_start"], 4)
                self.assertEqual(resumed["state"]["backfill_year"], 2024)
                self.assertEqual(resumed["state"]["backfill_start"], 0)


class SitemapPaginationTests(unittest.TestCase):
    source = {
        "id": "lab", "name": "Lab", "kind": "sitemap", "url": "https://research.example/",
        "feed_url": "https://research.example/sitemap.xml", "include_pattern": "/research/",
        "article_budget": 1, "backfill_pages": 1,
    }
    sitemap = b'''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://research.example/research/one</loc><lastmod>2026-10-01</lastmod></url>
      <url><loc>https://research.example/research/two</loc><lastmod>2026-10-02</lastmod></url>
      <url><loc>https://research.example/research/three</loc><lastmod>2026-10-03</lastmod></url>
    </urlset>'''

    def request(self, url):
        if url.endswith("sitemap.xml"):
            return self.sitemap
        return f'''<html><head><title>Research {url.rsplit('/', 1)[-1]}</title>
          <meta property="article:modified_time" content="2026-10-03T00:00:00Z"/>
          <meta name="description" content="Technical research abstract."/></head></html>'''.encode()

    def test_bounded_sitemap_batches_resume_without_treating_lastmod_as_publication(self):
        state, found = {}, set()
        for index in range(3):
            result = adapters.fetch(self.source, state, NOW, self.request)
            self.assertEqual(len(result["items"]), 1)
            self.assertIsNone(result["items"][0]["published_at"])
            self.assertTrue(result["warnings"])
            self.assertEqual(result["coverage"]["pending_documents"], 2 - index)
            self.assertEqual(result["coverage"]["backfill_complete"], index == 2)
            found.add(result["items"][0]["url"])
            state = result["state"]
        self.assertEqual(len(found), 3)

    def test_article_failure_keeps_other_records_and_retries_without_starving_older_pages(self):
        state = {"documents": {}}
        requested = []
        failed_url = "https://research.example/research/two"

        def request(url):
            requested.append(url)
            if url.endswith("/two"):
                raise TimeoutError("fixture: second article timed out")
            return self.request(url)

        source = {**self.source, "article_budget": 2}
        result = adapters.fetch(source, state, NOW, request)
        self.assertEqual(state, {"documents": {}})
        self.assertEqual({row["url"] for row in result["items"]}, {"https://research.example/research/three"})
        self.assertNotIn(failed_url, result["state"]["documents"])
        self.assertIn("https://research.example/research/three", result["state"]["documents"])
        self.assertTrue(result["coverage"]["degraded"])
        self.assertFalse(result["coverage"]["backfill_complete"])
        self.assertEqual(result["coverage"]["pending_documents"], 2)
        failure = result["state"]["failures"][failed_url]
        self.assertEqual(failure["attempts"], 1)
        self.assertEqual(failure["retry_after"], "2026-10-16T13:00:00.000Z")
        requested.clear()
        # The failed newer page is still queued, but its backoff must leave
        # room for older unfetched research rather than blocking all progress.
        second = adapters.fetch(source, result["state"], NOW + timedelta(minutes=30), request)
        self.assertNotIn(failed_url, requested)
        self.assertEqual({row["url"] for row in second["items"]}, {"https://research.example/research/one"})
        self.assertEqual(second["state"]["failures"][failed_url], failure)
        requested.clear()
        third = adapters.fetch(source, second["state"], NOW + timedelta(hours=1), request)
        self.assertIn(failed_url, requested)
        self.assertNotIn(failed_url, third["state"]["documents"])
        self.assertEqual(third["state"]["failures"][failed_url]["attempts"], 2)
        self.assertEqual(third["state"]["failures"][failed_url]["retry_after"], "2026-10-16T15:00:00.000Z")
        self.assertEqual(len(third["state"]["documents"]), 2)

    def test_malformed_child_sitemap_cannot_be_silently_ignored_in_a_complete_index(self):
        index = b'''<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://research.example/good.xml</loc></sitemap>
          <sitemap><loc>https://research.example/broken.xml</loc></sitemap></sitemapindex>'''

        def request(url):
            if url.endswith("sitemap.xml"):
                return index
            if url.endswith("good.xml"):
                return self.sitemap
            if url.endswith("broken.xml"):
                return b"<html><body>Temporarily unavailable</body></html>"
            return self.request(url)

        with self.assertRaises(ValueError):
            adapters.fetch(self.source, {}, NOW, request)

    def test_unchanged_lastmod_still_rechecks_old_pages_and_rotates_the_recheck_budget(self):
        urls = [f"https://research.example/research/{slug}" for slug in ("one", "two", "three")]
        state = {"documents": dict(zip(urls, ("2026-10-01", "2026-10-02", "2026-10-03"))),
                 "checked_at": {url: (NOW - timedelta(hours=169)).isoformat() for url in urls}}
        source = {**self.source, "recheck_budget": 1, "recheck_after_hours": 168}
        first = adapters.fetch(source, state, NOW, self.request)
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(first["coverage"]["pending_documents"], 0)
        second = adapters.fetch(source, first["state"], NOW + timedelta(hours=1), self.request)
        self.assertEqual(len(second["items"]), 1)
        self.assertNotEqual(first["items"][0]["url"], second["items"][0]["url"])
        self.assertEqual(state["checked_at"][urls[0]], (NOW - timedelta(hours=169)).isoformat())


class AnthropicRecheckTests(unittest.TestCase):
    def test_old_index_articles_are_rechecked_after_a_ttl_alongside_the_newest(self):
        source = {"id": "anthropic", "name": "Anthropic", "kind": "anthropic",
                  "url": "https://alignment.anthropic.com/", "article_budget": 1, "recheck_budget": 1,
                  "recheck_after_hours": 168}
        newest = source["url"] + "2026/newest/"
        older = source["url"] + "2024/older/"
        state = {"documents": {newest: NOW.isoformat(), older: (NOW - timedelta(hours=169)).isoformat()}}

        def request(url):
            if url == source["url"]:
                return b'<html><a class="paper" href="/2026/newest/"></a><a class="note" href="/2024/older/"></a></html>'
            return f'<html><title>AI control {url}</title><meta name="description" content="Updated publisher excerpt"/></html>'.encode()

        result = adapters.fetch(source, state, NOW, request)
        self.assertEqual({row["url"] for row in result["items"]}, {newest, older})
        self.assertEqual(result["state"]["documents"][older], "2026-10-16T12:00:00.000Z")
        self.assertEqual(state["documents"][older], (NOW - timedelta(hours=169)).isoformat())


class PublisherIndexTests(unittest.TestCase):
    def test_dated_cards_keep_explicit_dates_and_external_paper_links_without_fetching_them(self):
        source = {"id": "publisher", "name": "Publisher", "kind": "dated_index", "url": "https://research.example/"}
        body = b'''<html><a class="note card" href="/papers/one" data-date="2026-10-01">
          <h3>AI <em>control</em> result</h3><div class="description">Publisher <b>abstract</b>.</div></a>
          <a class="paper" href="https://arxiv.org/abs/2601.12345" data-date="2026-09-02">
          <h3>Second research result</h3><div class="description">Another abstract.</div></a></html>'''
        requested = []

        def request(url):
            requested.append(url)
            self.assertEqual(url, source["url"])
            return body

        result = adapters.fetch(source, {}, NOW, request)
        self.assertEqual(requested, [source["url"]])
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["title"], "AI control result")
        self.assertEqual(result["items"][0]["published_at"], "2026-10-01")
        self.assertEqual(result["items"][0]["date_precision"], "day")
        self.assertEqual({row["url"] for row in result["items"]},
                         {"https://research.example/papers/one", "https://arxiv.org/abs/2601.12345"})
        self.assertEqual(len({row["external_id"] for row in result["items"]}), 2)
        self.assertTrue(result["coverage"]["backfill_complete"])
        self.assertTrue(result["warnings"], "A traversed index must still disclose its scope")

    def test_bibliography_uses_the_cited_title_link_and_preserves_year_only_precision(self):
        source = {"id": "bibliography", "name": "Research Institute", "kind": "bibliography",
                  "url": "https://research.example/", "feed_url": "https://research.example/publications/"}
        body = b'''<html><p>Alice Researcher and Bob Scientist. 2004. "<a href="/files/early.pdf">Reliable agent behavior</a>.
          "Technical report. See <a href="https://arxiv.org/abs/2601.98765">a separate cited reference</a>.</p>
          <p>Carol Author. 2023. "<a href="https://arxiv.org/abs/2301.12345v2">A second alignment result</a>."</p>
          <p><a href="https://research.example/about">About the institute</a></p></html>'''
        requested = []

        def request(url):
            requested.append(url)
            self.assertEqual(url, source["feed_url"])
            return body

        result = adapters.fetch(source, {}, NOW, request)
        self.assertEqual(requested, [source["feed_url"]])
        self.assertEqual(len(result["items"]), 2)
        first, second = result["items"]
        self.assertEqual(first["title"], "Reliable agent behavior")
        self.assertEqual(first["publication_year"], 2004)
        self.assertEqual(first["date_precision"], "year")
        self.assertIsNone(first["published_at"])
        self.assertEqual(first["authors"], ["Alice Researcher", "Bob Scientist"])
        self.assertEqual(first["url"], "https://research.example/files/early.pdf")
        self.assertEqual([link["url"] for link in first["links"]], [first["url"]])
        self.assertEqual(second["arxiv_id"], "2301.12345")
        self.assertEqual(second["links"], [])
        self.assertTrue(result["coverage"]["backfill_complete"])
        self.assertTrue(result["warnings"])

    def test_unrecognized_bibliography_is_an_error_instead_of_a_completed_empty_archive(self):
        source = {"id": "bibliography", "name": "Research Institute", "kind": "bibliography",
                  "url": "https://research.example/", "feed_url": "https://research.example/publications/"}
        with self.assertRaises(ValueError):
            adapters.fetch(source, {}, NOW, lambda _url: b"<html><p>Temporarily unavailable</p></html>")


class ForumPaginationTests(unittest.TestCase):
    def test_tied_timestamps_preserve_offset_and_frozen_incremental_window(self):
        source = {
            "id": "forum", "name": "Alignment Forum", "kind": "forum_graphql",
            "url": "https://www.alignmentforum.org/", "feed_url": "https://www.alignmentforum.org/graphql",
            "page_size": 2, "catchup_pages": 1, "backfill_pages": 1, "af_only": True,
        }
        queries = []

        def request(url):
            query = parse_qs(urlsplit(url).query)["query"][0]
            queries.append(query)
            offset = int(re.search(r"offset:(\d+)", query).group(1))
            rows = [
                {"_id": str(index), "title": f"Report {index}",
                 "postedAt": "2026-10-14T00:00:00Z", "pageUrl": f"https://www.alignmentforum.org/posts/{index}/report",
                 "contents": {"plaintextMainText": "A technical finding."}, "user": {"displayName": "Researcher"}}
                for index in (range(2) if offset == 0 else range(2, 3))
            ]
            return json.dumps({"data": {"posts": {"results": rows}}}).encode()

        first = adapters.fetch(source, {"backfill_complete": True}, NOW, request)
        self.assertTrue(first["coverage"]["pending_incremental"])
        self.assertEqual(first["state"]["incremental_offset"], 2)
        upper = first["state"]["incremental_upper"]
        second = adapters.fetch(source, first["state"], NOW + timedelta(days=1), request)
        self.assertIn('before:"2026-10-14T00:00:00.001Z"', queries[1])
        self.assertIn("offset:2", queries[1])
        self.assertEqual(first["state"]["incremental_upper"], upper)
        self.assertFalse(second["coverage"]["pending_incremental"])
        self.assertNotIn("incremental_upper", second["state"])
        self.assertEqual({row["external_id"] for row in first["items"] + second["items"]}, {"0", "1", "2"})


if __name__ == "__main__":
    unittest.main()
