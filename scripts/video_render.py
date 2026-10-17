#!/usr/bin/env python3
"""Render reviewed research scripts as original, captioned diagram explainers.

Pillow draws the diagrams, local neural speech provides a disclosed synthetic
voice, and FFmpeg encodes the video. System speech remains an explicit option.
Caption chunks are timed proportionally within measured scene audio. They are
not forced-aligned, word-accurate transcripts.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from urllib.parse import urlparse

try:
    from .video_voice import synthesize, voice_settings
except ImportError:
    from video_voice import synthesize, voice_settings

try:
    from .video_design import ScenePainter, font, wrap_text, visual_settings
    from .video_diagrams import comparison_mode, analogy_mode
except ImportError:
    from video_design import ScenePainter, font, wrap_text, visual_settings
    from video_diagrams import comparison_mode, analogy_mode


try:
    from PIL import Image
except ImportError:  # Pure caption/timing helpers remain usable in collection CI.
    Image = None


PREVIEW_BACKGROUND = "#09252B"
PREVIEW_TEXT = "#F4F1E9"
PREVIEW_LINE = "#315159"
PREVIEW_ACCENT = "#D8F580"
PREVIEW_PANEL = "#12343B"
VISUALS = {"neuron", "analogy", "comparison", "test", "takeaway", "source"}
CANVASES = {"landscape": (1920, 1080), "portrait": (1080, 1920)}
PREVIEW_CANVASES = {"landscape": (1280, 720), "portrait": (720, 1280)}
CAPTION_TIMING = "Measured scene audio; proportional phrase timing, not word alignment."
TEACHING_PAUSES = {"within_concept_seconds": .55, "between_concepts_seconds": .85}


def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, capture_output=True, **kwargs)


def probe_media(path: Path) -> dict:
    result = _run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)])
    return json.loads(result.stdout)


def media_duration(path: Path) -> float:
    duration = float(probe_media(path)["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"Invalid media duration for {path}")
    return duration


def caption_chunks(text: str, max_words: int = 10, max_chars: int = 68) -> list[str]:
    """Short phrases, with sentence boundaries preferred over arbitrary cuts."""
    if max_words < 1 or max_chars < 8:
        raise ValueError("Caption limits must be positive and readable")
    words = re.sub(r"\s+", " ", str(text)).strip().split()
    chunks: list[str] = []
    current: list[str] = []
    for word in words:
        if current and (len(current) >= max_words or len(" ".join(current + [word])) > max_chars):
            chunks.append(" ".join(current))
            current = []
        current.append(word)
        if re.search(r"[.!?][\"')]*$", word) and len(current) >= 4:
            chunks.append(" ".join(current))
            current = []
    if current:
        chunks.append(" ".join(current))
    return chunks


def time_captions(text: str, start: float, audio_duration: float) -> list[dict]:
    if not math.isfinite(start) or start < 0 or not math.isfinite(audio_duration) or audio_duration <= 0:
        raise ValueError("Caption start and duration must be finite, with positive duration")
    chunks = caption_chunks(text)
    if not chunks:
        return []
    weights = [max(1, len(chunk.split())) for chunk in chunks]
    total = sum(weights)
    result = []
    cursor = start
    for index, (chunk, weight) in enumerate(zip(chunks, weights)):
        end = start + audio_duration if index == len(chunks) - 1 else cursor + audio_duration * weight / total
        result.append({"start": round(cursor, 6), "end": round(end, 6), "text": chunk})
        cursor = end
    return result


def subtitle_timestamp(seconds: float, vtt: bool = False) -> str:
    ms = max(0, round(seconds * 1000))
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    whole, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole:02d}{'.' if vtt else ','}{ms:03d}"


def write_subtitles(captions: list[dict], destination: Path) -> tuple[Path, Path]:
    srt = destination / "captions.srt"
    vtt = destination / "captions.vtt"
    srt.write_text("\n\n".join(
        f"{index}\n{subtitle_timestamp(c['start'])} --> {subtitle_timestamp(c['end'])}\n{c['text']}"
        for index, c in enumerate(captions, 1)) + "\n", encoding="utf-8")
    vtt.write_text("WEBVTT\n\n" + "\n\n".join(
        f"{subtitle_timestamp(c['start'], True)} --> {subtitle_timestamp(c['end'], True)}\n{c['text']}"
        for c in captions) + "\n", encoding="utf-8")
    return srt, vtt


def create_narration(text: str, output: Path, *, voice: str | None = None, rate: int = 155,
                     external_audio: Path | None = None, speech_settings: dict | None = None) -> float:
    if external_audio is not None:
        if not external_audio.is_file():
            raise FileNotFoundError(f"Narration file missing: {external_audio}")
        _run(["ffmpeg", "-y", "-v", "error", "-i", str(external_audio), "-ar", "48000", "-ac", "1", str(output)])
    else:
        settings = speech_settings if speech_settings is not None else voice_settings(voice=voice, rate=rate)
        synthesize(text, output, settings)
    return media_duration(output)


def _encode_scene(painter: ScenePainter, audio: Path, output: Path, fps: int,
                  captions: list[dict], offset: float, scene_frames: int) -> None:
    command = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{painter.width}x{painter.height}", "-r", str(fps), "-i", "pipe:0",
               "-i", str(audio), "-af", "apad", "-t", f"{scene_frames/fps:.6f}",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "1",
               "-movflags", "+faststart", str(output)]
    with tempfile.TemporaryFile() as error:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=error)
        try:
            caption_index = 0
            for number in range(scene_frames):
                elapsed = number / fps
                absolute = offset + elapsed
                while caption_index < len(captions) and absolute >= captions[caption_index]["end"]:
                    caption_index += 1
                caption = (captions[caption_index]["text"] if caption_index < len(captions)
                           and absolute >= captions[caption_index]["start"] else "")
                process.stdin.write(painter.frame(elapsed, caption).tobytes())
            process.stdin.close()
            code = process.wait()
            if code:
                error.seek(0)
                raise RuntimeError(f"FFmpeg encoding failed: {error.read().decode(errors='replace')[-3000:]}")
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait()
            raise


def _preview_html(script: dict, outputs: dict, destination: Path, report: dict) -> Path:
    escape = html.escape
    blocks = []
    for name, item in outputs.items():
        filename = escape(Path(item["path"]).name, quote=True)
        poster = escape(Path(item["poster"]).name, quote=True)
        blocks.append(f'''<section><h2>{escape(name.title())}</h2>
<video controls preload="metadata" poster="{poster}" playsinline><source src="{filename}" type="video/mp4"><track kind="captions" src="captions.vtt" srclang="en" label="English"></video>
<p>{item['width']} × {item['height']} · {item['duration_seconds']:.1f} seconds · <a href="{filename}" download>Download MP4</a></p></section>''')
    credits = script.get("credits", {})
    source_url = str(credits.get("source_url", ""))
    if urlparse(source_url).scheme not in {"https", "http"}:
        source_url = "#"
    limitations = "".join(f"<li>{escape(str(item))}</li>" for item in script.get("limitations", []))
    speech = report.get("voice", {})
    if speech.get("provider") == "external" or speech.get("engine") == "external audio":
        voice_note = "Narration supplied by the operator; confirm its voice disclosure during review."
    elif speech.get("provider") == "kokoro":
        voice_note = f"Synthetic narration by the local Kokoro neural speech engine, voice {escape(str(speech.get('voice', '')))}."
    else:
        voice_note = "Synthetic narration by an explicitly selected local system speech engine."
    teaching_note = ('<p class="notice">Each idea, three passes: <strong>technical → plain language → simplest</strong>.</p>'
                     if script.get("teaching_format") == "three_levels" else "")
    page = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(script['title'])} — Alignment Daily draft</title><style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;background:{PREVIEW_BACKGROUND};color:{PREVIEW_TEXT};font:17px/1.6 system-ui,sans-serif}}main{{max-width:1160px;margin:0 auto;padding:48px 24px}}.eyebrow{{font-size:13px;letter-spacing:.13em;font-weight:700}}h1{{font-size:clamp(30px,5vw,52px);line-height:1.1;max-width:850px}}h2{{font-size:22px}}.notice{{background:{PREVIEW_PANEL};padding:18px 22px;border-radius:12px}}.grid{{display:grid;grid-template-columns:1.5fr 1fr;gap:28px;align-items:start}}video{{width:100%;display:block;max-height:650px;background:{PREVIEW_PANEL};border-radius:14px}}a{{color:{PREVIEW_ACCENT}}}section{{min-width:0}}li{{margin-bottom:7px}}footer{{border-top:1px solid {PREVIEW_LINE};margin-top:34px;padding-top:20px;font-size:14px}}@media(max-width:760px){{.grid{{display:block}}section{{margin-top:30px}}main{{padding:28px 18px}}}}
</style><main><p class="eyebrow">ALIGNMENT DAILY · VIDEO STUDIO</p><h1>{escape(script['title'])}</h1><p>{escape(str(script.get('description','')))}</p>
<p class="notice"><strong>Draft for human review.</strong> {voice_note} Diagrams are explanatory illustrations. No video has been posted to YouTube, X, or another platform.</p>
{teaching_note}<div class="grid">{''.join(blocks)}</div><h2>Source and limits</h2><p><a href="{escape(source_url,quote=True)}">{escape(str(credits.get('title',script['title'])))}</a><br>{escape(', '.join(credits.get('authors',[])))}</p><ul>{limitations}</ul>
<p><a href="script.json">Evidence-linked script</a> · <a href="manifest.json">Production manifest</a> · <a href="captions.srt">SRT captions</a> · <a href="narration.wav">Narration audio</a></p>
<footer>{escape(CAPTION_TIMING)} Review the original source, factual claims, limitations, narration pronunciation, and final captions before publication.</footer></main></html>'''
    path = destination / "index.html"
    path.write_text(page, encoding="utf-8")
    return path


def render_video(script: dict, output_dir: Path, formats: tuple[str, ...] = ("landscape", "portrait"),
                 voice: str | None = None, rate: int = 155, fps: int = 24, preview: bool = False,
                 max_duration: float = 120, audio_dir: Path | None = None,
                 speech_provider: str = "kokoro", speech_speed: float | None = None) -> dict:
    """Render a script already validated by the editorial pipeline.

    Raises before video rendering when measured narration exceeds the duration
    budget. A voice is never accelerated to make an overlong script fit.
    """
    if not 80 <= rate <= 190:
        raise ValueError("Narration rate must be between 80 and 190 words per minute")
    if not 1 <= fps <= 60:
        raise ValueError("Frame rate must be between 1 and 60")
    if not 0 < max_duration <= 120:
        raise ValueError("The duration limit must be positive and at most 120 seconds")
    if not formats or any(name not in CANVASES for name in formats) or len(set(formats)) != len(formats):
        raise ValueError("Formats must be unique landscape and/or portrait")
    scenes = script.get("scenes", [])
    if not scenes or len(scenes) > 12:
        raise ValueError("A video needs 1–12 scenes")
    if any(not str(scene.get("narration", "")).strip() or scene.get("visual") not in VISUALS for scene in scenes):
        raise ValueError("Every scene needs narration and a supported diagram visual")
    speech = ({"provider": "external", "engine": "external audio", "voice": None, "rate": None,
               "synthetic": None, "pause_seconds": .32} if audio_dir else
              voice_settings(speech_provider=speech_provider, voice=voice, rate=rate, speech_speed=speech_speed))
    if Image is None:
        raise RuntimeError("Pillow is required for rendering. Install requirements-video.txt.")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and ffprobe are required to render videos.")
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    narration_dir = output_dir / "audio"
    narration_dir.mkdir(exist_ok=True)
    audio_dir = Path(audio_dir).resolve() if audio_dir else None
    timeline, captions = [], []
    cursor = 0.0
    for index, scene in enumerate(scenes):
        audio = narration_dir / f"scene-{index+1:02d}.wav"
        external = audio_dir / f"scene-{index+1:02d}.wav" if audio_dir else None
        duration = create_narration(str(scene["narration"]), audio, voice=voice, rate=rate, external_audio=external,
                                    speech_settings=speech)
        # Small natural pauses, quantized to whole frames so concatenation and
        # caption offsets share exactly the same clock.
        pause = speech["pause_seconds"]
        if script.get("teaching_format") == "three_levels":
            if scene.get("explanation_level") in {"technical", "plain"}:
                pause = max(pause, TEACHING_PAUSES["within_concept_seconds"])
            elif scene.get("explanation_level") == "simple":
                pause = max(pause, TEACHING_PAUSES["between_concepts_seconds"])
        frames = math.ceil((duration + pause) * fps)
        slot_duration = frames / fps
        captions.extend(time_captions(str(scene["narration"]), cursor, duration))
        timeline.append({"scene_id": scene.get("id", f"scene-{index+1}"), "start": round(cursor, 6),
                         "audio_duration": duration, "duration": slot_duration, "pause_seconds": pause,
                         "end": round(cursor+slot_duration, 6), "frames": frames,
                         "audio_path": str(audio), "visual": scene["visual"],
                         "claim_ids": scene.get("claim_ids", []), "concept_id": scene.get("concept_id"),
                         "explanation_level": scene.get("explanation_level")})
        cursor += slot_duration
    if cursor > max_duration:
        raise ValueError(f"Measured narration and pauses run {cursor:.2f}s, above the {max_duration:.0f}s limit. Shorten the script; speech has not been sped up.")
    srt, vtt = write_subtitles(captions, output_dir)
    (output_dir / "narration.txt").write_text("\n\n".join(str(scene["narration"]) for scene in scenes)+"\n", encoding="utf-8")
    (output_dir / "script.json").write_text(json.dumps(script, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    outputs: dict[str, dict] = {}
    dimensions = PREVIEW_CANVASES if preview else CANVASES
    display_script = dict(script, _narration_label="SUPPLIED NARRATION" if audio_dir else "AI NARRATION")
    with tempfile.TemporaryDirectory(prefix="alignment-render-") as temporary:
        temporary = Path(temporary)
        for format_name in formats:
            width, height = dimensions[format_name]
            parts = []
            for index, (scene, timing) in enumerate(zip(scenes, timeline)):
                painter = ScenePainter(display_script, scene, index, width, height, timing["duration"])
                part = temporary / f"{format_name}-{index:02d}.mp4"
                scene_captions = [caption for caption in captions if timing["start"] <= caption["start"] < timing["end"]]
                _encode_scene(painter, Path(timing["audio_path"]), part, fps, scene_captions, timing["start"], timing["frames"])
                parts.append(part)
                if index == 0:
                    poster = output_dir / f"{format_name}-poster.png"
                    painter.frame(min(2, timing["duration"]*.5), scene_captions[0]["text"] if scene_captions else "").save(poster)
            concat = temporary / f"{format_name}.txt"
            concat.write_text("".join(f"file '{part.name}'\n" for part in parts), encoding="utf-8")
            destination = output_dir / f"{format_name}.mp4"
            _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
                  "-c", "copy", "-movflags", "+faststart", str(destination)])
            measured = media_duration(destination)
            if measured > max_duration + .01:
                destination.unlink()
                raise ValueError(f"Encoded {format_name} duration {measured:.3f}s exceeds the {max_duration:.0f}s limit")
            outputs[format_name] = {"path": str(destination), "poster": str(output_dir / f"{format_name}-poster.png"),
                                    "width": width, "height": height, "duration_seconds": measured,
                                    "bytes": destination.stat().st_size,
                                    "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
        # Extract the published audio from a final video, preserving its scene
        # pauses and exact timing. This avoids an independently drifting concat.
        _run(["ffmpeg", "-y", "-v", "error", "-i", next(iter(outputs.values()))["path"],
              "-vn", "-c:a", "pcm_s16le", str(output_dir / "narration.wav")])
    report = {"schema_version": 1, "article_id": script.get("article_id"),
              "source_sha256": script.get("source_sha256"), "status": "draft_for_review", "publishing_approved": False,
              "title": script.get("title"), "voice": ({"synthetic": True, **speech} if not audio_dir else speech),
              "visual_design": visual_settings(), "teaching_format": script.get("teaching_format"),
              "teaching_pauses": TEACHING_PAUSES if script.get("teaching_format") == "three_levels" else None,
              "fps": fps, "caption_timing": CAPTION_TIMING, "timeline": timeline, "captions": captions,
              "formats": outputs, "duration_seconds": max(item["duration_seconds"] for item in outputs.values()),
              "narration_path": str(output_dir / "narration.wav"), "srt_path": str(srt), "vtt_path": str(vtt),
              "review_required": ["source grounding", "claims and caveats", "voice pronunciation", "caption timing", "visual clarity", "publication approval"]}
    manifest = output_dir / "manifest.json"
    report["manifest_path"] = str(manifest)
    report["preview_path"] = str(output_dir / "index.html")
    manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    _preview_html(script, outputs, output_dir, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("script", type=Path, help="Reviewed, evidence-linked script JSON")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--format", action="append", choices=sorted(CANVASES))
    parser.add_argument("--speech-provider", choices=["kokoro", "system"], default="kokoro")
    parser.add_argument("--voice", help="Voice ID; defaults to af_heart for Kokoro or Samantha on macOS")
    parser.add_argument("--rate", type=int, default=155, help="System speech words per minute; use --speech-speed for Kokoro")
    parser.add_argument("--speech-speed", type=float, help="Kokoro speech speed factor")
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--preview", action="store_true", help="720p landscape / 720×1280 portrait")
    parser.add_argument("--audio-dir", type=Path, help="Use supplied scene-01.wav etc. instead of local TTS")
    args = parser.parse_args()
    script = json.loads(args.script.read_text(encoding="utf-8"))
    report = render_video(script, args.output_dir, tuple(args.format or ["landscape", "portrait"]),
                          voice=args.voice, rate=args.rate, fps=args.fps, preview=args.preview, audio_dir=args.audio_dir,
                          speech_provider=args.speech_provider, speech_speed=args.speech_speed)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, FileNotFoundError, subprocess.CalledProcessError) as error:
        print(f"Video rendering failed: {error}", file=sys.stderr)
        raise SystemExit(1)
