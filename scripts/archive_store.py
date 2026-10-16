"""Durable metadata storage and content-addressed public library shards.

The authoritative archive commits item history and source cursors together. Public
files contain only normalized metadata and bounded excerpts; raw responses belong
in an ignored HTTP cache, not in the public site or tracked archive.
"""
from __future__ import annotations

from collections import defaultdict
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import warnings

SCHEMA_VERSION = 2
STORAGE_FORMAT = "sharded-v1"
MAX_CANONICAL_CHUNK_BYTES = 8 * 1024 * 1024
MAX_CANONICAL_MANIFEST_BYTES = 64 * 1024 * 1024
_STORAGE_KEYS = {"storage_format", "storage_chunks", "items_count", "storage_generation"}


def _json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def atomic_write(path, data):
    """Replace a complete JSON file, preserving the old file on any write failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".archive-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validate_archive(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("Archive must be an object containing an items list")
    if payload.get("schema_version", 1) not in {1, SCHEMA_VERSION}:
        raise ValueError("Unsupported archive schema version; upgrade the collector first")
    if not all(isinstance(item, dict) for item in payload["items"]):
        raise ValueError("Archive contains malformed records")
    if not isinstance(payload.get("source_states", {}), dict) or not all(isinstance(value, dict) for value in payload.get("source_states", {}).values()):
        raise ValueError("Archive checkpoints must map source IDs to objects")
    if not isinstance(payload.get("sources", []), list) or not all(isinstance(value, dict) for value in payload.get("sources", [])):
        raise ValueError("Archive source health must be a list of objects")
    for item in payload["items"]:
        if not item.get("title") or not item.get("url"):
            raise ValueError("Archive contains a record without required identity metadata")
    return payload


def _safe_canonical_chunk(master_path, reference, digest):
    """Resolve only generated files beside this master, without following escapes."""
    if not isinstance(reference, str) or "\\" in reference:
        raise ValueError("Canonical chunk path is not a safe local reference")
    relative = Path(reference)
    if relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] != master_path.stem:
        raise ValueError("Canonical chunk path escapes its storage directory")
    if not re.fullmatch(r"records-[0-9a-f]{16}\.json", relative.name):
        raise ValueError("Canonical chunk filename is not supported")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Canonical chunk checksum is not valid SHA-256")
    if relative.name != "records-" + digest[:16] + ".json":
        raise ValueError("Canonical chunk filename/checksum mismatch")
    candidate = master_path.parent / relative
    try:
        candidate.resolve().relative_to(master_path.parent.resolve())
    except ValueError as exc:
        raise ValueError("Canonical chunk path resolves outside the archive") from exc
    return candidate


def load_archive(path, previous=None):
    """Expand validated immutable shards, or read/migrate a legacy flat archive.

    Missing/corrupt master or chunk data is never treated as an empty archive.
    Hashes, local paths, per-chunk counts and total count are checked before any
    caller can advance a checkpoint or replace the authoritative manifest.
    """
    path = Path(path)
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Archive must be a JSON object")
        if payload.get("storage_format") is None:
            return copy.deepcopy(_validate_archive(payload))
        if payload.get("storage_format") != STORAGE_FORMAT:
            raise ValueError("Unsupported archive storage format; upgrade the collector first")
        _validate_archive({**payload, "items": []})
        count = payload.get("items_count")
        references = payload.get("storage_chunks")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0 or not isinstance(references, list):
            raise ValueError("Sharded archive needs a nonnegative item count and chunk list")
        items, seen = [], set()
        for reference in references:
            if not isinstance(reference, dict):
                raise ValueError("Canonical chunk reference must be an object")
            expected_count = reference.get("count")
            if isinstance(expected_count, bool) or not isinstance(expected_count, int) or not 1 <= expected_count <= 500:
                raise ValueError("Canonical chunk count must be between 1 and 500")
            chunk = _safe_canonical_chunk(path, reference.get("path"), reference.get("sha256"))
            if str(chunk) in seen:
                raise ValueError("Canonical archive repeats a chunk reference")
            seen.add(str(chunk))
            if chunk.stat().st_size > MAX_CANONICAL_CHUNK_BYTES:
                raise ValueError("Canonical chunk exceeds its bounded storage size")
            body = chunk.read_bytes()
            if hashlib.sha256(body).hexdigest() != reference["sha256"]:
                raise ValueError("Canonical chunk checksum mismatch: " + chunk.name)
            rows = json.loads(body)
            if not isinstance(rows, dict) or rows.get("schema_version") != SCHEMA_VERSION or not isinstance(rows.get("items"), list):
                raise ValueError("Canonical chunk has an unsupported record structure")
            if len(rows["items"]) != expected_count:
                raise ValueError("Canonical chunk record count mismatch")
            items.extend(rows["items"])
        if len(items) != count:
            raise ValueError("Canonical archive total count mismatch")
        result = {key: value for key, value in payload.items() if key not in _STORAGE_KEYS}
        result["items"] = items
        return copy.deepcopy(_validate_archive(result))
    if previous:
        return copy.deepcopy(_validate_archive(previous))
    return {"schema_version": SCHEMA_VERSION, "items": [], "sources": [], "source_states": {}}


def _retain_canonical_generations(directory, current_manifest, keep=3):
    """Prune only obsolete generated snapshots; records remain in current shards."""
    index = directory / "generations.json"
    try:
        payload = json.loads(index.read_text(encoding="utf-8"))
        names = payload.get("manifests", []) if isinstance(payload, dict) else []
        names = names if isinstance(names, list) else []
    except (OSError, ValueError):
        names = [entry.name for entry in sorted(directory.glob("manifest-*.json"),
                                               key=lambda entry: entry.stat().st_mtime, reverse=True)]
    names = [name for name in names if isinstance(name, str) and re.fullmatch(r"manifest-[0-9a-f]{16}\.json", name)]
    retained = list(dict.fromkeys([current_manifest.name, *names]))[:keep]
    referenced = set(retained)
    for name in retained:
        try:
            payload = json.loads((directory / name).read_text(encoding="utf-8"))
            for row in payload["storage_chunks"]:
                filename = Path(row["path"]).name
                if not re.fullmatch(r"records-[0-9a-f]{16}\.json", filename):
                    return
                referenced.add(filename)
        except (OSError, ValueError, TypeError, KeyError):
            return  # An uninspectable cached generation must not trigger guesses.
    atomic_write(index, {"schema_version": 1, "manifests": retained})
    for entry in directory.glob("*.json"):
        generated = re.fullmatch(r"(?:records|manifest)-[0-9a-f]{16}\.json", entry.name)
        if generated and entry.name not in referenced:
            entry.unlink()


def save_archive(path, archive, chunk_size=500, keep_generations=3):
    """Atomically commit all metadata and cursors through a small master manifest.

    Immutable metadata shards contain at most 500 records and 8 MiB. Write every
    shard and the recovery generation first, then replace the master exactly
    once. A failed replacement leaves the previous records and cursor together.
    The current and previous two complete generations remain in the checkout;
    canonical item history itself has no retention cutoff or total-count cap.
    """
    _validate_archive(archive)
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or not 1 <= chunk_size <= 500:
        raise ValueError("Canonical chunk size must be between 1 and 500")
    if not isinstance(keep_generations, int) or isinstance(keep_generations, bool) or keep_generations < 1:
        raise ValueError("At least one canonical generation must be retained")
    path = Path(path)
    directory = path.parent / path.stem
    references = []
    batch, batch_bytes = [], len(_json_bytes({"schema_version": SCHEMA_VERSION, "items": []}))
    overhead = batch_bytes

    def flush(rows):
        payload = {"schema_version": SCHEMA_VERSION, "items": rows}
        chunk, digest = _write_content_addressed(directory, "records", payload)
        references.append({"path": directory.name + "/" + chunk.name,
                           "count": len(rows), "sha256": digest})

    for item in archive["items"]:
        record_bytes = len(_json_bytes(item)) - 1
        projected = batch_bytes + record_bytes + bool(batch)
        if batch and (len(batch) >= chunk_size or projected > MAX_CANONICAL_CHUNK_BYTES):
            flush(batch)
            batch, batch_bytes = [], overhead
            projected = overhead + record_bytes
        if projected > MAX_CANONICAL_CHUNK_BYTES:
            raise ValueError("One metadata record exceeds the 8 MiB canonical shard limit; history was not replaced")
        batch.append(item)
        batch_bytes = projected
    if batch:
        flush(batch)
    manifest = {key: copy.deepcopy(value) for key, value in archive.items() if key not in {"items", *_STORAGE_KEYS}}
    manifest.update(schema_version=SCHEMA_VERSION, storage_format=STORAGE_FORMAT,
                    items_count=len(archive["items"]), storage_chunks=references)
    if len(_json_bytes(manifest)) > MAX_CANONICAL_MANIFEST_BYTES:
        raise ValueError("Source checkpoint metadata exceeds 64 MiB; compact source state before continuing")
    generation, _ = _write_content_addressed(directory, "manifest", manifest)
    committed = {**manifest, "storage_generation": directory.name + "/" + generation.name}
    pretty_bytes = len(json.dumps(committed, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")) + 1
    if pretty_bytes > MAX_CANONICAL_MANIFEST_BYTES:
        raise ValueError("Source checkpoint metadata exceeds 64 MiB; compact source state before continuing")
    # Do not perform retention until the durable master points to complete data.
    atomic_write(path, committed)
    try:
        _retain_canonical_generations(directory, generation, keep=keep_generations)
    except OSError as exc:
        warnings.warn("Canonical archive saved; old shard cleanup will retry next run: " + str(exc), RuntimeWarning)
    return committed


def _public_item(item):
    # Revisions are audit metadata in the durable archive; public consumers need
    # only a count. Keeping a field allowlist prevents future raw data leaking.
    keys = {
        "id", "aliases", "title", "url", "source", "source_id", "source_url", "published_at", "published_date",
        "date_precision", "publication_year", "summary", "summary_kind", "topics", "score", "relevance", "relevance_reason",
        "authors", "first_seen", "discovered_at", "doi", "arxiv_id", "links", "updated_at",
        "version", "external_id", "publication_type", "language", "provenance", "content_hash",
    }
    result = {key: copy.deepcopy(value) for key, value in item.items() if key in keys}
    if isinstance(result.get("provenance"), list):
        result["provenance"] = sorted(({key: value for key, value in row.items() if key != "last_seen"}
            for row in result["provenance"] if isinstance(row, dict)),
            key=lambda row: (row.get("source_id") or "", row.get("article_url") or ""))
    if isinstance(result.get("links"), list):
        result["links"] = sorted(set(result["links"]))
    if isinstance(result.get("aliases"), list):
        result["aliases"] = sorted(set(result["aliases"]))
    result["revision_count"] = len(item.get("revisions", []))
    return result


def _write_content_addressed(directory, stem, payload):
    """Hash the exact on-disk representation; old immutable paths remain usable."""
    body = _json_bytes(payload)
    digest = hashlib.sha256(body).hexdigest()
    path = directory / f"{stem}-{digest[:16]}.json"
    if not path.exists():
        directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=directory, prefix=".public-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError(f"Immutable public shard is corrupt: {path.name}")
    return path, digest


def _retain_public_generations(directory, current_manifest, keep=3):
    """Keep three complete public snapshots; canonical research is never pruned."""
    index = directory / "generations.json"
    names = []
    try:
        payload = json.loads(index.read_text(encoding="utf-8"))
        names = payload.get("manifests", []) if isinstance(payload, dict) else []
        if not isinstance(names, list):
            names = []
    except (OSError, ValueError):
        # First migration from an older export has no generation index.
        names = [path.name for path in sorted(directory.glob("manifest-*.json"),
                                             key=lambda path: path.stat().st_mtime, reverse=True)]
    valid_name = lambda name: isinstance(name, str) and bool(re.fullmatch(r"manifest-[0-9a-f]{16}\.json", name))
    ordered = list(dict.fromkeys([current_manifest.name, *[name for name in names if valid_name(name)]]))
    retained = [name for name in ordered if (directory / name).is_file()][:keep]
    referenced = set(retained)
    for name in retained:
        try:
            manifest = json.loads((directory / name).read_text(encoding="utf-8"))
            for chunk in manifest["chunks"]:
                filename = Path(chunk["url"]).name
                if re.fullmatch(r"(?:\d{4}|undated)-\d{4,}-[0-9a-f]{16}\.json", filename):
                    referenced.add(filename)
        except (OSError, ValueError, KeyError, TypeError):
            # If a retained snapshot cannot be inspected, skip deletion rather
            # than guess which immutable files a cached browser might need.
            return
    atomic_write(index, {"schema_version": 1, "manifests": retained})
    for path in directory.glob("*.json"):
        generated = re.fullmatch(r"(?:manifest|(?:\d{4}|undated)-\d{4,})-[0-9a-f]{16}\.json", path.name)
        if generated and path.name not in referenced:
            path.unlink()


def write_public_archive(archive, output, recent_limit=1500, chunk_size=500):
    """Publish the complete metadata archive in bounded immutable JSON chunks.

    The recent edition stays small. Consumers use its versioned manifest to load
    all records without mixed-generation cache artifacts. The current and prior
    two complete public generations remain available; canonical history is never
    deleted. Volatile observation timestamps stay in the internal archive.
    """
    _validate_archive(archive)
    if recent_limit < 1 or chunk_size < 1:
        raise ValueError("Public archive limits must be positive")
    output = Path(output)
    directory = output.parent / "archive"
    # URLs are relative to the web root; site/data/edition.json is the deployed
    # entry point. A custom output retains the same logical data/ URL contract.
    url_prefix = "data/archive/"
    groups = defaultdict(list)
    public_items = [_public_item(item) for item in archive["items"]]
    for item in public_items:
        date = item.get("published_at")
        known_year = item.get("publication_year")
        valid_year = not isinstance(known_year, bool) and isinstance(known_year, int) and 1000 <= known_year <= 9999
        year = date[:4] if isinstance(date, str) and re.match(r"^\d{4}-", date) else str(known_year) if valid_year else "undated"
        groups[year].append(item)
    chunks = []
    for year in sorted(groups, reverse=True):
        records = groups[year]
        for start in range(0, len(records), chunk_size):
            page = start // chunk_size + 1
            payload = {"schema_version": 1, "items": records[start:start + chunk_size]}
            path, digest = _write_content_addressed(directory, f"{year}-{page:04d}", payload)
            chunks.append({"url": url_prefix + path.name, "count": len(payload["items"]),
                           "sha256": digest, "year": year})
    manifest = {"schema_version": 1, "generated_at": archive.get("generated_at"),
                "total_items": len(public_items), "chunks": chunks}
    manifest_path, digest = _write_content_addressed(directory, "manifest", manifest)
    atomic_write(directory / "manifest.json", manifest)
    edition = {key: copy.deepcopy(value) for key, value in archive.items() if key not in {"items", "source_states"}}
    edition["items"] = public_items[:recent_limit]
    edition["library_index"] = {"manifest": url_prefix + manifest_path.name, "sha256": digest,
                                "total_items": len(public_items), "edition_items": len(edition["items"]),
                                "complete": len(public_items) <= recent_limit}
    atomic_write(output, edition)
    _retain_public_generations(directory, manifest_path)
    return edition
