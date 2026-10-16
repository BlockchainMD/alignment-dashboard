#!/usr/bin/env python3
"""Incrementally archive public alignment research without dropping old records.

The archive retains discovery candidates, source provenance and metadata revisions.
It stores bounded publisher excerpts, never a public full-text mirror. Coverage is
reported per source: collection cannot prove that every publication is indexed.
All remote content is untrusted data and is never executed.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import gzip
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html import unescape
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import parse_qsl, urlencode, unquote, urljoin, urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

UTC = timezone.utc
EDITION_TIMEZONE = "America/Detroit"
RETENTION_DAYS = None  # The canonical archive has no age-based deletion policy.
MIN_SCORE = 3
MAX_RESPONSE_BYTES = 24 * 1024 * 1024
MAX_CACHE_BYTES = 256 * 1024 * 1024
HTTP_TIMEOUT = 25
USER_AGENT = "AlignmentDaily/2.0 (+https://github.com/BlockchainMD/alignment-dashboard)"
ROOT = Path(__file__).resolve().parents[1]
# Support normal CLI execution and importlib-based offline verification.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

ARCHIVE_SCHEMA_VERSION = 2


class ArchiveBusyError(RuntimeError):
    """Another process owns the collection transaction for this archive."""


@contextmanager
def archive_lock(archive_path, lock_dir=None):
    """Hold one cross-process archive transaction without waiting or losing work.

    The stable lock file is intentionally left in place after unlocking: deleting
    it could let a third process lock a new inode while another waits on the old
    one. Only the OS lock indicates ownership; a stale PID file is harmless.
    """
    archive_path = Path(archive_path).resolve()
    directory = Path(lock_dir) if lock_dir is not None else ROOT / ".cache/alignment-locks"
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(archive_path).encode()).hexdigest()
    stream = (directory / (key + ".lock")).open("a+", encoding="utf-8")
    locked = False
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as exc:
            raise ArchiveBusyError("Another collector is already running for " + str(archive_path) + "; no archive or public files were changed.") from exc
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()) + "\n")
        stream.flush()
        yield
    finally:
        if locked:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


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


def normalize_doi(value):
    if not isinstance(value, str):
        return None
    value = unquote(value.strip()).removeprefix("doi:")
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    return value.casefold() if re.fullmatch(r"10\.\d{4,9}/[^\s<>\"]+", value, re.I) else None


def normalize_arxiv_id(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    value = re.sub(r"^arxiv:\s*", "", value, flags=re.I)
    value = re.sub(r"^https?://(?:export\.|www\.)?arxiv\.org/(?:abs|pdf)/", "", value, flags=re.I)
    value = value.split("?", 1)[0].split("#", 1)[0]
    value = re.sub(r"(?:v\d+)?(?:\.pdf)?/?$", "", value, flags=re.I)
    return value if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})", value, re.I) else None


def _authors(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    result = []
    for author in value[:200]:
        name = clean_text(author.get("name") if isinstance(author, dict) else author, 160)
        if name and name not in result:
            result.append(name)
    return result


def _trusted_alignment_source(source):
    return (source.get("scope") in {"alignment", "alignment_research", "trusted_alignment"}
            or source.get("research_scope") == "alignment" or source.get("alignment_research") is True)


def _content_hash(item):
    fields = {key: item.get(key) for key in (
        "title", "url", "published_at", "published_date", "publication_year", "summary", "summary_kind",
        "authors", "doi", "arxiv_id", "updated_at", "external_id", "version")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def normalize_item(raw, source, now):
    """Retain valid metadata even if publication date or relevance is uncertain.

    Keywords organize candidates after ingestion; they never delete discovered
    records. Future/invalid dates remain unknown rather than becoming today.
    """
    if not isinstance(raw, dict) or not isinstance(source, dict):
        return None
    now = now.astimezone(UTC)
    observed = iso(now)
    title = clean_text(raw.get("title"), 350)
    url = canonical_url(raw.get("url"))
    source_url = canonical_url(source.get("url") or raw.get("source_url"))
    if not title or not url or not source_url:
        return None
    published = parse_datetime(raw.get("published_at"), now=now)
    raw_year = raw.get("publication_year")
    publication_year = (int(raw_year) if not isinstance(raw_year, bool) and
                        isinstance(raw_year, (int, str)) and re.fullmatch(r"\d{4}", str(raw_year)) and
                        1000 <= int(raw_year) <= now.year else None)
    summary = clean_text(raw.get("summary", raw.get("excerpt", "")), 850)
    score, topics = classify(title, summary)
    context = title + " " + summary
    trusted = _trusted_alignment_source(source)
    is_arxiv = source.get("kind") == "arxiv" or source.get("id") == "arxiv" or urlsplit(url).hostname == "arxiv.org"
    matches = score >= MIN_SCORE
    if is_arxiv and not trusted:
        matches = bool(ARXIV_AI_CONTEXT.search(context) and ARXIV_SAFETY_CONTEXT.search(context))
    relevance = "relevant" if trusted or matches else "candidate"
    # Archived relevance decisions must survive a source disappearing from config.
    if raw.get("relevance") == "relevant" and not source.get("scope"):
        relevance = "relevant"
    priority = source.get("priority", 0)
    priority = min(max(priority, 0), 2) if isinstance(priority, (int, float)) else 0
    kind = raw.get("summary_kind", source.get("summary_kind", "publisher excerpt"))
    if kind not in {"publisher excerpt", "aggregator summary"}:
        kind = "publisher excerpt"
    raw_date = raw.get("published_at")
    day_only = bool(published and (raw.get("date_precision") == "day" or
                    isinstance(raw_date, str) and re.fullmatch(r"\s*(?:\d{4}-\d{2}-\d{2}|[A-Za-z]+ \d{1,2},? \d{4})\s*", raw_date)))
    first_seen = parse_datetime(raw.get("first_seen") or raw.get("discovered_at"), now=now)
    last_seen = parse_datetime(raw.get("last_seen"), now=now)
    links = []
    for link in raw.get("links", []) if isinstance(raw.get("links"), list) else []:
        link = canonical_url(link.get("url") or link.get("href")) if isinstance(link, dict) else canonical_url(link)
        if link and link not in links:
            links.append(link)
    doi = normalize_doi(raw.get("doi"))
    arxiv_id = normalize_arxiv_id(raw.get("arxiv_id"))
    for link in [url, *links]:
        if not doi and urlsplit(link).hostname in {"doi.org", "dx.doi.org"}:
            doi = normalize_doi(link)
        if not arxiv_id:
            arxiv_id = normalize_arxiv_id(link)
    source_id = clean_text(source.get("id") or raw.get("source_id"), 100)
    source_name = clean_text(source.get("name") or raw.get("source") or "Unknown source", 100)
    result = {
        "id": clean_text(raw.get("id"), 80) or hashlib.sha256(url.encode()).hexdigest()[:20],
        "title": title, "url": url, "source": source_name, "source_id": source_id,
        "source_url": source_url, "published_at": iso(published) if published else None,
        "date_precision": ("day" if day_only else "instant") if published else "year" if publication_year else "unknown",
        "summary": summary, "summary_kind": kind, "topics": topics,
        "score": min(score + priority, 100), "relevance": relevance,
        "relevance_reason": "trusted alignment source" if trusted else "topic heuristic" if matches else "discovery candidate; needs review",
        "authors": _authors(raw.get("authors")),
        "first_seen": iso(first_seen) if first_seen else observed,
        "discovered_at": iso(first_seen) if first_seen else observed,
        "last_seen": iso(last_seen) if last_seen else observed,
        "doi": doi, "arxiv_id": arxiv_id, "links": links,
        "aliases": [value for value in raw.get("aliases", []) if isinstance(value, str) and 0 < len(value) <= 80] if isinstance(raw.get("aliases"), list) else [],
        "updated_at": iso(updated) if (updated := parse_datetime(raw.get("updated_at"), now=now)) else None,
    }
    if publication_year:
        result["publication_year"] = published.year if published else publication_year
    if day_only:
        original_date = parse_datetime(raw.get("published_date"), now=now)
        result["published_date"] = original_date.date().isoformat() if original_date and original_date.date() == published.date() else published.date().isoformat()
    if arxiv_id and not raw.get("version"):
        for value in (raw.get("arxiv_id"), raw.get("url")):
            match = re.search(r"v(\d+)(?:\.pdf)?(?:[?#].*)?$", value) if isinstance(value, str) else None
            if match:
                result["version"] = match.group(1)
                break
    for key in ("version", "external_id", "publication_type", "language"):
        if raw.get(key):
            result[key] = clean_text(str(raw[key]), 200)
    provenance = []
    for row in raw.get("provenance", []) if isinstance(raw.get("provenance"), list) else []:
        if not isinstance(row, dict):
            continue
        article_url = canonical_url(row.get("article_url"))
        home_url = canonical_url(row.get("url"))
        if article_url and home_url:
            provenance.append({"source_id": clean_text(row.get("source_id"), 100),
                               "name": clean_text(row.get("name"), 100), "url": home_url,
                               "article_url": article_url,
                               "found_at": iso(found) if (found := parse_datetime(row.get("found_at"), now=now)) else result["first_seen"],
                               "last_seen": iso(seen) if (seen := parse_datetime(row.get("last_seen"), now=now)) else result["last_seen"]})
    if not provenance:
        provenance = [{"source_id": source_id, "name": source_name, "url": source_url,
                       "article_url": url, "found_at": result["first_seen"], "last_seen": result["last_seen"]}]
    result["provenance"] = provenance
    result["content_hash"] = _content_hash(result)
    result["revisions"] = [copy.deepcopy(row) for row in raw.get("revisions", [])
                           if isinstance(row, dict) and isinstance(row.get("content_hash"), str)] if isinstance(raw.get("revisions"), list) else []
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
    arxiv_id = normalize_arxiv_id(url)
    if arxiv_id:
        return "arxiv:" + arxiv_id
    if parsed.hostname in {"doi.org", "dx.doi.org"}:
        doi = normalize_doi(url)
        if doi:
            return "doi:" + doi
    return url


def _identities(item):
    keys = {_identity(item.get("url"))}
    if doi := normalize_doi(item.get("doi")):
        keys.add("doi:" + doi)
    if arxiv_id := normalize_arxiv_id(item.get("arxiv_id")):
        keys.add("arxiv:" + arxiv_id)
    for link in item.get("links", []):
        # Atom entries may include shared license/help URLs. Only paper
        # identifiers, not arbitrary related links, establish an identity.
        identity = _identity(link)
        if identity and identity.startswith(("arxiv:", "doi:")):
            keys.add(identity)
    for row in item.get("provenance", []):
        identity = _identity(row.get("article_url")) if isinstance(row, dict) else None
        if identity:
            keys.add(identity)
    keys.discard(None)
    return keys


def _merge_items(previous, incoming):
    """Merge explicit identities only; titles never prove two works are equal."""
    def rank(row):
        version = re.search(r"\d+", str(row.get("version") or ""))
        return (row.get("summary_kind") == "publisher excerpt",
                int(version.group(0)) if version else 0, row.get("updated_at") or "", bool(row.get("summary")),
                row.get("last_seen") or "", row.get("score", 0), row.get("source_id") or "", row.get("url") or "")
    preferred = incoming if rank(incoming) >= rank(previous) else previous
    other = previous if preferred is incoming else incoming
    result = copy.deepcopy(preferred)
    # Record identity is stable across corrections and publisher cross-posts.
    result["id"] = previous["id"]
    result["aliases"] = sorted((set(previous.get("aliases", [])) | set(incoming.get("aliases", [])) | {previous["id"], incoming["id"]}) - {result["id"]})
    first = min(filter(None, [previous.get("first_seen"), incoming.get("first_seen")]), default=None)
    result["first_seen"] = result["discovered_at"] = first
    result["last_seen"] = max(previous.get("last_seen") or "", incoming.get("last_seen") or "")
    for key in ("doi", "arxiv_id", "published_at", "published_date", "publication_year", "updated_at", "authors", "external_id", "version"):
        if not result.get(key) and other.get(key):
            result[key] = copy.deepcopy(other[key])
    if result.get("published_at"):
        if result.get("date_precision") in {"unknown", "year"}:
            result["date_precision"] = other.get("date_precision", "instant")
            if result["date_precision"] in {"unknown", "year"}:
                result["date_precision"] = "instant"
        if result.get("publication_year"):
            result["publication_year"] = parse_datetime(result["published_at"]).year
    elif result.get("publication_year"):
        result["date_precision"] = "year"
    result["relevance"] = "relevant" if "relevant" in {previous.get("relevance"), incoming.get("relevance")} else "candidate"
    result["topics"] = sorted(set(previous.get("topics", [])) | set(incoming.get("topics", [])))
    result["links"] = sorted(set(previous.get("links", [])) | set(incoming.get("links", [])) | {previous["url"], incoming["url"]})
    provenance = {}
    for row in [*previous.get("provenance", []), *incoming.get("provenance", [])]:
        key = (row.get("source_id"), row.get("article_url"))
        if key in provenance:
            old = provenance[key]
            old["found_at"] = min(old.get("found_at") or first or "", row.get("found_at") or first or "")
            old["last_seen"] = max(old.get("last_seen") or "", row.get("last_seen") or "")
        else:
            provenance[key] = copy.deepcopy(row)
    result["provenance"] = sorted(provenance.values(), key=lambda row: (row.get("source_id") or "", row.get("article_url") or ""))
    revisions = {}
    for row in [*previous.get("revisions", []), *incoming.get("revisions", [])]:
        revisions[row["content_hash"]] = copy.deepcopy(row)
    result["content_hash"] = _content_hash(result)
    for observed_item in (previous, incoming):
        observed_hash = observed_item.get("content_hash") or _content_hash(observed_item)
        if observed_hash != result["content_hash"]:
            revisions.setdefault(observed_hash, {"content_hash": observed_hash,
                "observed_at": observed_item.get("last_seen") or first, "updated_at": observed_item.get("updated_at")})
    revisions.pop(result["content_hash"], None)
    result["revisions"] = sorted(revisions.values(), key=lambda row: row.get("observed_at") or "")
    return result


def _item_sort_key(item):
    return (item.get("published_at") or item.get("discovered_at") or "", item.get("score", 0), item.get("id", ""))


def deduplicate(items):
    """Merge canonical URLs, DOI/arXiv IDs and cross-post provenance losslessly."""
    records, identity_index = {}, {}
    for item in items:
        if not isinstance(item, dict):
            continue
        keys = _identities(item)
        if not keys:
            continue
        matches = sorted({identity_index[key] for key in keys if key in identity_index})
        if matches:
            record_key = matches[0]
            current = records[record_key]
            for key in matches[1:]:
                current = _merge_items(current, records.pop(key))
                for identity, target in list(identity_index.items()):
                    if target == key:
                        identity_index[identity] = record_key
            current = _merge_items(current, item)
        else:
            record_key = item["id"]
            # Untrusted source IDs must not overwrite an unrelated article.
            if record_key in records:
                record_key = hashlib.sha256(item["url"].encode()).hexdigest()
                item = {**item, "id": record_key}
            current = copy.deepcopy(item)
        records[record_key] = current
        for key in _identities(current):
            identity_index[key] = record_key
    return sorted(records.values(), key=_item_sort_key, reverse=True)


class _PublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if not canonical_url(new_url):
            raise ValueError("Feed redirected to an unsafe URL")
        return super().redirect_request(request, response, code, message, headers, new_url)


class CachedNetwork:
    """Bounded retries and validators backed by an untracked local HTTP cache."""
    def __init__(self, directory=None, retries=3, opener=None, sleep=time.sleep):
        self.directory = Path(directory) if directory else None
        self.retries = max(1, min(int(retries), 5))
        self.opener = opener or build_opener(_PublicRedirects())
        self.sleep = sleep
        self.lock = threading.Lock()
        self.index = {}
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                candidate = json.loads((self.directory / "index.json").read_text(encoding="utf-8"))
                if isinstance(candidate, dict):
                    self.index = {key: value for key, value in candidate.items()
                                  if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key) and isinstance(value, dict)}
            except (OSError, ValueError):
                pass

    def __call__(self, url):
        safe_url = canonical_url(url)
        if not safe_url:
            raise ValueError("Source URL is not a public HTTP(S) URL")
        key = hashlib.sha256(safe_url.encode()).hexdigest()
        body_path = self.directory / (key + ".body") if self.directory else None
        with self.lock:
            cached = copy.deepcopy(self.index.get(key, {}))
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Accept": "application/rss+xml, application/atom+xml, application/json, text/html, */*"}
        cached_body = None
        if body_path and body_path.is_file():
            try:
                candidate = body_path.read_bytes() if body_path.stat().st_size <= MAX_RESPONSE_BYTES else None
                if candidate is not None and cached.get("cache_format") == 2 and cached.get("body_sha256") == hashlib.sha256(candidate).hexdigest():
                    cached_body = candidate
            except OSError:
                pass
        if cached_body is not None:
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = cached["last_modified"]
        for attempt in range(self.retries):
            try:
                request = Request(safe_url, headers=headers)
                with self.opener.open(request, timeout=HTTP_TIMEOUT) as response:
                    if not canonical_url(response.geturl()):
                        raise ValueError("Source redirected to an unsafe URL")
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ValueError("Response exceeds the 24 MiB response limit; source needs smaller pages")
                    encoding = (response.headers.get("Content-Encoding") or "identity").strip().lower()
                    if encoding in {"gzip", "x-gzip"}:
                        # Some publishers send compression even for identity.
                        # Limit decompression itself to avoid materializing an
                        # unbounded expanded response before checking size.
                        with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                            body = stream.read(MAX_RESPONSE_BYTES + 1)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise ValueError("Expanded gzip response exceeds the 24 MiB limit; source needs smaller pages")
                    elif encoding not in {"", "identity"}:
                        raise ValueError("Unsupported response Content-Encoding: " + encoding[:40])
                    if body_path:
                        # Validators and their corresponding body are committed together
                        # under the lock; source checkpoints are committed separately.
                        with self.lock:
                            temporary = body_path.with_suffix(".tmp")
                            temporary.write_bytes(body)
                            temporary.replace(body_path)
                            self.index[key] = {"cache_format": 2, "url": safe_url, "etag": response.headers.get("ETag"),
                                               "last_modified": response.headers.get("Last-Modified"),
                                               "body_sha256": hashlib.sha256(body).hexdigest(),
                                               "last_access": time.time(), "bytes": len(body)}
                    return body
            except HTTPError as exc:
                exc.close()
                if exc.code == 304 and cached_body is not None:
                    with self.lock:
                        if key in self.index:
                            self.index[key]["last_access"] = time.time()
                    return cached_body
                if exc.code not in {408, 425, 429, 500, 502, 503, 504} or attempt + 1 >= self.retries:
                    raise
                delay = min(2 ** attempt, 8)
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after and retry_after.isdigit():
                    delay = min(max(delay, int(retry_after)), 15)
            except (URLError, TimeoutError, ConnectionError, OSError):
                if attempt + 1 >= self.retries:
                    raise
                delay = min(2 ** attempt, 8)
            if urlsplit(safe_url).hostname in {"arxiv.org", "export.arxiv.org"}:
                delay = max(delay, 3)
            self.sleep(delay)
        raise RuntimeError("Source request exhausted retries")

    def save(self):
        if self.directory:
            with self.lock:
                # The cache is disposable and bounded; source records/cursors
                # remain in the authoritative archive independently.
                entries = []
                for key, metadata in list(self.index.items()):
                    if not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(metadata, dict):
                        self.index.pop(key, None)
                        continue
                    path = self.directory / (key + ".body")
                    if not path.is_file():
                        self.index.pop(key, None)
                        continue
                    try:
                        accessed = float(metadata.get("last_access", 0))
                    except (TypeError, ValueError):
                        accessed = 0
                    entries.append((accessed, key, path.stat().st_size))
                total = sum(entry[2] for entry in entries)
                for _, key, size in sorted(entries):
                    if total <= MAX_CACHE_BYTES:
                        break
                    (self.directory / (key + ".body")).unlink(missing_ok=True)
                    self.index.pop(key, None)
                    total -= size
                atomic_write(self.directory / "index.json", self.index)


def fetch_bytes(url):
    return CachedNetwork()(url)


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
        for node in root.findall(atom + "entry"):
            if "/api/errors" in _child(node, atom + "id"):
                raise ValueError("arXiv returned an API error: " + clean_text(_child(node, atom + "summary"), 160))
            link = next((x.get("href") for x in node.findall(atom + "link") if x.get("rel", "alternate") == "alternate"), _child(node, atom + "id"))
            result.append({"title": _child(node, atom + "title"), "url": link,
                           "published_at": _child(node, atom + "published"),
                           "updated_at": _child(node, atom + "updated"),
                           "authors": [_child(author, atom + "name") for author in node.findall(atom + "author")],
                           "links": [link.get("href") for link in node.findall(atom + "link") if link.get("href")],
                           "doi": _child(node, "{http://arxiv.org/schemas/atom}doi"),
                           "summary": _child(node, atom + "summary", atom + "content")})
        return result
    nodes = root.findall("./channel/item")
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("Response is not a supported RSS/Atom feed")
    return [{"title": _child(node, "title"), "url": _child(node, "link", "guid"),
             "published_at": _child(node, "pubDate", "{http://purl.org/dc/elements/1.1/}date"),
             "authors": [_child(node, "{http://purl.org/dc/elements/1.1/}creator", "author")],
             "summary": _child(node, "description", "{http://purl.org/rss/1.0/modules/content/}encoded")}
            for node in nodes]


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


def fetch_source(source, state=None, now=None, request=None):
    from source_adapters import fetch
    return fetch(source, state or {}, now or datetime.now(UTC), request or fetch_bytes)


def collect(sources, previous=None, now=None, fetcher=None, source_states=None, backfill_pages=3):
    """Collect without deleting history; checkpoints advance only on valid success.

    Offline seam ``fetcher(source)`` accepts a list or an adapter outcome dict.
    Real adapters receive persistent state and a bounded page budget. Partial
    failures keep every prior article and checkpoint and expose source warnings.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    previous = previous if isinstance(previous, dict) else {}
    checked = iso(now)
    old_states = copy.deepcopy(source_states if source_states is not None else previous.get("source_states", {}))
    old_states = old_states if isinstance(old_states, dict) else {}
    next_states = copy.deepcopy(old_states)
    prior_health = {row.get("id"): row for row in previous.get("sources", []) if isinstance(row, dict)}
    source_by_id = {row["id"]: row for row in sources}
    source_by_name = {row["name"]: row for row in sources}
    all_items = []
    for raw in previous.get("items", []):
        if not isinstance(raw, dict):
            continue
        source = source_by_id.get(raw.get("source_id")) or source_by_name.get(raw.get("source")) or {
            "id": raw.get("source_id"), "name": raw.get("source"), "url": raw.get("source_url")}
        retained_raw = raw
        if not raw.get("first_seen") and not raw.get("discovered_at"):
            migrated_observation = parse_datetime(previous.get("generated_at"), now=now)
            if migrated_observation:
                retained_raw = {**raw, "first_seen": iso(migrated_observation), "last_seen": raw.get("last_seen") or iso(migrated_observation)}
        item = normalize_item(retained_raw, source, now)
        if item:
            all_items.append(item)
    retained = len(all_items)
    known_source_ids = {row.get("source_id") for row in all_items}
    known_source_ids.update(provenance.get("source_id") for row in all_items for provenance in row.get("provenance", []))
    health, errors = [], []
    fetched = accepted = successes = suspicious = 0
    for source in sources:
        if not isinstance(source.get("id"), str) or not canonical_url(source.get("url")):
            raise ValueError("Every source needs a stable id and a public HTTP(S) homepage")

    def run(source):
        if fetcher:
            return fetcher(copy.deepcopy(source))
        return fetch_source({**source, "backfill_pages": backfill_pages},
                            copy.deepcopy(old_states.get(source["id"], {})), now=now)

    with ThreadPoolExecutor(max_workers=min(6, max(1, len(sources)))) as executor:
        jobs = {executor.submit(run, source): source for source in sources}
        for job in as_completed(jobs):
            source = jobs[job]
            old = prior_health.get(source["id"], {})
            state = {"id": source["id"], "name": source["name"], "url": source["url"],
                     "status": "error", "checked_at": checked, "last_success_at": old.get("last_success_at"),
                     "last_failure_at": old.get("last_failure_at"), "failure_streak": old.get("failure_streak", 0),
                     "last_nonempty_at": old.get("last_nonempty_at"),
                     "item_count": 0, "parsed_count": 0, "error": None, "warnings": [],
                     "scope": source.get("scope", "mixed"), "coverage": copy.deepcopy(old.get("coverage", {}))}
            try:
                outcome = job.result()
                if isinstance(outcome, list):
                    outcome = {"items": outcome, "state": old_states.get(source["id"], {}),
                               "coverage": {"mode": "feed_window", "backfill_complete": False}, "warnings": []}
                if not isinstance(outcome, dict) or not isinstance(outcome.get("items"), list):
                    raise ValueError("Source parser did not return an article list")
                rows = outcome["items"]
                fetched += len(rows)
                items = [item for row in rows if (item := normalize_item(row, source, now))]
                state["parsed_count"] = len(rows)
                state["item_count"] = len(items)
                state["warnings"] = [clean_text(message, 240) for message in outcome.get("warnings", []) if isinstance(message, str)]
                state["coverage"] = outcome.get("coverage") if isinstance(outcome.get("coverage"), dict) else {}
                # Incremental adapters can validate an unchanged index without
                # returning articles. They must explicitly distinguish that case
                # from a rolling feed unexpectedly becoming empty.
                validated_empty = state["coverage"].get("validated_empty") is True
                empty_suspicious = not rows and not validated_empty and (source["id"] in known_source_ids or old.get("parsed_count", old.get("item_count", 0)) > 0 or source.get("expect_nonempty", False))
                if empty_suspicious:
                    suspicious += 1
                    state["warnings"].append("No records parsed; source may be empty or its format may have changed. Checkpoint retained.")
                    prior_coverage = copy.deepcopy(old.get("coverage", {}))
                    # Preserve the prior completeness/checkpoint while still
                    # exposing a degraded backup transport on this attempt.
                    prior_coverage.update({key: value for key, value in state["coverage"].items()
                                           if key in {"degraded", "primary_error", "mode", "description", "history_limit", "fallback_url"}})
                    prior_coverage["suspicious_empty"] = True
                    prior_coverage["backfill_complete"] = bool(old_states.get(source["id"], {}).get("backfill_complete", prior_coverage.get("backfill_complete", False)))
                    prior_coverage["pending_backfill"] = prior_coverage.get("pending_backfill", not prior_coverage["backfill_complete"])
                    state["coverage"] = prior_coverage
                    state["status"] = "warning"
                else:
                    new_state = outcome.get("state", old_states.get(source["id"], {}))
                    if not isinstance(new_state, dict):
                        raise ValueError("Source checkpoint is not an object")
                    # Never advance a checkpoint if all parsed records were malformed.
                    if rows and not items:
                        raise ValueError("Source returned records but none had valid article metadata; checkpoint retained")
                    next_states[source["id"]] = copy.deepcopy(new_state)
                    state["status"] = "ok"
                if state["coverage"].get("degraded") is True:
                    state["status"] = "degraded"
                all_items.extend(items)
                accepted += len(items)
                successes += 1
                state.update(last_success_at=checked, failure_streak=0)
                if rows:
                    state["last_nonempty_at"] = checked
                if rows and len(items) < len(rows):
                    state["warnings"].append(f"{len(rows) - len(items)} records lacked a valid title or public article link.")
                if state["coverage"].get("pending_backfill") or state["coverage"].get("truncated"):
                    state["warnings"].append("Historical coverage is incomplete; later runs retry the backlog when its discovery route is available.")
            except Exception as exc:
                error = clean_text(str(exc), 220) or type(exc).__name__
                state.update(status="error", error=error, last_failure_at=checked,
                             coverage=copy.deepcopy(old.get("coverage", {})),
                             failure_streak=int(old.get("failure_streak", 0)) + 1)
                errors.append({"source_id": source["id"], "source": source["name"], "message": error})
            health.append(state)
    order = {row["id"]: index for index, row in enumerate(sources)}
    health.sort(key=lambda row: order[row["id"]])
    errors.sort(key=lambda row: order[row["source_id"]])
    items = deduplicate(all_items)
    # A targeted recovery retains health for sources outside the requested subset.
    health.extend(copy.deepcopy(row) for row in previous.get("sources", [])
                  if isinstance(row, dict) and row.get("id") not in source_by_id)
    known_publications = [item["published_at"] for item in items if item.get("published_at")]
    return {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "generated_at": checked,
        "last_successful_update": checked if successes else previous.get("last_successful_update"),
        "edition_date": now.astimezone(ZoneInfo(EDITION_TIMEZONE)).date().isoformat(),
        "timezone": EDITION_TIMEZONE, "items": items, "sources": health, "errors": errors,
        "source_states": next_states,
        "coverage": {"completeness": "unverified", "description": "An expanding metadata archive; feeds and search APIs cannot guarantee every published alignment work.",
                     "configured_sources": len(health), "pending_backfill_sources": sum(bool(row.get("coverage", {}).get("pending_backfill")) for row in health),
                     "feed_window_sources": sum(row.get("coverage", {}).get("mode") == "feed_window" for row in health)},
        "stats": {"total_items": len(items), "fetched_items": fetched, "accepted_items": accepted,
                  "retained_items": retained, "successful_sources": successes, "total_sources": len(sources),
                  "retention_days": None, "minimum_relevance_score": None,
                  "relevant_items": sum(row.get("relevance") == "relevant" for row in items),
                  "candidate_items": sum(row.get("relevance") == "candidate" for row in items),
                  "undated_items": sum(not row.get("published_at") and not row.get("publication_year") for row in items),
                  "year_only_items": sum(not row.get("published_at") and bool(row.get("publication_year")) for row in items),
                  "suspicious_empty_sources": suspicious,
                  "duplicates_removed": len(all_items) - len(items),
                  "newest_publication": max(known_publications, default=None)},
    }


# Re-export the atomic writer to keep the established collector API stable.
from archive_store import atomic_write, load_archive, save_archive, write_public_archive


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "site/data/edition.json")
    parser.add_argument("--config", type=Path, default=ROOT / "config/sources.json")
    parser.add_argument("--archive", type=Path, help="Authoritative metadata archive (never automatically reset)")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / ".cache/alignment-http")
    parser.add_argument("--now", help="Optional ISO clock for reproducible verification")
    parser.add_argument("--source", action="append", help="Collect only this configured source id; repeatable")
    parser.add_argument("--backfill-pages", type=int, default=3, help="Historical page budget per source per run (1–50)")
    args = parser.parse_args(argv)
    # Normalize before deciding the archive location: the scheduled workflow
    # passes a relative --output, which must never move private checkpoints into
    # the publicly served site tree.
    args.output = args.output.resolve()
    args.config = args.config.resolve()
    args.cache_dir = args.cache_dir.resolve()
    if args.archive:
        args.archive = args.archive.resolve()
    now = parse_datetime(args.now) if args.now else datetime.now(UTC)
    if now is None:
        parser.error("--now must be a valid ISO/RFC3339 timestamp")
    if not 1 <= args.backfill_pages <= 50:
        parser.error("--backfill-pages must be between 1 and 50")
    sources = json.loads(args.config.read_text(encoding="utf-8"))
    sources = sources["sources"] if isinstance(sources, dict) else sources
    if not isinstance(sources, list) or not sources:
        parser.error("Source configuration must contain a nonempty sources list")
    if args.source:
        unknown = set(args.source) - {source["id"] for source in sources}
        if unknown:
            parser.error("Unknown source ids: " + ", ".join(sorted(unknown)))
        sources = [source for source in sources if source["id"] in args.source]
    archive_path = args.archive or (ROOT / "data/archive.json" if args.output == ROOT / "site/data/edition.json" else args.output.parent / "collector-archive.json")
    try:
        with archive_lock(archive_path):
            return _run_collection(args, sources, archive_path, now)
    except ArchiveBusyError as exc:
        print(str(exc), file=sys.stderr)
        return 3


def _run_collection(args, sources, archive_path, now):
    """Run one collection transaction while its archive process lock is held."""
    legacy = {}
    if not archive_path.exists() and args.output.exists():
        try:
            legacy = json.loads(args.output.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            print("Previous edition could not be read; no authoritative archive exists.", file=sys.stderr)
    try:
        previous = load_archive(archive_path, previous=legacy)
    except (ValueError, OSError) as exc:
        print(f"Archive unreadable; refusing to overwrite history: {exc}", file=sys.stderr)
        return 2
    network = CachedNetwork(args.cache_dir)
    def adapter(source):
        return fetch_source({**source, "backfill_pages": args.backfill_pages},
                            copy.deepcopy(previous.get("source_states", {}).get(source["id"], {})), now=now, request=network)
    archive = collect(sources, previous=previous, now=now, fetcher=adapter, backfill_pages=args.backfill_pages)
    # The single archive commit makes records and cursors durable together.
    save_archive(archive_path, archive)
    write_public_archive(archive, args.output)
    try:
        network.save()
    except OSError as exc:
        print(f"Optional HTTP cache was not saved: {exc}", file=sys.stderr)
    print(f"Archived {archive['stats']['total_items']} records; {archive['stats']['relevant_items']} relevant, {archive['stats']['candidate_items']} candidates; {archive['stats']['successful_sources']}/{len(sources)} sources retrieved.")
    for error in archive["errors"]:
        print(f"{error['source']}: {error['message']}", file=sys.stderr)
    return 0 if archive["stats"]["successful_sources"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
