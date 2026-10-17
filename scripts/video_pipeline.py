#!/usr/bin/env python3
"""Queue new research and produce source-linked videos for local review."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import re
import sys
import tempfile
import uuid

try:
    from .archive_store import active_archive_items, atomic_write, load_archive
    from .collect import archive_lock
    from .export_video_jobs import make_packet
    from .video_voice import CONFIG_PATH as VOICE_CONFIG_PATH, voice_settings
    from .video_design import visual_settings
except ImportError:
    from archive_store import active_archive_items, atomic_write, load_archive
    from collect import archive_lock
    from export_video_jobs import make_packet
    from video_voice import CONFIG_PATH as VOICE_CONFIG_PATH, voice_settings
    from video_design import visual_settings

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUEUE = ROOT / "data/video-queue.json"
DEFAULT_WORK = ROOT / "artifacts/videos"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sync_queue(archive, previous=None):
    """Discovery state only; local rendering/review state is kept separately."""
    previous = previous or {}
    if previous and (previous.get("schema_version") != 1 or not isinstance(previous.get("jobs"), list)):
        raise ValueError("Video queue is unreadable; refusing to reset its history")
    old = {row["job_id"]: deepcopy(row) for row in previous.get("jobs", [])}
    jobs = []
    active_ids = set()
    checked = archive.get("generated_at") or utc_now()
    for article in active_archive_items(archive):
        packet = make_packet(article)
        active_ids.add(article["id"])
        prior = old.pop(packet["job_id"], {})
        needs_review = article.get("relevance") != "relevant" or article.get("temporal_status") in {
            "publication_time_uncertain", "publication_date_unverified"}
        jobs.append({"job_id": packet["job_id"], "article_id": article["id"],
                     "source_fingerprint": packet["source_fingerprint"], "title": article["title"],
                     "url": article["url"], "authors": article.get("authors", []),
                     "published_at": article.get("published_at"),
                     "research_classification": article.get("relevance", "candidate"),
                     "temporal_status": article.get("temporal_status"),
                     "status": "needs_relevance_review" if needs_review else "queued",
                     "first_queued_at": prior.get("first_queued_at", checked)})
    for row in old.values():
        row["status"] = "superseded" if row["article_id"] in active_ids else "out_of_scope"
        jobs.append(row)
    jobs.sort(key=lambda row: (row.get("first_queued_at", ""), row["job_id"]), reverse=True)
    return {"schema_version": 1, "generated_at": checked,
            "publication_start": archive.get("coverage_policy", {}).get("publication_start"),
            "maximum_duration_seconds": 120,
            "stats": {"queued": sum(row["status"] == "queued" for row in jobs),
                      "needs_relevance_review": sum(row["status"] == "needs_relevance_review" for row in jobs),
                      "retired": sum(row["status"] in {"superseded", "out_of_scope"} for row in jobs)},
            "jobs": jobs}


def _article(archive, article_id):
    for item in active_archive_items(archive):
        if item["id"] == article_id or article_id in item.get("aliases", []):
            return item
    raise ValueError("Article is not in the active publication window")


def _work_root(work_dir):
    base = Path(work_dir).expanduser().resolve()
    # Keep the lexical artifacts boundary: a symlink named artifacts must not
    # turn the public site (or another directory) into an allowed destination.
    boundary = ROOT.resolve() / "artifacts"
    if not base.is_relative_to(boundary):
        raise ValueError("Video snapshots, drafts, and renders must stay beneath the ignored artifacts/ directory")
    return base


def _inside(directory, *parts):
    base = _work_root(directory)
    target = base.joinpath(*parts).resolve()
    if not target.is_relative_to(base):
        raise ValueError("A video artifact path or symlink escapes its work directory")
    return target


def _work_path(work_dir, job_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", job_id):
        raise ValueError("Unsafe job identifier")
    return _inside(work_dir, job_id)


def _needs_review(article):
    return article.get("relevance") != "relevant" or article.get("temporal_status") in {
        "publication_time_uncertain", "publication_date_unverified"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _settings(formats, voice, rate, preview, speech_provider="kokoro", speech_speed=None):
    speech = voice_settings(speech_provider=speech_provider, voice=voice, rate=rate, speech_speed=speech_speed)
    return {"formats": sorted(formats), "speech": speech, "visual_design": visual_settings(), "preview": bool(preview), "fps": 24,
            "renderer_sha256": _file_digest(Path(__file__).with_name("video_render.py")),
            "voice_module_sha256": _file_digest(Path(__file__).with_name("video_voice.py")),
            "voice_profile_sha256": _file_digest(VOICE_CONFIG_PATH)}


def _render_fingerprint(source, script, settings):
    return _digest({"source_sha256": source["sha256"], "script_sha256": _digest(script), "settings": settings})


def _atomic_text(path, text):
    path = _inside(Path(path).parent, Path(path).name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def _invalidate_current(directory):
    for name in ("index.html", "preview.html", "publish-package.json"):
        _inside(directory, name).unlink(missing_ok=True)


def _verify_render_files(report, directory, formats, preview):
    if not isinstance(report, dict) or not isinstance(report.get("formats"), dict):
        raise ValueError("Renderer returned no format manifest")
    if set(report["formats"]) != set(formats):
        raise ValueError("Rendered formats do not match the requested formats")
    expected = {"landscape": (1280, 720) if preview else (1920, 1080),
                "portrait": (720, 1280) if preview else (1080, 1920)}
    root = _work_root(directory)
    for name in formats:
        output = report["formats"][name]
        if not isinstance(output, dict) or (output.get("width"), output.get("height")) != expected[name]:
            raise ValueError("Rendered dimensions do not match the requested resolution")
        path = Path(output.get("path", "")).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.suffix != ".mp4":
            raise ValueError("A rendered video is missing or escapes its generation directory")
        if path.stat().st_size != output.get("bytes") or _file_digest(path) != output.get("sha256"):
            raise ValueError("Rendered video size/checksum mismatch")
        duration = output.get("duration_seconds")
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= 120.01:
            raise ValueError("Rendered video exceeds the two-minute duration limit")
    for name in ("index.html", "script.json", "source.json", "captions.srt", "captions.vtt", "narration.wav"):
        if not _inside(root, name).is_file():
            raise ValueError(f"Render review package is missing {name}")


def _relocate_paths(value, old_root, new_root, key=None):
    if isinstance(value, dict):
        return {name: _relocate_paths(item, old_root, new_root, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_relocate_paths(item, old_root, new_root, key) for item in value]
    if isinstance(value, str) and (key in {"path", "poster"} or key and key.endswith("_path")):
        path = Path(value).resolve()
        if not path.is_relative_to(old_root):
            raise ValueError("A renderer path escapes its staged generation")
        return str(new_root / path.relative_to(old_root))
    return value


def prepare_job(article, work_dir=DEFAULT_WORK, allow_candidate=False, refresh_source=False, capture=None):
    if _needs_review(article) and not allow_candidate:
        raise ValueError("This candidate needs research/date review first; --include-candidate explicitly selects it for draft review")
    packet = make_packet(article)
    directory = _work_path(work_dir, packet["job_id"])
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write(_inside(directory, "packet.json"), packet)
    source_path = _inside(directory, "source.json")
    if not source_path.exists() or refresh_source:
        if capture is None:
            try:
                from .video_sources import capture_source
            except ImportError:
                from video_sources import capture_source
            capture = capture_source
        source = capture(article, directory)
        atomic_write(source_path, source)
    else:
        source = read_json(source_path)
    try:
        from .video_draft import validate_source
    except ImportError:
        from video_draft import validate_source
    validate_source(source)
    if source.get("article_id") != article["id"]:
        raise ValueError("Source snapshot identity/checksum mismatch; recapture with --refresh-source")
    return directory, source


def render_is_current(article, work_dir=DEFAULT_WORK, *, formats=("landscape", "portrait"),
                      voice=None, rate=155, preview=False, script_path=None,
                      speech_provider="kokoro", speech_speed=None):
    """Reuse only a complete generation bound to this source/script/settings."""
    directory = _work_path(work_dir, make_packet(article)["job_id"])
    try:
        state = read_json(_inside(directory, "job.json"))
        if state.get("status") != "rendered_for_review" or state.get("job_id") != make_packet(article)["job_id"]:
            return False
        try:
            from .video_draft import validate_draft, validate_source
        except ImportError:
            from video_draft import validate_draft, validate_source
        source = read_json(_inside(directory, "source.json"))
        validate_source(source)
        if source["article_id"] != article["id"] or state.get("article_id") != article["id"]:
            return False
        script = read_json(script_path or _inside(directory, "script.json"))
        validate_draft(script, source)
        settings = _settings(formats, voice, rate, preview, speech_provider, speech_speed)
        if state.get("source_sha256") != source["sha256"] or state.get("script_sha256") != _digest(script):
            return False
        if state.get("render_settings") != settings or state.get("render_fingerprint") != _render_fingerprint(source, script, settings):
            return False
        generation = Path(state.get("current_render_dir", "")).resolve()
        if not generation.is_relative_to(_inside(directory, "renders")) or not generation.is_dir():
            return False
        report = state.get("render")
        if report != read_json(_inside(generation, "manifest.json")):
            return False
        if report.get("source_sha256") != source["sha256"] or report.get("script_sha256") != _digest(script):
            return False
        if report.get("voice") != {"synthetic": True, **settings["speech"]}:
            return False
        if report.get("visual_design") != settings["visual_design"]:
            return False
        if read_json(_inside(generation, "script.json")) != script or read_json(_inside(generation, "source.json")) != source:
            return False
        _verify_render_files(report, generation, formats, preview)
        return _inside(directory, "index.html").is_file() and _inside(directory, "publish-package.json").is_file()
    except (ValueError, OSError, KeyError, TypeError, RuntimeError):
        return False


def produce(article, work_dir=DEFAULT_WORK, script_path=None, provider="codex", model=None,
            formats=("landscape", "portrait"), voice=None, rate=155, preview=False,
            allow_candidate=False, refresh_source=False, redraft=False, capture=None,
            draft_generator=None, renderer=None, codex_cli_defaults=False,
            speech_provider="kokoro", speech_speed=None):
    # Validate the complete speech identity before capturing, drafting, or
    # invalidating an existing generation. This does not load a neural model.
    settings = _settings(formats, voice, rate, preview, speech_provider, speech_speed)
    packet = make_packet(article)
    directory = _work_path(work_dir, packet["job_id"])
    directory.mkdir(parents=True, exist_ok=True)
    with archive_lock(_inside(directory, "job.json")):
        state_path = _inside(directory, "job.json")
        state = read_json(state_path) if state_path.exists() else {
            "schema_version": 1, "job_id": packet["job_id"], "article_id": article["id"],
            "title": article["title"], "source_fingerprint": packet["source_fingerprint"],
            "created_at": utc_now(), "publishing": {"approved": False, "remote_ids": {}}}
        try:
            _invalidate_current(directory)
            state.pop("render", None)
            state.pop("current_render_dir", None)
            directory, source = prepare_job(article, work_dir, allow_candidate, refresh_source, capture)
            state.update(status="source_ready", source_sha256=source["sha256"], error=None)
            atomic_write(state_path, state)
            try:
                from .video_draft import build_prompt, generate_draft, generate_codex_draft, validate_draft
            except ImportError:
                from video_draft import build_prompt, generate_draft, generate_codex_draft, validate_draft
            _atomic_text(_inside(directory, "draft-prompt.txt"), build_prompt(source))
            local_script = _inside(directory, "script.json")
            if script_path:
                script = read_json(script_path)
            elif local_script.exists() and not redraft:
                script = read_json(local_script)
            elif draft_generator:
                script = draft_generator(source)
            elif provider == "codex":
                script = generate_codex_draft(source, use_cli_defaults=codex_cli_defaults)
            elif provider == "openai":
                if not model:
                    raise ValueError("--model is required for the OpenAI API provider")
                script = generate_draft(source, model=model)
            else:
                raise ValueError("Supply --script, or select an installed drafting provider")
            validate_draft(script, source)
            atomic_write(local_script, script)
            fingerprint = _render_fingerprint(source, script, settings)
            state.update(status="draft_ready", drafting_provider="supplied_script" if script_path else provider,
                         script_sha256=_digest(script), render_settings=settings, render_fingerprint=fingerprint)
            atomic_write(state_path, state)
            if renderer is None:
                try:
                    from .video_render import render_video
                except ImportError:
                    from video_render import render_video
                renderer = render_video
            renders = _inside(directory, "renders")
            renders.mkdir(exist_ok=True)
            generation = _inside(renders, f"{fingerprint[:16]}-{uuid.uuid4().hex[:12]}")
            with tempfile.TemporaryDirectory(prefix=".staging-", dir=renders) as staged:
                staging = Path(staged).resolve()
                atomic_write(_inside(staging, "script.json"), script)
                atomic_write(_inside(staging, "source.json"), source)
                render_report = renderer(script, staging, formats=tuple(formats), voice=voice, rate=rate, preview=preview,
                                         speech_provider=speech_provider, speech_speed=speech_speed)
                _verify_render_files(render_report, staging, formats, preview)
                if render_report.get("voice") != {"synthetic": True, **settings["speech"]}:
                    raise ValueError("Rendered voice does not match the requested speech settings")
                if render_report.get("visual_design") != settings["visual_design"]:
                    raise ValueError("Rendered visuals do not match the requested design settings")
                if render_report.get("source_sha256") != source["sha256"] or render_report.get("article_id") != article["id"]:
                    raise ValueError("Rendered report does not match the source identity")
                render_report = _relocate_paths(render_report, staging, generation)
                render_report.update(script_sha256=_digest(script), render_settings=settings, render_fingerprint=fingerprint,
                                     manifest_path=str(generation / "manifest.json"), preview_path=str(generation / "index.html"))
                package = {
                    "schema_version": 1, "job_id": state["job_id"], "title": script["title"],
                    "source_sha256": source["sha256"], "script_sha256": _digest(script),
                    "description": script["description"] + "\n\nOriginal research: " + source["url"]
                        + "\nAI narration. Source-linked explanation; review before sharing.",
                    "credits": script["credits"], "research_type": script["research_type"],
                    "limitations": script["limitations"], "render": render_report,
                    "review_required": ["Source supports each claim", "Technical details are correct and plainly explained",
                                        "Limitations and attribution are accurate", "Listen to narration and inspect both aspect ratios"],
                    "publishing": {"approved": False, "platforms": [], "remote_ids": {}}}
                atomic_write(_inside(staging, "manifest.json"), render_report)
                atomic_write(_inside(staging, "publish-package.json"), package)
                staging.rename(generation)
            # All generation files are now complete and immutable. Only these
            # small current pointers change when a later render succeeds.
            atomic_write(_inside(directory, "publish-package.json"), package)
            target = f"renders/{generation.name}/index.html"
            _atomic_text(_inside(directory, "index.html"), '<!doctype html><html lang="en"><meta charset="utf-8">'
                         '<meta name="viewport" content="width=device-width,initial-scale=1">'
                         f'<meta http-equiv="refresh" content="0;url={html.escape(target, quote=True)}">'
                         f'<title>{html.escape(script["title"])}</title><a href="{html.escape(target, quote=True)}">Open the complete video review package</a></html>')
            state.update(status="rendered_for_review", updated_at=utc_now(), render=render_report,
                         current_render_dir=str(generation), publishing={"approved": False, "remote_ids": {}})
            atomic_write(state_path, state)
        except Exception as exc:
            _invalidate_current(directory)
            state.pop("render", None)
            state.pop("current_render_dir", None)
            state.update(status="failed", error=str(exc)[:500], updated_at=utc_now())
            atomic_write(state_path, state)
            write_studio(work_dir)
            raise
    write_studio(work_dir)
    return state


def write_studio(work_dir):
    base = _work_root(work_dir)
    cards = []
    for path in sorted(base.glob("*/job.json")):
        directory = _work_path(base, path.parent.name)
        path = _inside(directory, "job.json")
        job = read_json(path)
        preview_file = "index.html" if job.get("status") == "rendered_for_review" and _inside(directory, "index.html").exists() else None
        title = html.escape(job.get("title", job["job_id"]))
        target = f"{path.parent.name}/{preview_file}" if preview_file else f"{path.parent.name}/job.json"
        cards.append(f'<a class="card" href="{html.escape(target, quote=True)}"><small>{html.escape(job["status"])}</small><h2>{title}</h2><span>Open review package →</span></a>')
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Alignment Daily — Video studio</title><style>body{font:17px system-ui;background:#f7f6f2;color:#253a32;max-width:1100px;margin:60px auto;padding:0 24px}h1{font-size:clamp(36px,7vw,64px);letter-spacing:-.05em;margin-bottom:14px}p{line-height:1.7;max-width:700px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:22px;margin:40px 0}.card{display:block;border:1px solid #daddd2;border-radius:16px;background:#fdfcf9;padding:28px;color:inherit;text-decoration:none}.card h2{font-size:25px;letter-spacing:-.03em}.card span,small{color:#506848}small{text-transform:uppercase;font-size:11px;letter-spacing:.12em}footer{font-size:13px;color:#667267;margin-top:40px}</style><small>Alignment Daily · Local video studio</small><h1>Research, made clear.</h1><p>Source-linked explanations for alignment researchers and curious enthusiasts. Keep the technical point. Explain it simply. Every render is a draft for review.</p><main>''' + "".join(cards) + '''</main><footer>Source snapshots and drafts stay local. Videos have not been posted to YouTube or X.</footer></html>'''
    base.mkdir(parents=True, exist_ok=True)
    _atomic_text(_inside(base, "index.html"), page)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["sync", "prepare", "produce", "batch"])
    parser.add_argument("--archive", type=Path, default=ROOT / "data/archive.json")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--article-id")
    parser.add_argument("--script", type=Path)
    parser.add_argument("--provider", choices=["codex", "openai"], default="codex")
    parser.add_argument("--model", help="Explicit model for API drafting; Codex uses its configured model")
    parser.add_argument("--codex-cli-defaults", action="store_true", help="Explicitly use Codex CLI defaults for this drafting process")
    parser.add_argument("--format", choices=["both", "landscape", "portrait"], default="both")
    parser.add_argument("--speech-provider", choices=["kokoro", "system"], default="kokoro",
                        help="Narration engine; Kokoro neural speech is the production default")
    parser.add_argument("--voice", help="Voice ID; defaults to af_heart for Kokoro or Samantha on macOS")
    parser.add_argument("--rate", type=int, default=155, help="System speech words per minute; use --speech-speed for Kokoro")
    parser.add_argument("--speech-speed", type=float, help="Kokoro delivery speed factor; independent of system words per minute")
    parser.add_argument("--preview", action="store_true", help="Render at 720p for faster iteration")
    parser.add_argument("--include-candidate", action="store_true")
    parser.add_argument("--refresh-source", action="store_true")
    parser.add_argument("--redraft", action="store_true")
    parser.add_argument("--limit", type=int, default=1, help="Maximum batch jobs; default 1 bounds drafting usage")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 20:
        parser.error("--limit must be between 1 and 20")
    try:
        args.work_dir = _work_root(args.work_dir)
        archive = load_archive(args.archive)
        if args.command == "sync":
            queue = sync_queue(archive, read_json(args.queue) if args.queue.exists() else None)
            atomic_write(args.queue, queue)
            print(json.dumps(queue["stats"]))
            return 0
        if args.command in {"prepare", "produce"} and not args.article_id:
            parser.error("--article-id is required")
        if args.command == "prepare":
            directory, source = prepare_job(_article(archive, args.article_id), args.work_dir,
                                            args.include_candidate, args.refresh_source)
            try:
                from .video_draft import build_prompt
            except ImportError:
                from video_draft import build_prompt
            _atomic_text(_inside(directory, "draft-prompt.txt"), build_prompt(source))
            print(f"Full source and drafting prompt prepared: {directory}")
            return 0
        formats = ("landscape", "portrait") if args.format == "both" else (args.format,)
        articles = [_article(archive, args.article_id)] if args.command == "produce" else [
            article for article in active_archive_items(archive) if not _needs_review(article) or args.include_candidate]
        failures = done = 0
        for article in articles:
            if args.command == "batch":
                if done + failures >= args.limit:
                    break
                if not (args.refresh_source or args.redraft) and render_is_current(article, args.work_dir,
                        formats=formats, voice=args.voice, rate=args.rate, preview=args.preview, script_path=args.script,
                        speech_provider=args.speech_provider, speech_speed=args.speech_speed):
                    continue
            try:
                job = produce(article, args.work_dir, args.script, args.provider, args.model, formats,
                              args.voice, args.rate, args.preview, args.include_candidate,
                              args.refresh_source, args.redraft, codex_cli_defaults=args.codex_cli_defaults,
                              speech_provider=args.speech_provider, speech_speed=args.speech_speed)
                done += 1
                print(f"{job['job_id']}: rendered for review. No social posts were published.")
            except Exception as exc:
                failures += 1
                print(f"{article['id']}: {exc}", file=sys.stderr)
                if args.command != "batch":
                    return 1
        write_studio(args.work_dir)
        print(f"Video studio: {args.work_dir.resolve() / 'index.html'}; rendered {done}, failed {failures}")
        return 1 if failures else 0
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
