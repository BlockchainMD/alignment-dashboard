#!/usr/bin/env python3
"""Export source-grounded input packets for a future short-explainer pipeline.

This prepares metadata only. It does not summarize full papers, generate media,
or publish anything. Every packet requires source and script review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile

try:
    from .archive_store import load_archive
except ImportError:
    from archive_store import load_archive

ROOT = Path(__file__).resolve().parents[1]


def make_packet(article):
    evidence = {
        "title": article["title"],
        "url": article["url"],
        "doi": article.get("doi"),
        "arxiv_id": article.get("arxiv_id"),
        "version": article.get("version"),
        "updated_at": article.get("updated_at"),
        "content_hash": article.get("content_hash"),
        "authors": article.get("authors", []),
        "published_at": article.get("published_at"),
        "published_date": article.get("published_date"),
        "publication_year": article.get("publication_year"),
        "date_precision": article.get("date_precision", "unknown"),
        "source_excerpt": article.get("summary", ""),
        "excerpt_kind": article.get("summary_kind", "publisher excerpt"),
        "provenance": article.get("provenance", []),
        "links": article.get("links", []),
    }
    stable_evidence = {key: value for key, value in evidence.items() if key not in {"provenance", "links"}}
    fingerprint = hashlib.sha256(json.dumps(stable_evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {
        "schema_version": 1,
        "job_id": f"{article['id']}-{fingerprint[:12]}",
        "article_id": article["id"],
        "source_fingerprint": fingerprint,
        "status": "source_review_required",
        "research_classification": article.get("relevance", "candidate"),
        "evidence": evidence,
        "video_brief": {
            "audience": "A curious viewer without an AI research background",
            "maximum_duration_seconds": 120,
            "target_narration_words": {"minimum": 180, "maximum": 230},
            "outline": [
                {"section": "The question", "target_seconds": 15},
                {"section": "The idea, explained with an everyday example", "target_seconds": 35},
                {"section": "What the researchers tested and found", "target_seconds": 35},
                {"section": "Why it matters and what remains uncertain", "target_seconds": 25},
                {"section": "Original research credit", "target_seconds": 10},
            ],
            "script": None,
            "claims": [],
            "limitations": [],
            "visuals": [],
        },
        "required_reviews": [
            "Confirm that the record is substantive alignment research.",
            "Retrieve and read the original paper or article; an excerpt is not sufficient evidence.",
            "Attach a source section or page to each factual claim in the script.",
            "Preserve the distinction between measured findings, hypotheses, and commentary.",
            "Use plain language and a concrete example without overstating the result.",
            "Review the script and original visuals, including research attribution.",
            "Measure the rendered media duration and require 120 seconds or less.",
            "Approve the final video and platform-specific post before publication.",
        ],
        "publishing": {"approved": False, "platforms": [], "remote_ids": {}},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=ROOT / "data/archive.json")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/video-jobs.jsonl")
    parser.add_argument("--article-id", help="Prepare a single record instead of the whole archive")
    args = parser.parse_args(argv)
    archive = load_archive(args.archive)
    items = archive["items"]
    if args.article_id:
        items = [item for item in items if item["id"] == args.article_id or args.article_id in item.get("aliases", [])]
        if not items:
            parser.error("No matching article is present in the archive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=args.output.parent, encoding="utf-8", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            for item in items:
                stream.write(json.dumps(make_packet(item), ensure_ascii=False) + "\n")
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(args.output)
    print(f"Prepared {len(items)} metadata packets at {args.output}. Source review is required; no videos were generated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
