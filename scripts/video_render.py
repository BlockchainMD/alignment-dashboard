#!/usr/bin/env python3
"""Render reviewed research scripts as original, captioned diagram explainers.

No generation service is required: Pillow draws the diagrams, a local speech
engine provides a disclosed synthetic voice, and FFmpeg encodes the video.
Caption chunks are timed proportionally within measured scene audio. They are
not forced-aligned, word-accurate transcripts.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from functools import lru_cache
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from urllib.parse import urlparse

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # Pure caption/timing helpers remain usable in collection CI.
    Image = ImageDraw = ImageFont = None


CREAM = "#F5F1E8"
PAPER = "#FFFCF5"
GREEN = "#173E35"
MUTED = "#53665B"
LINE = "#D3D9CD"
OCHRE = "#BA8237"
PALE = "#E6EBD9"
RED = "#A34E43"
WHITE = "#FFFFFF"
VISUALS = {"neuron", "analogy", "comparison", "test", "takeaway", "source"}
CANVASES = {"landscape": (1920, 1080), "portrait": (1080, 1920)}
PREVIEW_CANVASES = {"landscape": (1280, 720), "portrait": (720, 1280)}
CAPTION_TIMING = "Measured scene audio; proportional phrase timing, not word alignment."


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


@lru_cache(maxsize=256)
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = ([os.environ.get("ALIGNMENT_VIDEO_FONT_BOLD", ""),
                   "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else
                  [os.environ.get("ALIGNMENT_VIDEO_FONT", ""),
                   "/System/Library/Fonts/Supplemental/Arial.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=max(8, size))
    return ImageFont.truetype("DejaVuSans.ttf", size=max(8, size))


def wrap_text(text: str, face: ImageFont.ImageFont, width: float, max_lines: int | None = None) -> list[str]:
    """Wrap to a measured pixel width, including overlong single words."""
    width = max(1, width)
    lines: list[str] = []
    current = ""
    for word in str(text).split():
        candidate = f"{current} {word}".strip()
        if face.getlength(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        while face.getlength(word) > width and len(word) > 1:
            split = 1
            while split < len(word) and face.getlength(word[:split + 1]) <= width:
                split += 1
            lines.append(word[:split])
            word = word[split:]
        current = word
    if current:
        lines.append(current)
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        tail = lines[-1]
        while tail and face.getlength(tail + "…") > width:
            tail = tail[:-1].rstrip()
        lines[-1] = tail + "…"
    return lines


def _text(draw: ImageDraw.ImageDraw, text: str, box: tuple[float, float, float, float],
          size: int, fill: str = GREEN, bold: bool = False, align: str = "left",
          max_lines: int = 3, min_size: int | None = None) -> None:
    x, y, width, height = box
    min_size = min_size or max(12, int(size * .62))
    chosen = font(size, bold)
    while size > min_size:
        chosen = font(size, bold)
        lines = wrap_text(text, chosen, width)
        if len(lines) <= max_lines and len(lines) * size * 1.2 <= height:
            break
        size -= 1
    lines = wrap_text(text, chosen, width, max_lines)
    line_height = size * 1.2
    for index, line in enumerate(lines):
        tx = x if align == "left" else x + (width - chosen.getlength(line)) / 2
        draw.text((int(tx), int(y + index * line_height)), line, font=chosen, fill=fill)


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


def create_narration(text: str, output: Path, *, voice: str, rate: int,
                     external_audio: Path | None = None) -> float:
    if external_audio is not None:
        if not external_audio.is_file():
            raise FileNotFoundError(f"Narration file missing: {external_audio}")
        _run(["ffmpeg", "-y", "-v", "error", "-i", str(external_audio), "-ar", "48000", "-ac", "1", str(output)])
    elif shutil.which("say"):
        with tempfile.TemporaryDirectory(prefix="alignment-speech-") as temp:
            text_path = Path(temp) / "narration.txt"
            source = Path(temp) / "voice.aiff"
            text_path.write_text(text, encoding="utf-8")
            _run(["say", "-v", voice, "-r", str(rate), "-f", str(text_path), "-o", str(source)])
            _run(["ffmpeg", "-y", "-v", "error", "-i", str(source), "-ar", "48000", "-ac", "1", str(output)])
    elif shutil.which("espeak-ng") or shutil.which("espeak"):
        executable = shutil.which("espeak-ng") or shutil.which("espeak")
        _run([str(executable), "-s", str(rate), "-w", str(output), "--stdin"], input=text.encode("utf-8"))
    else:
        raise RuntimeError("Install a local speech engine (macOS say or espeak-ng), or provide --audio-dir.")
    return media_duration(output)


def _ease(value: float) -> float:
    value = min(1., max(0., value))
    return value * value * (3 - 2 * value)


def _line(draw: ImageDraw.ImageDraw, a: tuple[float, float], b: tuple[float, float],
          progress: float, color: str = GREEN, width: int = 4, arrow: bool = False) -> None:
    if progress <= 0:
        return
    progress = min(1, progress)
    end = (a[0] + (b[0] - a[0]) * progress, a[1] + (b[1] - a[1]) * progress)
    draw.line((a, end), fill=color, width=max(1, width))
    if arrow and progress > .96:
        angle = math.atan2(b[1] - a[1], b[0] - a[0])
        length = width * 3
        points = [end,
                  (end[0] - length * math.cos(angle - .5), end[1] - length * math.sin(angle - .5)),
                  (end[0] - length * math.cos(angle + .5), end[1] - length * math.sin(angle + .5))]
        draw.polygon(points, fill=color)


def _card(draw: ImageDraw.ImageDraw, box: tuple[float, float, float, float], *, fill: str = PAPER,
          outline: str = LINE, width: int = 2, radius: int = 20) -> None:
    x, y, w, h = box
    draw.rounded_rectangle((int(x), int(y), int(x + w), int(y + h)), radius=radius,
                           fill=fill, outline=outline, width=width)


def _labels(scene: dict, defaults: list[str], count: int = 3) -> list[str]:
    labels = [str(value) for value in scene.get("labels", []) if str(value).strip()]
    return (labels + defaults[len(labels):])[:count]


def comparison_mode(scene: dict) -> str:
    """Choose meaningful schematic marks without adding article-specific facts."""
    text = " ".join([str(scene.get("heading", ""))] + [str(label) for label in scene.get("labels", [])]).lower()
    if "probabil" in text:
        return "probability"
    if any(term in text for term in ("response", "strong", "activation")):
        return "activation"
    return "generic"


def analogy_mode(scene: dict) -> str:
    """Reserve the literal signal for scripts that actually use that analogy."""
    text = " ".join([str(scene.get("heading", "")), str(scene.get("narration", ""))] +
                    [str(label) for label in scene.get("labels", [])]).lower()
    if re.search(r"\btraffic[\s-]*lights?\b|\bintersection\b|\bred\b.{0,16}\bstopped\s+cars\b", text):
        return "traffic_light"
    return "generic"


def _diagram(image: Image.Image, scene: dict, box: tuple[float, float, float, float],
             elapsed: float, scene_duration: float, portrait: bool, credits: dict) -> None:
    draw = ImageDraw.Draw(image)
    x, y, w, h = box
    scale = image.width / (720 if portrait else 1280)
    line = max(2, round(3 * scale))
    size = round((25 if portrait else 27) * scale)
    radius = round(16 * scale)
    reveal = _ease(elapsed / 1.5)
    kind = scene.get("visual", "neuron")
    labels = _labels(scene, ["Training", "New situation", "Check behavior"])
    if kind == "neuron":
        layers = [2, 3, 3, 2]
        nx, ny, nw, nh = x + w * .16, y + h * .18, w * .68, h * .56
        nodes = [[(nx + index * nw / 3, ny + (j + 1) * nh / (count + 1)) for j in range(count)]
                 for index, count in enumerate(layers)]
        for index in range(3):
            for start in nodes[index]:
                for end in nodes[index + 1]:
                    _line(draw, start, end, _ease(elapsed / 1.2 - index * .25), LINE, line)
        pulse = ((elapsed * .35) % 1) * 3
        for index, layer in enumerate(nodes):
            for j, (cx, cy) in enumerate(layer):
                r = (13 if portrait else 17) * scale
                active = abs(index - pulse) < .6 and j == index % len(layer)
                draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=OCHRE if active else GREEN)
        _text(draw, labels[0], (x, y, w * .46, h * .16), size, bold=True, max_lines=2)
        _text(draw, labels[1], (x + w * .54, y, w * .46, h * .16), size, bold=True, align="center", max_lines=2)
        _card(draw, (x + w * .12, y + h * .79, w * .76, h * .19), fill=PALE, radius=radius)
        _text(draw, labels[2], (x + w * .15, y + h * .82, w * .70, h * .14), size, align="center", max_lines=2)
    elif kind == "analogy" and analogy_mode(scene) == "generic":
        # Labels come from the reviewed script. An unrelated analogy must never
        # silently acquire a literal traffic signal or another invented object.
        for index, label in enumerate(labels):
            if portrait:
                bx, by, bw, bh = x+w*.08, y+h*(.02+index*.33), w*.84, h*.25
            else:
                bx, by, bw, bh = x+w*(index*.35), y+h*.16, w*.30, h*.62
            offset = (1-_ease(elapsed/1.4-index*.15))*h*.04
            by += offset
            _card(draw, (bx, by, bw, bh), fill=PALE if index == 1 else PAPER, radius=radius)
            if portrait:
                number_box = (bx+bw*.035, by+bh*.26, bw*.18, bh*.5)
                label_box = (bx+bw*.25, by+bh*.23, bw*.69, bh*.59)
            else:
                number_box = (bx+bw*.05, by+bh*.10, bw*.90, bh*.24)
                label_box = (bx+bw*.08, by+bh*.46, bw*.84, bh*.43)
            _text(draw, f"0{index+1}", number_box, round(23*scale), fill=OCHRE, bold=True,
                  align="center", max_lines=1)
            if elapsed >= index*.35:
                _text(draw, label, label_box, size, bold=True,
                      align="left" if portrait else "center", max_lines=3)
            if index < 2:
                if portrait:
                    _line(draw, (x+w*.5, by+bh+h*.014), (x+w*.5, by+bh+h*.065),
                          _ease(elapsed-index*.35), GREEN, line, True)
                else:
                    _line(draw, (bx+bw+w*.012, y+h*.47), (bx+bw+w*.043, y+h*.47),
                          _ease(elapsed-index*.35), GREEN, line, True)
        if not portrait:
            _text(draw, "ANALOGY · ILLUSTRATIVE", (x+w*.05, y+h*.88, w*.90, h*.1),
                  round(13*scale), fill=OCHRE, bold=True, align="center", max_lines=1)
    elif kind == "analogy":
        # A traffic signal illustrates a rule and a changing situation. This is
        # marked as an analogy; it does not depict a measured paper result.
        cw, ch = w * .25, h * .67
        sx, sy = x + w * .375, y + h * .03
        _card(draw, (sx, sy, cw, ch), fill=GREEN, outline=GREEN, radius=radius)
        for index, color in enumerate([RED, OCHRE, "#8AAC80"]):
            cx, cy = sx + cw / 2, sy + ch * (.2 + index * .29)
            r = min(cw * .22, ch * .1)
            # Hold red through the opening explanation, then use the familiar
            # red → green → amber order as the situation changes.
            phase = 0 if elapsed < scene_duration*.55 else 2 if elapsed < scene_duration*.83 else 1
            active = index == phase
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color if active else "#36574A")
            if active:
                draw.ellipse((cx-r*1.3, cy-r*1.3, cx+r*1.3, cy+r*1.3), outline=CREAM, width=line)
        _line(draw, (x + w * .04, y + h * .43), (sx - w * .04, y + h * .43), reveal, OCHRE, line, True)
        _line(draw, (sx + cw + w * .04, y + h * .43), (x + w * .96, y + h * .43), _ease(elapsed / 1.5 - .4), GREEN, line, True)
        _text(draw, labels[0], (x, y + h * .06, w * .32, h * .27), size, bold=True, align="center", max_lines=3)
        _text(draw, labels[1], (x + w * .69, y + h * .06, w * .31, h * .27), size, bold=True, align="center", max_lines=3)
        _text(draw, labels[2], (x + w * .03, y + h * .79, w * .94, h * .2), size, bold=True, align="center", max_lines=2)
    elif kind == "comparison":
        gap = w * .06
        cw = (w - gap) / 2
        mode = comparison_mode(scene)
        for index in range(2):
            cx = x + index * (cw + gap)
            offset = (1 - _ease(elapsed / 1.5 - index * .2)) * h * .1
            cy = y + h * .04 + offset
            _card(draw, (cx, cy, cw, h * .7), fill=PAPER if index == 0 else PALE, radius=radius)
            _text(draw, labels[index], (cx + cw * .08, cy + h * .07, cw * .84, h * .22), size, bold=True, align="center", max_lines=3)
            if mode == "probability":
                # Deliberately invented toy profiles explain what is compared;
                # they are explicitly marked and contain no numerical results.
                baseline = cy + h * .61
                heights = [.25, .60, .15]
                if index:
                    delta = (.5 + .5 * math.sin(elapsed * .65)) * .18
                    heights = [.25 + delta, .60 - delta, .15]
                draw.line((cx+cw*.12, baseline, cx+cw*.88, baseline), fill=LINE, width=line)
                for j, value in enumerate(heights):
                    px = cx + cw * (.22+j*.28)
                    bw = cw*.13
                    bh = h*.38*value*_ease(elapsed/1.5)
                    draw.rounded_rectangle((px-bw/2, baseline-bh, px+bw/2, baseline),
                                           radius=max(2, round(4*scale)), fill=GREEN if index == 0 else OCHRE)
                    _text(draw, "ABC"[j], (px-cw*.08, baseline+h*.015, cw*.16, h*.065),
                          round(13*scale), fill=MUTED, align="center", max_lines=1)
            elif mode == "activation":
                baseline = cy + h*.59
                if index == 0:
                    # The same symbolic pattern need not switch every neuron on.
                    for j in range(3):
                        px, py = cx+cw*(.22+j*.28), cy+h*.47
                        rr = min(cw*.065, h*.065)
                        draw.ellipse((px-rr, py-rr, px+rr, py+rr), fill=GREEN if j == 0 else PAPER,
                                     outline=GREEN, width=line)
                        if j == 0:
                            pulse = rr*(1.35+.15*math.sin(elapsed*2))
                            draw.ellipse((px-pulse, py-pulse, px+pulse, py+pulse), outline=OCHRE, width=max(1,line//2))
                        _text(draw, "ON" if j == 0 else "OFF", (px-cw*.10, baseline, cw*.20, h*.07),
                              round(12*scale), fill=MUTED, align="center", max_lines=1)
                else:
                    for j, value in enumerate([.30, .90]):
                        px = cx+cw*(.3+j*.4)
                        bw = cw*.16
                        bh = h*.26*value*_ease(elapsed/1.5)
                        draw.rounded_rectangle((px-bw/2, baseline-bh, px+bw/2, baseline),
                                               radius=max(2, round(4*scale)), fill=OCHRE)
                        _text(draw, "weak" if j == 0 else "strong", (px-cw*.18, baseline+h*.025, cw*.36, h*.07),
                              round(12*scale), fill=MUTED, align="center", max_lines=1)
            else:
                # Unquantified symbolic environments for other research topics.
                for j in range(3):
                    px = cx + cw * (.22 + j * .28)
                    py = cy + h * .48
                    rr = min(cw * .06, h * .06)
                    if index == 0:
                        draw.ellipse((px-rr, py-rr, px+rr, py+rr), fill=GREEN)
                    else:
                        rotation = elapsed * .3 + j
                        points = [(px + math.cos(rotation + q * math.pi/2) * rr * 1.4,
                                   py + math.sin(rotation + q * math.pi/2) * rr * 1.4) for q in range(4)]
                        draw.polygon(points, fill=OCHRE)
        if mode != "generic":
            _text(draw, "TOY EXAMPLE · NO MEASURED DATA", (x+w*.04, y+h*.77, w*.92, h*.07),
                  round(12*scale), fill=OCHRE, bold=True, align="center", max_lines=1)
            label_y = .87
        else:
            label_y = .82
        _text(draw, labels[2], (x + w * .04, y + h * label_y, w * .92, h * .13), size, bold=True, align="center", max_lines=2)
    elif kind == "test":
        labels = _labels(scene, ["Ask", "Check", "Repeat"])
        vertical = portrait
        for index, label in enumerate(labels):
            if vertical:
                bx, by, bw, bh = x + w * .1, y + h * (.02 + index * .33), w * .8, h * .25
            else:
                bx, by, bw, bh = x + index * w * .35, y + h * .2, w * .30, h * .57
            _card(draw, (bx, by, bw, bh), fill=PALE if index == 1 else PAPER, radius=radius)
            circle_r = min(bw, bh) * .16
            cx = bx + (bw * .13 if vertical else bw * .5)
            cy = by + (bh * .5 if vertical else bh * .27)
            draw.ellipse((cx-circle_r, cy-circle_r, cx+circle_r, cy+circle_r), fill=OCHRE if index == 1 else GREEN)
            _text(draw, str(index + 1), (cx-circle_r, cy-circle_r*.85, circle_r*2, circle_r*2), round(circle_r*1.25), fill=WHITE, bold=True, align="center", max_lines=1)
            label_box = ((bx+bw*.27, by+bh*.26, bw*.66, bh*.55) if vertical else
                         (bx+bw*.08, by+bh*.56, bw*.84, bh*.34))
            if elapsed > index * .5:
                _text(draw, label, label_box, size, bold=True, align="left" if vertical else "center", max_lines=3)
            if index < 2:
                if vertical:
                    _line(draw, (x+w*.5, by+bh+h*.015), (x+w*.5, by+bh+h*.065), _ease(elapsed-index*.5), GREEN, line, True)
                else:
                    _line(draw, (bx+bw+w*.012, y+h*.48), (bx+bw+w*.043, y+h*.48), _ease(elapsed-index*.5), GREEN, line, True)
    elif kind == "takeaway":
        cx, cy = x+w*.5, y+h*.32
        r = min(w, h) * .25
        for index in reversed(range(3)):
            rr = r * (index + 1) / 3
            draw.ellipse((cx-rr, cy-rr, cx+rr, cy+rr), outline=GREEN if index%2 == 0 else OCHRE, width=line)
        # Moving focus point suggests inspection, not a measured result.
        angle = min(elapsed, 3) * .9
        pr = r * max(.05, .7 - elapsed * .13)
        px, py = cx+math.cos(angle)*pr, cy+math.sin(angle)*pr
        dot = 7*scale
        draw.ellipse((px-dot, py-dot, px+dot, py+dot), fill=OCHRE)
        _text(draw, labels[0], (x+w*.01, y+h*.65, w*.98, h*.15), size+round(3*scale), bold=True, align="center", max_lines=2)
        _text(draw, " · ".join(labels[1:]), (x+w*.04, y+h*.84, w*.92, h*.15), size-round(3*scale), fill=MUTED, align="center", max_lines=2)
    else:
        _card(draw, (x + w*.03, y, w*.94, h*.96), fill=PAPER, radius=radius)
        _text(draw, "READ THE ORIGINAL", (x+w*.09, y+h*.07, w*.82, h*.12), round(19*scale), fill=OCHRE, bold=True, max_lines=1)
        _text(draw, str(credits.get("title") or scene.get("heading", "Research source")),
              (x+w*.09, y+h*.25, w*.82, h*.31), round(31*scale), bold=True, max_lines=3)
        authors = credits.get("authors") or []
        _text(draw, ", ".join(authors[:4]) + (" et al." if len(authors) > 4 else ""),
              (x+w*.09, y+h*.63, w*.82, h*.13), round(20*scale), fill=MUTED, max_lines=2)
        source = urlparse(str(credits.get("source_url", ""))).netloc
        _text(draw, source, (x+w*.09, y+h*.82, w*.82, h*.1), round(20*scale), fill=GREEN, max_lines=1)


class ScenePainter:
    """Cache expensive text and background work; animate the diagram each frame."""
    def __init__(self, script: dict, scene: dict, index: int, width: int, height: int, duration: float):
        self.script, self.scene, self.index = script, scene, index
        self.width, self.height, self.duration = width, height, duration
        self.portrait = height > width
        self.scale = width / (720 if self.portrait else 1280)
        self.base = Image.new("RGB", (width, height), CREAM)
        draw = ImageDraw.Draw(self.base)
        x = width * .075
        # Texture-free backgrounds keep compression clear and small.
        draw.line((x, height*.115, width-x, height*.115), fill=LINE, width=max(1, round(self.scale)))
        _text(draw, "ALIGNMENT DAILY", (x, height*.055, width*.6, height*.05), round(18*self.scale), bold=True, max_lines=1)
        _text(draw, f"{index+1:02d} / {len(script['scenes']):02d}", (width*.76, height*.055, width*.16, height*.05), round(18*self.scale), fill=MUTED, align="center", max_lines=1)
        tag = ("ANALOGY" if scene.get("visual") == "analogy" else
               "SOURCE & CONTEXT" if scene.get("visual") == "source" else
               "RESEARCH DISCUSSION" if script.get("research_type") == "commentary" else
               "RESEARCH, EXPLAINED")
        _text(draw, tag, (x, height*.15, width*.85, height*.035), round(14*self.scale), fill=OCHRE, bold=True, max_lines=1)
        _text(draw, str(scene.get("heading", "")), (x, height*.202, width*.85, height*(.095 if not self.portrait else .115)),
              round((43 if not self.portrait else 44)*self.scale), bold=True, max_lines=2)
        self.diagram_box = (x, height*(.335 if self.portrait else .34), width*.85, height*(.365 if self.portrait else .365))
        self.caption_box = (x, height*.773, width*.85, height*.151)
        _card(draw, self.caption_box, fill=GREEN, outline=GREEN, radius=round(16*self.scale))
        narration_label = script.get("_narration_label", "AI NARRATION")
        _text(draw, f"{narration_label} · ILLUSTRATIVE DIAGRAMS · SOURCE LINKED BELOW", (x, height*.949, width*.86, height*.025),
              round(11*self.scale), fill=MUTED, max_lines=1)
        self.caption_layers: OrderedDict[str, Image.Image] = OrderedDict()

    def frame(self, elapsed: float, caption: str) -> Image.Image:
        frame = self.base.copy()
        _diagram(frame, self.scene, self.diagram_box, elapsed, self.duration, self.portrait, self.script.get("credits", {}))
        if caption:
            layer = self.caption_layers.get(caption)
            if layer is None:
                layer = Image.new("RGBA", frame.size)
                draw = ImageDraw.Draw(layer)
                x, y, w, h = self.caption_box
                _text(draw, caption, (x+w*.045, y+h*.15, w*.91, h*.78),
                      round((28 if self.portrait else 31)*self.scale), fill=WHITE, bold=True,
                      max_lines=3 if self.portrait else 2)
                self.caption_layers[caption] = layer
            frame.paste(layer, (0, 0), layer)
        draw = ImageDraw.Draw(frame)
        y = self.height*.98
        progress = (self.index + min(1, elapsed/self.duration)) / len(self.script["scenes"])
        draw.rectangle((0, y, self.width*progress, self.height), fill=OCHRE)
        return frame


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
    voice_note = ("Narration supplied by the operator; confirm its voice disclosure during review." if report.get("voice", {}).get("engine") == "external audio"
                  else "Synthetic narration by a local speech engine.")
    page = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(script['title'])} — Alignment Daily draft</title><style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;background:{CREAM};color:{GREEN};font:17px/1.6 system-ui,sans-serif}}main{{max-width:1160px;margin:0 auto;padding:48px 24px}}.eyebrow{{font-size:13px;letter-spacing:.13em;font-weight:700}}h1{{font-size:clamp(30px,5vw,52px);line-height:1.1;max-width:850px}}h2{{font-size:22px}}.notice{{background:{PALE};padding:18px 22px;border-radius:12px}}.grid{{display:grid;grid-template-columns:1.5fr 1fr;gap:28px;align-items:start}}video{{width:100%;display:block;max-height:650px;background:{GREEN};border-radius:14px}}a{{color:{GREEN}}}section{{min-width:0}}li{{margin-bottom:7px}}footer{{border-top:1px solid {LINE};margin-top:34px;padding-top:20px;font-size:14px}}@media(max-width:760px){{.grid{{display:block}}section{{margin-top:30px}}main{{padding:28px 18px}}}}
</style><main><p class="eyebrow">ALIGNMENT DAILY · VIDEO STUDIO</p><h1>{escape(script['title'])}</h1><p>{escape(str(script.get('description','')))}</p>
<p class="notice"><strong>Draft for human review.</strong> {voice_note} Diagrams are explanatory illustrations. No video has been posted to YouTube, X, or another platform.</p>
<div class="grid">{''.join(blocks)}</div><h2>Source and limits</h2><p><a href="{escape(source_url,quote=True)}">{escape(str(credits.get('title',script['title'])))}</a><br>{escape(', '.join(credits.get('authors',[])))}</p><ul>{limitations}</ul>
<p><a href="script.json">Evidence-linked script</a> · <a href="manifest.json">Production manifest</a> · <a href="captions.srt">SRT captions</a> · <a href="narration.wav">Narration audio</a></p>
<footer>{escape(CAPTION_TIMING)} Review the original source, factual claims, limitations, narration pronunciation, and final captions before publication.</footer></main></html>'''
    path = destination / "index.html"
    path.write_text(page, encoding="utf-8")
    return path


def render_video(script: dict, output_dir: Path, formats: tuple[str, ...] = ("landscape", "portrait"),
                 voice: str = "Samantha", rate: int = 155, fps: int = 24, preview: bool = False,
                 max_duration: float = 120, audio_dir: Path | None = None) -> dict:
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
        duration = create_narration(str(scene["narration"]), audio, voice=voice, rate=rate, external_audio=external)
        # Small natural pauses, quantized to whole frames so concatenation and
        # caption offsets share exactly the same clock.
        frames = math.ceil((duration + .32) * fps)
        slot_duration = frames / fps
        captions.extend(time_captions(str(scene["narration"]), cursor, duration))
        timeline.append({"scene_id": scene.get("id", f"scene-{index+1}"), "start": round(cursor, 6),
                         "audio_duration": duration, "duration": slot_duration,
                         "end": round(cursor+slot_duration, 6), "frames": frames,
                         "audio_path": str(audio), "visual": scene["visual"],
                         "claim_ids": scene.get("claim_ids", [])})
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
              "title": script.get("title"), "voice": {"engine": "external audio" if audio_dir else "macOS say" if shutil.which("say") else "espeak", "name": None if audio_dir else voice if shutil.which("say") else "default English", "rate": rate if not audio_dir else None, "synthetic": None if audio_dir else True},
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
    parser.add_argument("--voice", default="Samantha")
    parser.add_argument("--rate", type=int, default=155)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--preview", action="store_true", help="720p landscape / 720×1280 portrait")
    parser.add_argument("--audio-dir", type=Path, help="Use supplied scene-01.wav etc. instead of local TTS")
    args = parser.parse_args()
    script = json.loads(args.script.read_text(encoding="utf-8"))
    report = render_video(script, args.output_dir, tuple(args.format or ["landscape", "portrait"]),
                          voice=args.voice, rate=args.rate, fps=args.fps, preview=args.preview, audio_dir=args.audio_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, FileNotFoundError, subprocess.CalledProcessError) as error:
        print(f"Video rendering failed: {error}", file=sys.stderr)
        raise SystemExit(1)
