#!/usr/bin/env python3
"""Flag important coverage outages after publishing their visible health state."""
import argparse
import json
from pathlib import Path

CORE_SOURCES = {"arxiv", "openreview", "alignment-feed", "alignment-forum"}


def coverage_failures(edition):
    failures = []
    present = {source["id"] for source in edition["sources"]}
    for identifier in sorted(CORE_SOURCES - present):
        failures.append(f"Core discovery source {identifier} has no health record.")
    for source in edition["sources"]:
        status = source.get("status")
        if status == "ok":
            continue
        if source["id"] in CORE_SOURCES or source.get("failure_streak", 0) >= 3:
            failures.append(f"{source.get('name', source['id'])}: {status}. {source.get('error') or source.get('coverage', {}).get('primary_error') or 'Inspect source coverage and warnings.'}")
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edition", type=Path, default=Path("site/data/edition.json"))
    args = parser.parse_args(argv)
    edition = json.loads(args.edition.read_text(encoding="utf-8"))
    failures = coverage_failures(edition)
    for failure in failures:
        # Messages are public diagnostics; never include raw feed bodies.
        print(f"Coverage needs attention: {failure}")
    if not failures:
        print("Core discovery sources are available; no repeated publisher outages are recorded. Historical completeness remains unverified.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
