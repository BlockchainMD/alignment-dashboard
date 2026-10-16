#!/usr/bin/env python3
"""Collect a finite, free alignment-research edition. Python standard library only.

Summaries are excerpts supplied by publishers or summaries supplied by Alignment
Feed. Keyword scores are discovery heuristics, not evidence-quality assessments.
No page content is evaluated as code or interpreted as instructions.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html import unescape
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

UTC = timezone.utc
EDITION_TIMEZONE = "America/Detroit"
RETENTION_DAYS = 90
MIN_SCORE = 3
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
HTTP_TIMEOUT = 25
USER_AGENT = "AlignmentDaily/1.0 (+https://github.com/BlockchainMD/alignment-dashboard)"
ROOT = Path(__file__).resolve().parents[1]


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_datetime(value, now=None):
    """Parse ISO, RSS/RFC2822, and explicit human publication dates as UTC.

    Missing dates remain missing; ingestion time is never a publication date.
    When `now` is given, future timestamps are rejected without a grace period.
    """
    if isinstance(value, datetime):
        result = value
    elif not isinstance(value, str) or not value.strip():
        return None
    else:
        value = value.strip()
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                result = parsedate_to_datetime(value)
            except (TypeError, ValueError, IndexError, OverflowError):
                result = None
                for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y"):
                    try:
                        result = datetime.strptime(value, fmt)
                        break
                    except ValueError:
                        continue
                if result is None:
                    return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    result = result.astimezone(UTC)
    if now is not None and result > now.astimezone(UTC):
        return None
    return result


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template", "noscript"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "div", "br", "li", "h1", "h2", "h3", "section"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "template", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean_text(value, max_length=700):
    """Return plain text, bounded for safe display and bounded relevance scoring."""
    if not isinstance(value, str):
        return ""
    parser = _TextParser()
    parser.feed(value[:250_000])
    text = unescape("".join(parser.parts))
    # Aggregator excerpts may contain Markdown rather than HTML.
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_length:
        text = text[:max_length - 1].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
    return text


def canonical_url(url):
    """Validate public HTTP(S) links and normalize known tracking/paper variants."""
    if not isinstance(url, str) or not url or len(url) > 8192:
        return None
    # Backslashes and control characters create browser/parser ambiguities.
    if re.search(r"[\x00-\x20\x7f\\]", url):
        return None
    try:
        p = urlsplit(unescape(url))
        if p.scheme.lower() not in {"http", "https"} or not p.hostname or p.username or p.password:
            return None
        host = p.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".localhost", ".internal")) or "." not in host:
            return None
        if "%" in host:
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            # Reject nonstandard numeric IP spellings interpreted as loopback by browsers.
            if re.fullmatch(r"[0-9.]+", host):
                return None
        port = p.port
        if port and port not in {80, 443}:
            return None
        host = host.encode("idna").decode("ascii")
        if not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host:
            return None
        path = p.path or "/"
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
        if host in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            m = re.fullmatch(r"/(?:abs|pdf)/((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7}))(?:v\d+)?(?:\.pdf)?/?", path, re.I)
            if m:
                return "https://arxiv.org/abs/" + m.group(1)
        return urlunsplit((p.scheme.lower(), host, path.rstrip("/") or "/", urlencode(query), ""))
    except (ValueError, UnicodeError):
        return None


# Every expression contributes at most once. Each topic is capped, so repeating
# keywords or long publisher content cannot dominate a finite edition.
TOPIC_RULES = {
    "Scheming & deception": [
        (r"\b(?:scheming|sandbagging|deceptive alignment|alignment faking|agentic misalignment|misaligned (?:model|ai|agent)s?)\b", 8),
        (r"\b(?:deception|deceptive|reward hacking|reward seeker|hidden objectives?|sabotag\w*|lie detectors?)\b", 5),
    ],
    "AI control & oversight": [
        (r"\b(?:ai control|loss.of.control|scalable oversight|untrusted (?:model|monitor|agent|advice)s?|monitor evasion)\b", 8),
        (r"\b(?:monitorability|monitoring|oversight|red.team\w*|alignment audit\w*|auditing|containment|corrigib\w*)\b", 4),
    ],
    "Interpretability": [
        (r"\b(?:mechanistic interpretability|interpretability|circuit tracing|activation (?:oracles|steering)|sparse autoencoders?)\b", 7),
        (r"\b(?:chain.of.thought faithfulness|reasoning faithfulness|model internals)\b", 5),
    ],
    "Evaluations & risk": [
        (r"\b(?:dangerous.capabilit\w*|frontier (?:risk|model|safety)|safety eval\w*|alignment eval\w*|risk assess\w*|control assess\w*)\b", 6),
        (r"\b(?:autonomous (?:capabilit\w*|agent)s?|model evaluations?|capability evaluations?|ai risk|catastrophic (?:ai )?risk|existential (?:ai )?risk)\b", 4),
    ],
    "Alignment methods": [
        (r"\b(?:alignment|ai safety|weak.to.strong|constitutional ai|eliciting latent knowledge|honesty elicitation)\b", 4),
        (r"\b(?:training interventions?|robustness to training|model organisms?)\b", 3),
    ],
    "AI R&D & agents": [
        (r"\b(?:automated alignment|automated (?:ai )?research\w*|ai r[&a]d|recursive self.improvement|research automation)\b", 6),
    ],
}

# General ML "alignment" includes image registration, speech/brain decoding and
# representation matching. arXiv is broad, so an alignment keyword alone cannot
# admit a paper. Require a concrete safety topic plus a language-model/AI context.
ARXIV_AI_CONTEXT = re.compile(r"\b(?:large language models?|language models?|llms?|frontier (?:model|ai)s?|ai (?:agent|system|model|control|safety|alignment)s?|artificial intelligence|transformers?|neural networks?|reinforcement learning|policy alignment)\b", re.I)
ARXIV_SAFETY_CONTEXT = re.compile(r"\b(?:ai alignment|mechanistic interpretability|scheming|decept\w*|misalign\w*|reward hacking|reward seeker|goal misgeneralization|scalable oversight|ai control|sandbag\w*|alignment faking|hidden objectives?|monitorability|monitor evasion|alignment audit\w*|safety eval\w*|dangerous.capabilit\w*|catastrophic risk|existential risk|constitutional ai|weak.to.strong|recursive self.improvement|automated (?:alignment|research))\b", re.I)


def classify(title, summary):
    title = clean_text(title, 350).casefold()
    summary = clean_text(summary, 3000).casefold()
    topics, score = [], 0
    for topic, rules in TOPIC_RULES.items():
        value = 0
        for pattern, weight in rules:
            if re.search(pattern, title):
                value += weight + 2
            elif re.search(pattern, summary):
                value += weight
        if value:
            topics.append(topic)
            score += min(value, 14)
    return min(score, 100), topics


def normalize_item(raw, source, now):
    if not isinstance(raw, dict):
        return None
    now = now.astimezone(UTC)
    title = clean_text(raw.get("title"), 350)
    url = canonical_url(raw.get("url"))
    source_url = canonical_url(source.get("url"))
    published = parse_datetime(raw.get("published_at"), now=now)
    if not title or not url or not source_url or not published or published < now - timedelta(days=RETENTION_DAYS):
        return None
    summary = clean_text(raw.get("summary", ""), 850)
    if source.get("id") == "arxiv" or urlsplit(url).hostname == "arxiv.org":
        context = title + " " + summary
        if not ARXIV_AI_CONTEXT.search(context) or not ARXIV_SAFETY_CONTEXT.search(context):
            return None
    score, topics = classify(title, summary)
    if score < MIN_SCORE:
        return None
    priority = source.get("priority", 0)
    priority = min(max(priority, 0), 2) if isinstance(priority, (int, float)) else 0
    kind = raw.get("summary_kind", source.get("summary_kind", "publisher excerpt"))
    if kind not in {"publisher excerpt", "aggregator summary"}:
        kind = "publisher excerpt"
    raw_date = raw.get("published_at")
    day_only = (raw.get("date_precision") == "day" or
                isinstance(raw_date, str) and bool(re.fullmatch(r"\s*(?:\d{4}-\d{2}-\d{2}|[A-Za-z]+ \d{1,2},? \d{4})\s*", raw_date)))
    result = {
        "id": hashlib.sha256(url.encode()).hexdigest()[:20],
        "title": title,
        "url": url,
        "source": clean_text(source.get("name", "Unknown source"), 100),
        "source_url": source_url,
        "published_at": iso(published),
        "date_precision": "day" if day_only else "instant",
        "summary": summary,
        "summary_kind": kind,
        "topics": topics,
        "score": min(score + priority, 100),
    }
    if day_only:
        original_date = raw.get("published_date")
        valid_date = parse_datetime(original_date, now=now)
        result["published_date"] = valid_date.date().isoformat() if valid_date and valid_date.date() == published.date() else published.date().isoformat()
    return result


def _identity(url):
    url = canonical_url(url)
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.hostname in {"www.lesswrong.com", "lesswrong.com", "www.alignmentforum.org", "alignmentforum.org"}:
        match = re.match(r"/posts/([^/]+)", parsed.path)
        if match:
            return "alignment-post:" + match.group(1)
    return url


def deduplicate(items):
    """Prefer publisher excerpts when a primary URL is repeated by aggregators."""
    records = {}
    for item in items:
        key = _identity(item.get("url"))
        if not key:
            continue
        previous = records.get(key)
        rank = lambda row: (row.get("summary_kind") == "publisher excerpt", bool(row.get("summary")), row.get("score", 0))
        if previous is None or rank(item) >= rank(previous):
            records[key] = item
    return sorted(records.values(), key=lambda row: (row["published_at"], row["score"], row["id"]), reverse=True)


class _PublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if not canonical_url(new_url):
            raise ValueError("Feed redirected to an unsafe URL")
        return super().redirect_request(request, response, code, message, headers, new_url)


def fetch_bytes(url):
    safe_url = canonical_url(url)
    if not safe_url:
        raise ValueError("Feed URL is not a public HTTP(S) URL")
    request = Request(safe_url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/json, text/html, */*"})
    with build_opener(_PublicRedirects()).open(request, timeout=HTTP_TIMEOUT) as response:
        if not canonical_url(response.geturl()):
            raise ValueError("Feed redirected to an unsafe URL")
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("Feed exceeds the 12 MiB response limit")
        return body


def _child(node, *names):
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text
    return ""


def parse_feed(body):
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("XML document declarations are unsupported")
    root = ET.fromstring(body)
    atom = "{http://www.w3.org/2005/Atom}"
    if root.tag == atom + "feed":
        result = []
        for node in root.findall(atom + "entry")[:250]:
            if "/api/errors" in _child(node, atom + "id"):
                raise ValueError("arXiv returned an API error: " + clean_text(_child(node, atom + "summary"), 160))
            link = next((x.get("href") for x in node.findall(atom + "link") if x.get("rel", "alternate") == "alternate"), _child(node, atom + "id"))
            result.append({"title": _child(node, atom + "title"), "url": link,
                           "published_at": _child(node, atom + "published"),
                           "summary": _child(node, atom + "summary", atom + "content")})
        return result
    nodes = root.findall("./channel/item")
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("Response is not a supported RSS/Atom feed")
    return [{"title": _child(node, "title"), "url": _child(node, "link", "guid"),
             "published_at": _child(node, "pubDate", "{http://purl.org/dc/elements/1.1/}date"),
             "summary": _child(node, "description", "{http://purl.org/rss/1.0/modules/content/}encoded")}
            for node in nodes[:250]]


class _AnthropicIndex(HTMLParser):
    """Extract only article cards from the publisher's server-rendered index."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.card = None
        self.field = None
        self.field_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "a" and ("note" in classes or "paper" in classes):
            self.card = {"url": attrs.get("href"), "title": "", "summary": ""}
        if self.card is not None:
            if tag == "h3":
                self.field, self.field_depth = "title", 1
            elif tag == "div" and "description" in classes:
                self.field, self.field_depth = "summary", 1
            elif self.field:
                self.field_depth += 1

    def handle_endtag(self, tag):
        if self.card is not None:
            if tag == "a":
                self.items.append(self.card)
                self.card, self.field = None, None
            elif self.field:
                self.field_depth -= 1
                if self.field_depth <= 0:
                    self.field = None

    def handle_data(self, data):
        if self.card is not None and self.field:
            self.card[self.field] += data + " "


def _anthropic_date(body):
    text = body.decode("utf-8", errors="replace")
    # Accept explicit publisher metadata first, then the article's byline only.
    for pattern in (r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|date|datePublished)["\'][^>]+content=["\']([^"\']+)',
                    r'"datePublished"\s*:\s*"([^"]+)"',
                    r'<time[^>]+datetime=["\']([^"\']+)'):
        match = re.search(pattern, text, re.I)
        if match and parse_datetime(match.group(1)):
            return match.group(1)
    for pattern in (r'<div[^>]+class="section-authors"[^>]*>([\s\S]{0,5000}?)</div>\s*</div>',
                    r'<div[^>]+class="d-byline"[^>]*>([\s\S]{0,6000}?)</div>\s*</div>',
                    r'<d-byline[^>]*>([\s\S]*?)</d-byline>'):
        match = re.search(pattern, text, re.I)
        if match:
            date = re.search(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+20\d{2}\b", clean_text(match.group(1), 6000))
            if date:
                return date.group(0)
    # Distill articles often render a Published label in their author block.
    match = re.search(r"(?:Published|Publication date)\s*</[^>]+>\s*<(?:p|div|span)[^>]*>([^<]+)", text, re.I)
    return match.group(1).strip() if match else None


def fetch_source(source):
    kind = source.get("kind", "rss")
    body = fetch_bytes(source.get("feed_url", source["url"]))
    if kind == "alignment_feed":
        payload = json.loads(body)
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError("Alignment Feed response has no article list")
        return [{"title": row.get("title"), "url": row.get("link"),
                 "published_at": row.get("published_at"),
                 "summary": row.get("summary") or row.get("text_start", ""),
                 "summary_kind": "aggregator summary"} for row in rows[:250] if isinstance(row, dict)]
    if kind == "anthropic":
        parser = _AnthropicIndex()
        parser.feed(body.decode("utf-8", errors="replace"))
        if not parser.items:
            raise ValueError("Anthropic article index structure changed")

        def article(row):
            row["url"] = urljoin(source["url"], row["url"])
            # Only fetch the publisher's own article pages. Other cards remain
            # discoverable through RSS/arXiv without scraping external domains.
            if urlsplit(row["url"]).hostname != "alignment.anthropic.com":
                return None
            row["published_at"] = _anthropic_date(fetch_bytes(row["url"]))
            return row if row["published_at"] else None

        with ThreadPoolExecutor(max_workers=4) as executor:
            rows = list(executor.map(article, parser.items[:source.get("max_articles", 14)]))
        result = [row for row in rows if row]
        if not result:
            raise ValueError("No explicit article publication dates found")
        return result
    if kind == "rss":
        return parse_feed(body)
    raise ValueError("Unsupported source kind: " + str(kind))


def collect(sources, previous=None, now=None, fetcher=None):
    """Return one edition, preserving valid previous research during outages.

    `fetcher(source) -> list[raw item]` is the offline testing seam. Source status
    reports collection success separately from whether any item passed filtering.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    previous = previous if isinstance(previous, dict) else {}
    fetcher = fetcher or fetch_source
    checked = iso(now)
    prior_health = {row.get("id"): row for row in previous.get("sources", []) if isinstance(row, dict)}
    source_by_name = {row["name"]: row for row in sources}
    all_items = []
    retained = 0
    for raw in previous.get("items", []):
        if not isinstance(raw, dict):
            continue
        source = source_by_name.get(raw.get("source"), {"name": raw.get("source"), "url": raw.get("source_url")})
        item = normalize_item(raw, source, now)
        if item:
            all_items.append(item)
            retained += 1

    health, errors = [], []
    fetched = accepted = successes = 0
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(sources)))) as executor:
        jobs = {executor.submit(fetcher, source): source for source in sources}
        for job in as_completed(jobs):
            source = jobs[job]
            old = prior_health.get(source["id"], {})
            state = {"id": source["id"], "name": source["name"], "url": source["url"],
                     "status": "error", "checked_at": checked,
                     "last_success_at": old.get("last_success_at"), "item_count": 0, "error": None}
            try:
                rows = job.result()
                if not isinstance(rows, list):
                    raise ValueError("Source parser did not return an article list")
                fetched += len(rows)
                items = [item for row in rows if (item := normalize_item(row, source, now))]
                all_items.extend(items)
                accepted += len(items)
                successes += 1
                state.update(status="ok", last_success_at=checked, item_count=len(items))
            except Exception as exc:
                # Errors describe source health only; no page content or stack
                # traces are included in the public artifact.
                error = clean_text(str(exc), 220) or type(exc).__name__
                state["error"] = error
                errors.append({"source_id": source["id"], "source": source["name"], "message": error})
            health.append(state)
    order = {row["id"]: index for index, row in enumerate(sources)}
    health.sort(key=lambda row: order[row["id"]])
    errors.sort(key=lambda row: order[row["source_id"]])
    items = deduplicate(all_items)
    # The cap bounds the static artifact even if upstream feeds grow indefinitely.
    items = items[:1000]
    return {
        "generated_at": checked,
        "last_successful_update": checked if successes else previous.get("last_successful_update"),
        "edition_date": now.astimezone(ZoneInfo(EDITION_TIMEZONE)).date().isoformat(),
        "timezone": EDITION_TIMEZONE,
        "items": items,
        "sources": health,
        "errors": errors,
        "stats": {"total_items": len(items), "fetched_items": fetched, "accepted_items": accepted,
                  "retained_items": retained, "successful_sources": successes, "total_sources": len(sources),
                  "retention_days": RETENTION_DAYS, "minimum_relevance_score": MIN_SCORE,
                  "duplicates_removed": len(all_items) - len(deduplicate(all_items)),
                  "newest_publication": items[0]["published_at"] if items else None},
    }


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".edition-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "site/data/edition.json")
    parser.add_argument("--config", type=Path, default=ROOT / "config/sources.json")
    parser.add_argument("--now", help="Optional ISO clock for reproducible verification")
    args = parser.parse_args(argv)
    now = parse_datetime(args.now) if args.now else datetime.now(UTC)
    if now is None:
        parser.error("--now must be a valid ISO/RFC3339 timestamp")
    sources = json.loads(args.config.read_text(encoding="utf-8"))
    sources = sources["sources"] if isinstance(sources, dict) else sources
    if not isinstance(sources, list) or not sources:
        parser.error("Source configuration must contain a nonempty sources list")
    previous = {}
    if args.output.exists():
        try:
            previous = json.loads(args.output.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            print("Previous edition could not be read; rebuilding from public sources.", file=sys.stderr)
    edition = collect(sources, previous=previous, now=now)
    atomic_write(args.output, edition)
    print(f"Wrote {len(edition['items'])} research items; {edition['stats']['successful_sources']}/{len(sources)} sources collected successfully.")
    for error in edition["errors"]:
        print(f"{error['source']}: {error['message']}", file=sys.stderr)
    return 0 if edition["stats"]["successful_sources"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
