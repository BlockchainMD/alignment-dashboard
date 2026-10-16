"""Key-free discovery of public OpenReview submissions, with honest query scope."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from urllib.parse import urlencode, urljoin


def _iso_ms(value):
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def _value(content, key, default=None):
    value = content.get(key, default)
    return value.get("value", default) if isinstance(value, dict) else value


def _record(row):
    if not isinstance(row, dict) or not row.get("id") or not isinstance(row.get("content"), dict):
        raise ValueError("OpenReview returned a malformed submission")
    content = row["content"]
    # Only top-level public paper forums, never reviews or author email fields.
    if row.get("replyto") or row.get("forum", row["id"]) != row["id"]:
        return None
    title = _value(content, "title")
    if not isinstance(title, str) or not title.strip():
        return None
    pdf = _value(content, "pdf")
    links = [{"url": urljoin("https://openreview.net/", pdf), "rel": "fulltext", "type": "application/pdf"}] if isinstance(pdf, str) else []
    return {"title": title, "url": f"https://openreview.net/forum?id={row['id']}",
            "published_at": _iso_ms(row.get("odate")),
            "updated_at": _iso_ms(row.get("tmdate") or row.get("mdate")),
            "summary": _value(content, "abstract", ""),
            "authors": _value(content, "authors", []), "doi": _value(content, "doi"),
            "external_id": row["id"], "links": links, "license": row.get("license"),
            "venue": _value(content, "venue")}


def fetch_openreview(source, state, now, request):
    state = deepcopy(state) if isinstance(state, dict) else {}
    checkpoints = state.setdefault("queries", {})
    queries = source.get("queries", ["AI alignment", "AI safety", "mechanistic interpretability"])
    size = max(1, min(int(source.get("page_size", 100)), 100))
    budget = max(1, min(int(source.get("backfill_pages", 2)), 10))
    catchup_budget = max(1, min(int(source.get("catchup_pages", 2)), 10))
    rows_by_id, warnings, totals, pages = {}, [], {}, 0
    capped, all_complete = False, True
    for phrase in queries:
        checkpoint = checkpoints.setdefault(phrase, {})
        cache = {}

        def page(offset):
            nonlocal pages
            if offset in cache:
                return cache[offset]
            params = {"term": phrase, "type": "exact", "source": "forum", "content": "all",
                      "sort": "tmdate:desc", "limit": size, "offset": offset}
            data = json.loads(request(source.get("feed_url", "https://api2.openreview.net/notes/search") + "?" + urlencode(params)))
            if not isinstance(data, dict) or not isinstance(data.get("notes"), list):
                raise ValueError("OpenReview search returned no submission list")
            pages += 1
            records = []
            for note in data["notes"]:
                item = _record(note)
                if item:
                    records.append(item)
                    rows_by_id[item["external_id"]] = item
            total = data.get("count")
            if isinstance(total, int):
                totals[phrase] = total
            # Preserve raw result length for offset arithmetic even when a note
            # is excluded as a non-paper record.
            cache[offset] = (records, len(data["notes"]))
            return cache[offset]

        head, length = page(0)
        old_head = checkpoint.get("head_id")
        ids = {row["external_id"] for row in head}
        pending_head = False
        if old_head and old_head not in ids and length == size:
            offset = max(size, int(checkpoint.get("head_scan_offset", size)) - size // 2)
            for _ in range(catchup_budget):
                fetched, raw_length = page(offset)
                if old_head in {row["external_id"] for row in fetched} or raw_length < size:
                    checkpoint.pop("head_scan_offset", None)
                    break
                offset += size
                if offset >= 9900:
                    warnings.append(f"OpenReview catch-up search for {phrase!r} reached the search window limit.")
                    break
            else:
                checkpoint["head_scan_offset"] = offset
                pending_head = True
        if head and not pending_head:
            checkpoint["head_id"] = head[0]["external_id"]
        if pending_head:
            warnings.append(f"A publication/update burst for {phrase!r} exceeds this run's catch-up budget; scanning resumes next run.")

        complete = bool(checkpoint.get("backfill_complete"))
        if not complete:
            offset = max(0, int(checkpoint.get("backfill_offset", 0)) - size // 2)
            for _ in range(budget):
                _, raw_length = page(offset)
                if raw_length < size:
                    complete = True
                    break
                offset += size
                if offset >= 9900:
                    capped = True
                    warnings.append(f"OpenReview query {phrase!r} reached its 10,000-result search window. Narrower query partitions are needed for older coverage.")
                    break
            checkpoint["backfill_offset"] = offset
            checkpoint["backfill_complete"] = complete
        all_complete = all_complete and complete
    if any(total >= 10000 for total in totals.values()):
        capped = True
        warnings.append("At least one OpenReview query reports the search-result ceiling; coverage is incomplete.")
    warnings.append("OpenReview coverage is limited to public API v2 submissions matching configured phrases; API v1 venues and unindexed work may be missing.")
    return {"items": list(rows_by_id.values()), "state": state,
            "coverage": {"mode": "scholarly_query", "scope": "Public OpenReview API v2 paper submissions; phrase-matched title and abstract discovery.",
                         "pages": pages, "query_totals": totals, "backfill_complete": all_complete and not capped,
                         "pending_backfill": not all_complete or capped, "query_limited": True, "truncated": capped},
            "warnings": list(dict.fromkeys(warnings))}
