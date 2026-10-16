"""Public research discovery adapters; transport, storage and policy live elsewhere.

``fetch(source, state, now, request)`` never mutates its checkpoint. The caller
commits the returned checkpoint only after persisting every returned item.
Response budgets leave visible resumable backlogs, rather than dropping history.
All supplied content is untrusted data. This module uses only the standard library.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import json
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

ATOM = "{http://www.w3.org/2005/Atom}"
SM = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
ARXIV = "{http://arxiv.org/schemas/atom}"
UTC = timezone.utc


def _iso(value):
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)
    except ValueError:
        try:
            result = parsedate_to_datetime(value)
            return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)
        except (ValueError, TypeError, OverflowError):
            return None


def _query(url, **values):
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in values.items() if value is not None})
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _xml(body):
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("XML entity and document declarations are unsupported")
    return ET.fromstring(body)


def _text(node, *names):
    for name in names:
        child = node.find(name)
        if child is not None:
            value = "".join(child.itertext()).strip()
            if value:
                return value
    return ""


def _authors(value):
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, dict):
        return _authors(value.get("name") or value.get("displayName") or "")
    if isinstance(value, list):
        return [name for entry in value for name in _authors(entry)]
    return []


class _Plain(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template", "noscript"}:
            self.hidden += 1
        if not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "template", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden:
            self.parts.append(" ")

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def _plain(value, limit=3000):
    parser = _Plain()
    parser.feed(value or "")
    return re.sub(r"\s+", " ", unescape("".join(parser.parts))).strip()[:limit]


def parse_feed(body):
    """Read the whole RSS/Atom response, including author and paper identities."""
    root = _xml(body)
    items = []
    if root.tag == ATOM + "feed":
        for node in root.findall(ATOM + "entry"):
            identifier = _text(node, ATOM + "id")
            if "/api/errors" in identifier:
                raise ValueError("arXiv returned an API error: " + _plain(_text(node, ATOM + "summary"), 180))
            links = [{"url": link.get("href"), "rel": link.get("rel", "alternate"),
                      "type": link.get("type", "")} for link in node.findall(ATOM + "link") if link.get("href")]
            url = next((link["url"] for link in links if link["rel"] == "alternate"), identifier)
            arxiv_id = None
            if "arxiv.org/" in identifier:
                arxiv_id = re.sub(r"v\d+$", "", identifier.split("/abs/")[-1])
            items.append({"title": _text(node, ATOM + "title"), "url": url,
                          "published_at": _text(node, ATOM + "published") or None,
                          "updated_at": _text(node, ATOM + "updated") or None,
                          "summary": _plain(_text(node, ATOM + "summary", ATOM + "content")),
                          "authors": [_text(author, ATOM + "name") for author in node.findall(ATOM + "author")],
                          "external_id": identifier, "arxiv_id": arxiv_id,
                          "doi": _text(node, ARXIV + "doi") or None, "links": links})
        return items
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("Response is not a supported RSS/Atom feed")
    for node in root.findall("./channel/item"):
        url = _text(node, "link", "guid")
        row = {"title": _text(node, "title"), "url": url,
                      "published_at": _text(node, "pubDate", "{http://purl.org/dc/elements/1.1/}date") or None,
                      "summary": _plain(_text(node, "description", "{http://purl.org/rss/1.0/modules/content/}encoded")),
                      "authors": _authors(_text(node, "{http://purl.org/dc/elements/1.1/}creator", "author")),
                      "external_id": _text(node, "guid") or url, "links": []}
        announcement = _text(node, ARXIV + "announce_type")
        if announcement:
            row["arxiv_id"] = re.sub(r"v\d+$", "", url.split("/abs/")[-1])
            row["announced_at"] = row["published_at"]
            row["date_precision"] = "day"
            row["license"] = _text(node, "{http://purl.org/dc/elements/1.1/}rights") or None
            if announcement != "new":
                # A replacement/cross-list announcement is not a paper's first
                # public publication date. Preserve the unknown date for review.
                row["updated_at"], row["published_at"] = row["published_at"], None
        items.append(row)
    return items


def _coverage(items, mode, pages, complete=False, **extra):
    dates = [item.get("published_at") for item in items if _date(item.get("published_at"))]
    return {"mode": mode, "pages": pages, "backfill_complete": bool(complete),
            "pending_backfill": not complete, "oldest_fetched_at": min(dates) if dates else None,
            "latest_fetched_at": max(dates) if dates else None, **extra}


def _result(items, state, coverage, warnings=None):
    return {"items": items, "state": state, "coverage": coverage, "warnings": warnings or []}


def _identity(row):
    return str(row.get("external_id") or row.get("url") or "")


def _offset_archive(source, state, now, request, kind):
    size = max(1, min(int(source.get("page_size", 50)), 100))
    budget = max(1, min(int(source.get("backfill_pages", 1)), 20))
    cache, seen_pages = {}, {}

    def page(number):
        if number in cache:
            return cache[number]
        if kind == "alignment_feed":
            body = request(_query(source["feed_url"], page=number, page_size=size, sort="published_at_desc"))
            payload = json.loads(body)
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise ValueError("Alignment Feed response has no article list")
            parsed = [{"title": row.get("title"), "url": row.get("link"),
                       "published_at": row.get("published_at"), "updated_at": row.get("updated_at"),
                       "summary": row.get("summary") or row.get("text_start") or "",
                       "summary_kind": "aggregator summary", "authors": _authors(row.get("authors")),
                       "external_id": row.get("hash_id") or row.get("link"), "links": []}
                      for row in rows if isinstance(row, dict)]
        else:
            rows = json.loads(request(_query(source["archive_url"], sort="new", offset=(number - 1) * size, limit=size)))
            if not isinstance(rows, list):
                raise ValueError("Substack archive response is not an article list")
            parsed = [{"title": row.get("title"), "url": row.get("canonical_url"),
                       "published_at": row.get("post_date"),
                       "summary": row.get("description") or row.get("subtitle") or row.get("truncated_body_text") or "",
                       "authors": _authors(row.get("publishedBylines")), "external_id": str(row.get("id", "")),
                       "links": []} for row in rows if isinstance(row, dict)]
        if len(parsed) != len(rows):
            raise ValueError("Archive returned malformed article records")
        if parsed:
            signature = tuple(_identity(row) for row in parsed)
            if signature in seen_pages and seen_pages[signature] != number:
                raise ValueError("Archive pagination repeated a page; checkpoint preserved")
            seen_pages[signature] = number
        cache[number] = parsed
        return parsed

    latest = page(1)
    previous_head = state.get("head_id")
    # A delayed run scans until the previously observed newest article. This
    # catches items beyond the first page instead of silently skipping a burst.
    pending_head = False
    if previous_head and previous_head not in {_identity(row) for row in latest}:
        number = max(2, int(state.get("head_scan_page", 2)))
        for _ in range(max(1, int(source.get("catchup_pages", 2)))):
            rows = page(number)
            if not rows or previous_head in {_identity(row) for row in rows}:
                state.pop("head_scan_page", None)
                break
            number += 1
        else:
            state["head_scan_page"] = number
            pending_head = True
    if not pending_head and latest:
        state["head_id"] = _identity(latest[0])
    complete = bool(state.get("backfill_complete"))
    if not complete:
        number = max(1, int(state.get("backfill_page", 1)))
        for _ in range(budget):
            rows = page(number)
            number += 1
            if len(rows) < size:
                complete = True
                break
        state.update(backfill_page=number, backfill_complete=complete)
    items = [row for rows in cache.values() for row in rows]
    warnings = ["Recent catch-up is pending; the saved page cursor will resume next run."] if pending_head else []
    return _result(items, state, _coverage(items, "paginated_public_archive", len(cache), complete,
                   pending_incremental=pending_head, next_page=state.get("backfill_page")), warnings)


def _forum(source, state, now, request):
    size = max(1, min(int(source.get("page_size", 50)), 100))
    pages, items = 0, []

    def page(before=None, after=None, offset=0):
        nonlocal pages
        terms = {"view": "new", "limit": size, "offset": offset}
        if source.get("af_only"):
            terms["af"] = True
        if before:
            terms["before"] = before
        if after:
            terms["after"] = after
        fields = ",".join(key + ":" + json.dumps(value) for key, value in terms.items())
        query = "{posts(input:{terms:{" + fields + "}}){results{_id title postedAt pageUrl url contents{plaintextMainText} user{displayName}}}}"
        payload = json.loads(request(_query(source["feed_url"], query=query)))
        if payload.get("errors"):
            raise ValueError("Forum GraphQL returned errors")
        rows = payload.get("data", {}).get("posts", {}).get("results")
        if not isinstance(rows, list):
            raise ValueError("Forum GraphQL returned no posts list")
        pages += 1
        for row in rows:
            if not isinstance(row, dict) or not row.get("_id") or not _date(row.get("postedAt")):
                raise ValueError("Forum GraphQL returned malformed article records")
            items.append({"title": row.get("title"), "url": row.get("pageUrl"),
                          "published_at": row.get("postedAt"),
                          "summary": (row.get("contents") or {}).get("plaintextMainText", "")[:3000],
                          "authors": _authors(row.get("user")), "external_id": row.get("_id"),
                          "links": [{"url": row["url"], "rel": "source"}] if row.get("url") else []})
        return rows

    # Incremental windows are frozen while being paginated. An overlapping day
    # catches publication-time edits; long outages resume without a latest-page cap.
    upper = state.get("incremental_upper") or _iso(now)
    lower = state.get("watermark") or _iso(now - timedelta(days=7))
    cursor = state.get("incremental_before") or upper
    offset = int(state.get("incremental_offset", 0))
    incremental_complete = False
    for _ in range(max(1, int(source.get("catchup_pages", 1)))):
        rows = page(cursor, lower, offset)
        if len(rows) < size:
            incremental_complete = True
            break
        oldest = min(_date(row["postedAt"]) for row in rows)
        same = sum(_date(row["postedAt"]) == oldest for row in rows)
        next_cursor = _iso(oldest + timedelta(milliseconds=1))
        offset = offset + same if cursor == next_cursor else same
        cursor = next_cursor
    if incremental_complete:
        state["watermark"] = _iso((_date(upper) or now) - timedelta(days=1))
        for key in ("incremental_upper", "incremental_before", "incremental_offset"):
            state.pop(key, None)
    else:
        state.update(incremental_upper=upper, incremental_before=cursor, incremental_offset=offset)
    complete = bool(state.get("backfill_complete"))
    if not complete:
        cursor = state.get("backfill_before") or _iso(now)
        offset = int(state.get("backfill_offset", 0))
        for _ in range(max(1, min(int(source.get("backfill_pages", 1)), 20))):
            rows = page(cursor, offset=offset)
            if len(rows) < size:
                complete = True
                break
            oldest = min(_date(row["postedAt"]) for row in rows)
            same = sum(_date(row["postedAt"]) == oldest for row in rows)
            next_cursor = _iso(oldest + timedelta(milliseconds=1))
            offset = offset + same if cursor == next_cursor else same
            cursor = next_cursor
        state.update(backfill_before=cursor, backfill_offset=offset, backfill_complete=complete)
    warnings = [] if incremental_complete else ["Recent forum catch-up is pending and will resume from its saved date cursor."]
    return _result(items, state, _coverage(items, "date_paginated_forum", pages, complete,
                   pending_incremental=not incremental_complete, validated_empty=not items), warnings)


def _arxiv(source, state, now, request):
    size = max(1, min(int(source.get("page_size", 100)), 1000))
    search = source["search_query"]
    pages, items, last_request = 0, [], 0.0

    def page(query, start=0, sort="submittedDate"):
        nonlocal pages, last_request
        # arXiv's public API asks clients to leave three seconds between calls.
        spacing = float(source.get("request_interval_seconds", 3.05))
        delay = spacing - (time.monotonic() - last_request)
        if last_request and delay > 0:
            time.sleep(delay)
        body = request(_query(source["feed_url"], search_query=query, start=start,
                              max_results=size, sortBy=sort, sortOrder="descending"))
        last_request = time.monotonic()
        root = _xml(body)
        rows = parse_feed(body)
        total = _text(root, OPENSEARCH + "totalResults")
        if not total or not total.isdigit():
            raise ValueError("arXiv response is missing its total-results metadata")
        total = int(total)
        if start < total and not rows:
            raise ValueError("arXiv returned an empty page before its reported end")
        if start and len(rows) and (index := _text(root, OPENSEARCH + "startIndex")) and int(index) != start:
            raise ValueError("arXiv pagination start does not match the request")
        items.extend(rows)
        pages += 1
        return rows, total

    def stamp(value):
        return value.astimezone(UTC).strftime("%Y%m%d%H%M")

    upper = state.get("incremental_upper") or _iso(now)
    start = int(state.get("incremental_start", 0))
    lower = _date(state.get("watermark")) or now - timedelta(days=7)
    # arXiv documents submittedDate as its sole date-search field. In particular,
    # lastUpdatedDate is a SORT option, not a supported range-search field. Sort
    # the query by updates and stop client-side at the overlapping watermark.
    # The submitted-date ceiling keeps newly submitted papers out of a pending
    # snapshot; changed older versions can still move and are re-read on overlap.
    query = f'({search}) AND submittedDate:[199101010000 TO {stamp(_date(upper) or now)}]'
    incremental_complete = False
    for _ in range(max(1, int(source.get("catchup_pages", 1)))):
        rows, total = page(query, start, "lastUpdatedDate")
        start += len(rows)
        update_dates = [_date(row.get("updated_at")) for row in rows]
        crossed_watermark = bool(rows) and all(date is not None for date in update_dates) and min(update_dates) < lower
        if start >= total or crossed_watermark:
            incremental_complete = True
            break
        if start >= 30000:
            raise ValueError("arXiv update window exceeds 30,000 results; narrow the query/date window")
    if incremental_complete:
        state["watermark"] = _iso((_date(upper) or now) - timedelta(days=7))
        state.pop("incremental_start", None)
        state.pop("incremental_upper", None)
    else:
        state.update(incremental_start=start, incremental_upper=upper)

    year = int(state.get("backfill_year", now.year))
    start = int(state.get("backfill_start", 0))
    complete = bool(state.get("backfill_complete"))
    if not complete:
        for _ in range(max(1, min(int(source.get("backfill_pages", 1)), 20))):
            query = f'({search}) AND submittedDate:[{year}01010000 TO {year}12312359]'
            rows, total = page(query, start)
            start += len(rows)
            if start >= total:
                year, start = year - 1, 0
            elif start >= 30000:
                raise ValueError("arXiv annual backfill exceeds 30,000 results; split the query")
            if year < 1991:
                complete = True
                break
        state.update(backfill_year=year, backfill_start=start, backfill_complete=complete)
    warnings = [] if incremental_complete else ["Recent arXiv updates exceed this run's budget; the saved offset will resume next run."]
    return _result(items, state, _coverage(items, "year_partitioned_arxiv", pages, complete,
                   pending_incremental=not incremental_complete, next_year=year, next_offset=start,
                   validated_empty=not items), warnings)


class _Metadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.json_blocks, self.links, self.times = {}, [], [], []
        self.title, self.h1 = "", ""
        self._title = self._h1 = False
        self._seen_h1 = False
        self._json, self._block = False, ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = attrs.get("name") or attrs.get("property")
            if key and attrs.get("content"):
                self.meta.setdefault(key.lower(), []).append(attrs["content"])
        elif tag == "script" and attrs.get("type", "").lower() in {"application/ld+json", "text/json"}:
            self._json, self._block = True, ""
        elif tag == "title":
            self._title = True
        elif tag == "h1":
            self._h1 = not self._seen_h1
            self._seen_h1 = True
        elif tag == "time" and attrs.get("datetime"):
            self.times.append(attrs["datetime"])
        elif tag == "link" and attrs.get("href") and attrs.get("type") == "application/pdf":
            self.links.append({"url": attrs["href"], "rel": "fulltext", "type": "application/pdf"})

    def handle_endtag(self, tag):
        if tag == "title":
            self._title = False
        elif tag == "h1":
            self._h1 = False
        elif tag == "script" and self._json:
            self.json_blocks.append(self._block)
            self._json = False

    def handle_data(self, value):
        if self._title:
            self.title += value
        if self._h1:
            self.h1 += value
        if self._json:
            self._block += value


def _json_articles(value):
    if isinstance(value, list):
        for entry in value:
            yield from _json_articles(entry)
    elif isinstance(value, dict):
        if "@graph" in value:
            yield from _json_articles(value["@graph"])
        kind = value.get("@type", "")
        if any(term in str(kind) for term in ("Article", "Posting", "ScholarlyArticle", "Report")):
            yield value


def parse_article(body, url):
    text = body.decode("utf-8", errors="replace")
    parser = _Metadata()
    parser.feed(text)
    meta = lambda *keys: next((parser.meta[key][0] for key in keys if parser.meta.get(key)), None)
    ld, frontmatter = [], {}
    for block in parser.json_blocks:
        try:
            value = json.loads(block)
            ld.extend(_json_articles(value))
            if isinstance(value, dict) and "description" in value and "@type" not in value:
                frontmatter = value
        except (ValueError, TypeError):
            continue
    article = ld[0] if ld else {}
    published = meta("citation_publication_date", "article:published_time", "datepublished", "date") or article.get("datePublished")
    # FAR's Webflow migration date occurs in JSON-LD. The research byline carries
    # the actual original publication date, so it takes precedence.
    byline_patterns = [r'<p[^>]+class=["\'][^"\']*research_hero_date[^"\']*["\'][^>]*>(.*?)</p>',
                       r'Published on\s*</[^>]+>\s*<[^>]+>(.*?)</[^>]+>',
                       r'Published\s*(?:</a>\s*)?</(?:h\d|span|p)>\s*<(?:div|p|span)[^>]*>(.*?)</(?:div|p|span)>',
                       r'<(?:div|d-byline)[^>]+class=["\'](?:section-authors|d-byline)["\'][^>]*>([\s\S]{0,6000}?)</(?:div|d-byline)>']
    for pattern in byline_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            visible = _plain(match.group(1))
            date = re.search(r'\b(?:[A-Za-z]+\s+\d{1,2},?\s+20\d{2}|\d{1,2}\s+[A-Za-z]+\s+20\d{2})\b', visible)
            if date:
                for fmt in ("%B %d, %Y", "%B %d %Y", "%d %B %Y", "%b %d, %Y"):
                    try:
                        published = datetime.strptime(date.group(0), fmt).date().isoformat()
                        break
                    except ValueError:
                        continue
    if not published and parser.times:
        published = parser.times[0]
    if not published:
        # Anthropic Distill articles may have a custom d-byline rather than meta.
        match = re.search(r'<d-byline[^>]*>([\s\S]*?)</d-byline>', text, re.I)
        if match:
            match = re.search(r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+20\d{2}\b', _plain(match.group(1), 6000))
            if match:
                published = match.group(0)
    title = meta("citation_title") or article.get("headline") or _plain(parser.h1) or meta("og:title") or parser.title
    summary = meta("description", "og:description", "citation_abstract") or article.get("description") or frontmatter.get("description") or ""
    authors = parser.meta.get("citation_author", []) or parser.meta.get("author", []) or _authors(article.get("author")) or _authors(frontmatter.get("authors"))
    links = parser.links
    pdf = meta("citation_pdf_url")
    if pdf:
        links.append({"url": urljoin(url, pdf), "rel": "fulltext", "type": "application/pdf"})
    # An explicitly labelled paper button is an article identity; bibliography
    # links are deliberately excluded to avoid merging a paper with its references.
    for match in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>\s*(?:Paper|Read the paper|Full paper)\s*</a>', text, re.I):
        links.append({"url": urljoin(url, unescape(match.group(1))), "rel": "paper"})
    return {"title": _plain(title, 500), "url": url, "published_at": published,
            "updated_at": meta("article:modified_time") or article.get("dateModified"),
            "summary": _plain(summary), "authors": authors, "links": links,
            "doi": meta("citation_doi", "dc.identifier"), "arxiv_id": meta("citation_arxiv_id"),
            "external_id": url}


def _sitemap(source, state, now, request):
    body = request(source["feed_url"])
    root = _xml(body)
    pages = 1
    if root.tag == SM + "sitemapindex":
        maps = [node.text for node in root.findall("./" + SM + "sitemap/" + SM + "loc") if node.text]
        if len(maps) > 50:
            raise ValueError("Sitemap index has more than 50 children; configure a specific child sitemap")
        roots = [_xml(request(url)) for url in maps]
        if any(child.tag != SM + "urlset" for child in roots):
            raise ValueError("Sitemap index contains an unsupported or malformed child sitemap")
        pages += len(roots)
    elif root.tag == SM + "urlset":
        roots = [root]
    else:
        raise ValueError("Response is not a sitemap")
    pattern = re.compile(source.get("include_pattern", r".*"))
    documents = []
    for root in roots:
        for node in root.findall(SM + "url"):
            url, modified = _text(node, SM + "loc"), _text(node, SM + "lastmod")
            if url and pattern.search(url):
                documents.append((url, modified))
    if not documents:
        raise ValueError("Sitemap contains no matching research pages")
    old = state.get("documents", {})
    if not isinstance(old, dict):
        old = {}
    pending = [(url, modified) for url, modified in documents if url not in old or old[url] != modified]
    pending.sort(key=lambda pair: (pair[1], pair[0]), reverse=True)
    budget = max(1, min(int(source.get("article_budget", 12)) * int(source.get("backfill_pages", 1)), 200))
    checked = state.setdefault("checked_at", {})
    failures = state.setdefault("failures", {})
    def due(url):
        retry = _date((failures.get(url) or {}).get("retry_after"))
        return retry is None or retry <= now
    stale_before = now - timedelta(hours=float(source.get("recheck_after_hours", 168)))
    stale = [(url, modified) for url, modified in documents if url in old and
             ((_date(checked.get(url)) or datetime.min.replace(tzinfo=UTC)) < stale_before)]
    stale.sort(key=lambda pair: checked.get(pair[0], ""))
    selected = dict([pair for pair in pending if due(pair[0])][:budget] +
                    [pair for pair in stale if due(pair[0])][:max(1, int(source.get("recheck_budget", 2)))])
    rows, failed_this_run = [], []
    for url, modified in selected.items():
        # Publisher-owned URLs only. The transport independently validates URLs.
        if urlsplit(url).hostname not in source.get("allowed_hosts", [urlsplit(source["url"]).hostname]):
            raise ValueError("Sitemap linked to an unexpected host")
        try:
            row = parse_article(request(url), url)
            if not row["title"]:
                raise ValueError("Research page has no article title")
        except Exception as error:
            attempts = min(20, int((failures.get(url) or {}).get("attempts", 0)) + 1)
            failures[url] = {"attempts": attempts, "error": _plain(str(error), 180),
                             "retry_after": _iso(now + timedelta(hours=min(24, 2 ** min(attempts - 1, 5))))}
            failed_this_run.append(url)
            pages += 1
            continue
        rows.append(row)
        old[url] = modified
        checked[url] = _iso(now)
        failures.pop(url, None)
        pages += 1
    state["documents"] = old
    pending_count = sum(url not in old or old[url] != modified for url, modified in documents)
    complete = pending_count == 0
    warnings = []
    missing = sum(not row.get("published_at") for row in rows)
    if missing:
        warnings.append(f"{missing} research pages have no explicit publication date; retained for review.")
    if failures:
        warnings.append(f"{len(failures)} publisher pages remain queued after fetch failures; bounded retries back off up to 24 hours while other pages continue.")
    return _result(rows, state, _coverage(rows, "publisher_sitemap", pages, complete,
                   discovered_urls=len(documents), pending_documents=pending_count,
                   failed_documents=len(failures), failed_this_run=len(failed_this_run),
                   degraded=bool(failures), validated_empty=not rows), warnings)


class _AnthropicIndex(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and set(attrs.get("class", "").split()) & {"note", "paper"} and attrs.get("href"):
            self.urls.append(attrs["href"])


class _DatedCards(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self.card, self.field, self.depth = [], None, None, 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get("class", "").split())
        if tag == "a" and classes & {"note", "paper"} and attrs.get("href"):
            self.card = {"url": attrs["href"], "title": "", "summary": "",
                         "published_at": attrs.get("data-date"), "date_precision": "day",
                         "authors": [], "links": []}
        if self.card is not None:
            if tag == "h3":
                self.field, self.depth = "title", 1
            elif tag == "div" and "description" in classes:
                self.field, self.depth = "summary", 1
            elif self.field and tag not in {"img", "br", "hr", "input", "meta", "link"}:
                self.depth += 1

    def handle_endtag(self, tag):
        if self.card is not None:
            if tag == "a":
                self.items.append(self.card)
                self.card, self.field = None, None
            elif self.field:
                self.depth -= 1
                if self.depth <= 0:
                    self.field = None

    def handle_data(self, value):
        if self.card is not None and self.field:
            self.card[self.field] += value + " "


def _dated_index(source, state, now, request):
    parser = _DatedCards()
    parser.feed(request(source["url"]).decode("utf-8", errors="replace"))
    rows = []
    for row in parser.items:
        row["url"] = urljoin(source["url"], row["url"])
        row["title"], row["summary"] = _plain(row["title"], 500), _plain(row["summary"])
        row["external_id"] = row["url"]
        rows.append(row)
    if not rows or any(not row["title"] for row in rows):
        raise ValueError("Publisher index contains no recognized article cards")
    state["last_index_scan_at"] = _iso(now)
    return _result(rows, state, _coverage(rows, "publisher_dated_index", 1, True,
                   discovered_urls=len(rows), scope="Article cards linked by the publisher's current dated index."),
                   ["Coverage is limited to the publisher's current index; publication dates are the explicit data-date attributes on its article cards."])


def _anthropic(source, state, now, request):
    parser = _AnthropicIndex()
    parser.feed(request(source["url"]).decode("utf-8", errors="replace"))
    urls = list(dict.fromkeys(urljoin(source["url"], url) for url in parser.urls))
    urls = [url for url in urls if urlsplit(url).hostname == urlsplit(source["url"]).hostname]
    if not urls:
        raise ValueError("Anthropic research index has no article cards")
    old = state.get("documents", {})
    pending = [url for url in urls if url not in old]
    failures = state.setdefault("failures", {})
    def due(url):
        retry = _date((failures.get(url) or {}).get("retry_after"))
        return retry is None or retry <= now
    budget = max(1, min(int(source.get("article_budget", 12)) * int(source.get("backfill_pages", 1)), 200))
    # The newest page is rechecked hourly; older known pages rotate after a TTL,
    # including when the publisher does not expose modification metadata.
    stale_before = now - timedelta(hours=float(source.get("recheck_after_hours", 168)))
    stale = [url for url in urls if url in old and ((_date(old[url]) or datetime.min.replace(tzinfo=UTC)) < stale_before)]
    stale.sort(key=lambda url: old.get(url, ""))
    selected = list(dict.fromkeys([url for url in urls[:1] if due(url)] +
                    [url for url in pending if due(url)][:budget] +
                    [url for url in stale if due(url)][:max(1, int(source.get("recheck_budget", 2)))]))
    rows = []
    for url in selected:
        try:
            row = parse_article(request(url), url)
            if not row["title"]:
                raise ValueError("Research page has no article title")
        except Exception as error:
            attempts = min(20, int((failures.get(url) or {}).get("attempts", 0)) + 1)
            failures[url] = {"attempts": attempts, "error": _plain(str(error), 180),
                             "retry_after": _iso(now + timedelta(hours=min(24, 2 ** min(attempts - 1, 5))))}
            continue
        rows.append(row)
        old[url] = _iso(now)
        failures.pop(url, None)
    state["documents"] = old
    pending_count = sum(url not in old for url in urls)
    complete = pending_count == 0
    warnings = ["Publisher index coverage is limited to the articles linked by the current index."]
    missing = sum(not row.get("published_at") for row in rows)
    if missing:
        warnings.append(f"{missing} research pages have no explicit publication date; retained for review.")
    if failures:
        warnings.append(f"{len(failures)} publisher pages remain queued after fetch failures; bounded retries back off up to 24 hours while other pages continue.")
    return _result(rows, state, _coverage(rows, "publisher_index", len(selected) + 1, complete,
                   discovered_urls=len(urls), pending_documents=pending_count,
                   failed_documents=len(failures), degraded=bool(failures), validated_empty=not rows), warnings)


def _bibliography(source, state, now, request):
    """Parse explicit author/year/title citations, without downloading the PDFs."""
    text = request(source["feed_url"]).decode("utf-8", errors="replace")
    rows = {}
    for paragraph in re.findall(r'<p\b[^>]*>([\s\S]*?)</p>', text, re.I):
        paragraph = unescape(paragraph)
        # First linked title following the authors/year is the cited publication;
        # later links to venues, earlier versions and cited works are not aliases.
        match = re.match(r'\s*(.*?)\.\s*((?:19|20)\d{2})\.\s*[“"\u2018\']*\s*<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', paragraph, re.I)
        if not match:
            continue
        authors, year, link, title = match.groups()
        url, title = urljoin(source["url"], link), _plain(title, 500)
        if len(title) < 8 or urlsplit(url).scheme not in {"https", "http"}:
            continue
        arxiv_id = re.search(r'arxiv\.org/(?:abs|pdf)/([^?#]+)', url)
        rows[url] = {"title": title, "url": url, "published_at": None,
                     "publication_year": int(year), "date_precision": "year",
                     "summary": _plain(paragraph), "authors": _authors(_plain(authors).replace(", and ", ", ").replace(" and ", ", ")),
                     "arxiv_id": re.sub(r'v\d+$|\.pdf$', '', arxiv_id.group(1)) if arxiv_id else None,
                     "external_id": url,
                     "links": [{"url": url, "rel": "fulltext", "type": "application/pdf"}] if urlsplit(url).path.lower().endswith(".pdf") else []}
    if not rows:
        raise ValueError("Publisher bibliography contains no recognized publication citations")
    items = list(rows.values())
    state["last_index_scan_at"] = _iso(now)
    return _result(items, state, _coverage(items, "publisher_bibliography", 1, True,
                   discovered_urls=len(items), scope="Publications explicitly listed in the publisher's current bibliography."),
                   ["Bibliography supplies citation years, not exact publication dates; retained with unknown dates until another source supplies a precise date."])


def _fetch(source, state, now, request):
    """Fetch one bounded batch with a resumable, non-mutating checkpoint."""
    state = deepcopy(state) if isinstance(state, dict) else {}
    kind = source.get("kind", "rss")
    if kind in {"alignment_feed", "substack"}:
        return _offset_archive(source, state, now, request, kind)
    if kind == "arxiv":
        checkpoint = deepcopy(state)
        try:
            return _arxiv(source, state, now, request)
        except Exception as error:
            if not source.get("rss_fallback_url"):
                raise
            rows = parse_feed(request(source["rss_fallback_url"]))
            feed_had_items = bool(rows)
            pattern = re.compile(source.get("fallback_match", r"alignment|interpretability|scheming|sandbagging|oversight|reward.hacking"), re.I)
            rows = [row for row in rows if pattern.search(row.get("title", "") + " " + row.get("summary", ""))]
            message = _plain(str(error), 180) or type(error).__name__
            return _result(rows, checkpoint, _coverage(rows, "rss_fallback", 1, False,
                           degraded=True, primary_error=message, archive_available=False,
                           validated_empty=not rows and feed_had_items,
                           fallback_scope="Current arXiv announcement feed for cs.AI, cs.LG, cs.CL, cs.MA and stat.ML, matched locally against discovery keywords."),
                           ["arXiv search API failed: " + message,
                            "Backup RSS discovers only the current announcement window in five configured categories. Search/archive coverage is degraded; saved API backfill cursors are unchanged."])
    if kind == "forum_graphql":
        return _forum(source, state, now, request)
    if kind == "sitemap":
        return _sitemap(source, state, now, request)
    if kind == "anthropic":
        return _anthropic(source, state, now, request)
    if kind == "dated_index":
        return _dated_index(source, state, now, request)
    if kind == "bibliography":
        return _bibliography(source, state, now, request)
    if kind == "openreview":
        try:
            from .scholarly_adapters import fetch_openreview
        except ImportError:
            from scholarly_adapters import fetch_openreview
        return fetch_openreview(source, state, now, request)
    if kind == "rss":
        rows = parse_feed(request(source["feed_url"]))
        extra_pages, pending = 0, 0
        if source.get("enrich_metadata"):
            metadata = state.setdefault("article_metadata", {})
            undated = [row for row in rows if not row.get("published_at") and row["url"] not in metadata]
            budget = max(1, min(int(source.get("article_budget", 10)) * int(source.get("backfill_pages", 1)), 200))
            for row in undated[:budget]:
                if urlsplit(row["url"]).hostname != urlsplit(source["url"]).hostname:
                    raise ValueError("Feed linked to an unexpected metadata host")
                metadata[row["url"]] = parse_article(request(row["url"]), row["url"])
                extra_pages += 1
            pending = max(0, len(undated) - budget)
            for row in rows:
                enriched = metadata.get(row["url"], {})
                for key in ("published_at", "authors", "doi", "arxiv_id", "links"):
                    if not row.get(key) and enriched.get(key):
                        row[key] = enriched[key]
        # Most RSS endpoints publish a finite recent window, with no historical
        # pagination contract. A successful fetch cannot establish archive coverage.
        warnings = ["RSS coverage is the publisher's current feed window; older history may be missing."]
        if pending:
            warnings.append(f"Publication-date metadata for {pending} feed entries is pending and will resume next run.")
        return _result(rows, state, _coverage(rows, "rolling_feed", 1 + extra_pages, False,
                       archive_available=False, pending_documents=pending), warnings)
    raise ValueError("Unsupported source kind: " + str(kind))


def fetch(source, state, now, request):
    """Public adapter contract, including human-readable source scope metadata."""
    result = _fetch(source, state, now, request)
    if source.get("coverage_note"):
        result["coverage"]["description"] = source["coverage_note"]
    return result
