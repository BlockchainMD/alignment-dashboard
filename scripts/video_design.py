"""Reusable editorial art direction for Alignment Daily research videos.

The layout keeps the title, diagram and captions in distinct reading zones. A
shared palette, bundled fonts and content-safe typography apply to every future
script; science-specific illustrations live in video_diagrams. No model/image
API is needed to reproduce a render.
"""
from __future__ import annotations

from collections import OrderedDict
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # Keep the hourly collector's standard-library CI usable.
    Image = ImageDraw = ImageFont = None

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "video-design.json"
DARK_SCENES = {"neuron", "test", "takeaway"}
EYEBROWS = {
    "neuron": "THE QUESTION",
    "analogy": "AN INTUITION",
    "comparison": "LOOK CLOSER",
    "test": "PUT IT TO THE TEST",
    "takeaway": "WHAT TO TAKE AWAY",
    "source": "KEEP EXPLORING",
}


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_design(path: Path | None = None) -> dict:
    """Validate the deliberately small public design configuration."""
    value = json.loads((path or CONFIG_PATH).read_text(encoding="utf-8"))
    expected = {"schema_version", "name", "palette", "layout", "motion"}
    if (not isinstance(value, dict) or set(value) != expected
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise ValueError("Video design must use the supported schema_version 1 and keys")
    if not isinstance(value["name"], str) or not 1 <= len(value["name"]) <= 100:
        raise ValueError("Video design needs a short name")
    palette = value["palette"]
    if (not isinstance(palette, dict) or set(palette) != {"midnight", "ivory", "lime", "aqua", "coral"}
            or any(not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color)
                   for color in palette.values())):
        raise ValueError("Video palette must contain five six-digit hexadecimal colors")
    layout = value["layout"]
    layout_ranges = {"landscape_margin": (.04, .10), "portrait_margin": (.06, .12),
                     "portrait_content_right": (.82, .90),
                     "caption_max_lines_landscape": (2, 3), "caption_max_lines_portrait": (2, 3)}
    motion_ranges = {"intro_seconds": (.2, 1.5), "ambient_speed": (0, .5), "exit_seconds": (0, .5)}
    for group, bounds in ((layout, layout_ranges), (value["motion"], motion_ranges)):
        if not isinstance(group, dict) or set(group) != set(bounds):
            raise ValueError("Video design has unsupported layout or motion keys")
        for key, (low, high) in bounds.items():
            number = group[key]
            if (isinstance(number, bool) or not isinstance(number, (int, float))
                    or not math.isfinite(number) or not low <= number <= high):
                raise ValueError(f"Video design value {key} must be within {low}–{high}")
            if key.startswith("caption_max_lines") and not isinstance(number, int):
                raise ValueError("Caption line counts must be integers")
    if layout["portrait_content_right"] - layout["portrait_margin"] < .68:
        raise ValueError("Portrait content area is too narrow")
    return value


def _font_candidates(bold: bool) -> list[str]:
    return ([os.environ.get("ALIGNMENT_VIDEO_FONT_BOLD", ""),
             str(ROOT / "assets/fonts/Manrope-Bold.ttf"),
             "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else
            [os.environ.get("ALIGNMENT_VIDEO_FONT", ""),
             str(ROOT / "assets/fonts/Manrope-Regular.ttf"),
             "/System/Library/Fonts/Supplemental/Arial.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])


def _font_path(bold: bool) -> Path | None:
    return next((Path(p) for p in _font_candidates(bold) if p and Path(p).is_file()), None)


@lru_cache(maxsize=256)
def font(size: int, bold: bool = False):
    if ImageFont is None:
        raise RuntimeError("Install requirements-video.txt to render video typography")
    path = _font_path(bold)
    return ImageFont.truetype(str(path) if path else "DejaVuSans.ttf", size=max(8, int(size)))


def visual_settings() -> dict:
    """Canonical design inputs, including actual resolved fonts, for cache keys."""
    paths = [Path(__file__), Path(__file__).with_name("video_diagrams.py")]
    fonts = {}
    for label, bold in (("regular", False), ("bold", True)):
        path = _font_path(bold)
        if path:
            bundled = path.is_relative_to(ROOT / "assets/fonts")
            fonts[label] = {"name": str(path.relative_to(ROOT)) if bundled else str(path),
                            "sha256": _hash(path), "bytes": path.stat().st_size,
                            "weight": 700 if bold else 400, "bundled": bundled}
        else:
            fonts[label] = {"name": "DejaVuSans.ttf", "sha256": None, "bundled": False}
    return {"design": load_design(), "modules": {p.name: _hash(p) for p in paths if p.is_file()},
            "fonts": fonts, "renderer": "pillow-science-studio-v1"}


def wrap_text(text: str, face: Any, width: float, max_lines: int | None = None) -> list[str]:
    """Measure wrapping without truncating evidence or caption words.

    max_lines is retained for helper API compatibility; fitting is draw_text's
    job. It must never turn a long spoken caption into an ellipsis.
    """
    del max_lines
    if not math.isfinite(width) or width <= 0:
        raise ValueError("Text width must be positive")
    lines, current = [], ""
    for word in str(text).split():
        candidate = f"{current} {word}".strip()
        if face.getlength(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        while len(word) > 1 and face.getlength(word) > width:
            split = 1
            while split < len(word) and face.getlength(word[:split + 1]) <= width:
                split += 1
            lines.append(word[:split])
            word = word[split:]
        current = word
    if current:
        lines.append(current)
    return lines


def draw_text(draw: Any, text: str, box: tuple[float, float, float, float], size: int,
              fill: str, bold: bool = False, align: str = "left", max_lines: int = 3,
              min_size: int | None = None, *, vertical: str = "top", break_long_words: bool = True) -> dict:
    """Fit all words inside a box, or fail instead of silently hiding content."""
    x, y, width, height = box
    if width <= 0 or height <= 0 or max_lines < 1:
        raise ValueError("Text box must have positive dimensions and line count")
    if align not in {"left", "center", "right"} or vertical not in {"top", "center"}:
        raise ValueError("Unsupported text alignment")
    minimum = max(8, int(min_size or max(8, size * .5)))
    chosen = None
    for actual_size in range(max(minimum, int(size)), minimum - 1, -1):
        face = font(actual_size, bold)
        lines = wrap_text(text, face, width)
        line_height = actual_size * 1.24
        if (len(lines) <= max_lines and len(lines) * line_height <= height
                and all(face.getlength(line) <= width for line in lines)
                and (break_long_words or all(face.getlength(word) <= width for word in str(text).split()))):
            chosen = (actual_size, face, lines, line_height)
            break
    if chosen is None:
        raise ValueError(f"Text does not fit safely in its video layout: {str(text)[:80]}")
    actual_size, face, lines, line_height = chosen
    top = y + ((height - len(lines) * line_height) / 2 if vertical == "center" else 0)
    for index, line in enumerate(lines):
        length = face.getlength(line)
        tx = x + ((width - length) / 2 if align == "center" else width - length if align == "right" else 0)
        draw.text((round(tx), round(top + index * line_height)), line, font=face, fill=fill, anchor="lt")
    return {"font_size": actual_size, "lines": lines, "box": tuple(box),
            "height": len(lines) * line_height}


def _ease(value: float) -> float:
    value = min(1., max(0., value))
    return 1 - (1 - value) ** 3


def _mix(a: str, b: str, amount: float) -> str:
    amount = min(1., max(0., amount))
    channels = [round(int(a[i:i + 2], 16) * (1 - amount) + int(b[i:i + 2], 16) * amount)
                for i in (1, 3, 5)]
    return "#" + "".join(f"{v:02x}" for v in channels)


def _theme(kind: str, config: dict) -> dict:
    p = config["palette"]
    dark = kind in DARK_SCENES
    background, foreground = (p["midnight"], p["ivory"]) if dark else (p["ivory"], p["midnight"])
    return {"background": background, "foreground": foreground,
            "muted": _mix(background, foreground, .60), "line": _mix(background, foreground, .15),
            "panel": _mix(background, foreground, .045), "surface": _mix(background, foreground, .045),
            "accent": p["lime"] if dark else "#53702B", "lime": p["lime"], "ivory": p["ivory"],
            "aqua": p["aqua"] if dark else "#197D7A", "secondary": p["aqua"] if dark else "#197D7A",
            "coral": p["coral"] if dark else "#B95338", "dark": dark}


class ScenePainter:
    """Cache static type/background layers; keep restrained motion alive throughout."""
    def __init__(self, script: dict, scene: dict, index: int, width: int, height: int, duration: float):
        if Image is None:
            raise RuntimeError("Install requirements-video.txt to render videos")
        if (width < 320 or height < 320 or not math.isfinite(duration) or duration <= 0
                or not isinstance(index, int) or not 0 <= index < len(script.get("scenes", []))):
            raise ValueError("Scene needs valid canvas dimensions, index and duration")
        self.script, self.scene, self.index = script, scene, index
        self.width, self.height, self.duration = int(width), int(height), duration
        self.portrait = height > width
        self.scale = width / (1080 if self.portrait else 1920)
        self.design = load_design()
        self.theme = _theme(scene.get("visual", "source"), self.design)
        self.theme["scale"] = self.scale
        t, config = self.theme, self.design
        x = width * config["layout"]["portrait_margin" if self.portrait else "landscape_margin"]
        right = width * (config["layout"]["portrait_content_right"] if self.portrait else .935)
        self.content_box = (x, right)
        self.diagram_box = ((x, height * .365, right - x, height * .325) if self.portrait else
                            (width * .405, height * .205, width * .535, height * .50))
        self.caption_box = ((x, height * .765, right - x, height * .115) if self.portrait else
                            (x, height * .79, right - x, height * .115))
        self.heading_box = ((x, height * .184, right - x, height * .154) if self.portrait else
                            (x, height * .245, width * .307, height * .385))
        self.base = Image.new("RGB", (width, height), t["background"])
        draw = ImageDraw.Draw(self.base)
        # Large, quiet rings are a depth cue, not decorative data or chart axes.
        ring = _mix(t["background"], t["foreground"], .035)
        cx, cy = width * (.91 if self.portrait else .81), height * .39
        for radius in (width * .31, width * .43, width * .58):
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                         outline=ring, width=max(1, round(self.scale)))
        # Identity mark stays small; the research remains the hero.
        brand_y = height * (.066 if self.portrait else .068)
        mark = 28 * self.scale
        for column, offset in enumerate((.30, .05, .55)):
            bx = x + column * mark * .30
            draw.rounded_rectangle((bx, brand_y + mark * offset, bx + mark * .15, brand_y + mark),
                                   radius=max(1, round(self.scale)), fill=t["accent"])
        draw_text(draw, "ALIGNMENT DAILY", (x + 42 * self.scale, brand_y + self.scale, width * .60, 34 * self.scale),
                  round(22 * self.scale), t["foreground"], bold=True, max_lines=1,
                  min_size=max(8, round(12 * self.scale)))
        chapter = f"{index + 1:02d} / {len(script['scenes']):02d}"
        draw_text(draw, chapter, (right - 104 * self.scale, brand_y + self.scale, 104 * self.scale, 32 * self.scale),
                  round(20 * self.scale), t["muted"], align="right", max_lines=1,
                  min_size=max(8, round(12 * self.scale)))
        draw.line((x, height * .118, right, height * .118), fill=t["line"], width=max(1, round(self.scale)))
        # This anchor stays unchanged while technical → plain → simple unfolds.
        # It lives above the changing level badge, rather than competing with
        # the main heading or diagram labels.
        concept_title = str(scene.get("concept_title") or "").strip()
        self.concept_label = None
        if concept_title:
            concept_ids = list(dict.fromkeys(item.get("concept_id") for item in script["scenes"]
                                            if item.get("concept_id")))
            concept_id = scene.get("concept_id")
            number = concept_ids.index(concept_id) + 1 if concept_id in concept_ids else 1
            self.concept_label = f"IDEA {number:02d}  ·  {concept_title}"
            self.concept_box = (x, height * (.128 if self.portrait else .142), right - x,
                                height * (.024 if self.portrait else .032))
            draw_text(draw, self.concept_label, self.concept_box, round(19 * self.scale),
                      t["muted"], max_lines=1, min_size=max(8, round(12 * self.scale)))
        # Human-readable provenance is always visible, including introductory frames.
        research = "RESEARCH DISCUSSION" if script.get("research_type") == "commentary" else "RESEARCH EXPLAINED"
        disclosure = f"{script.get('_narration_label', 'AI NARRATION')} · ILLUSTRATIVE DIAGRAMS"
        footer_y = height * (.916 if self.portrait else .944)
        self.provenance = research + " · " + disclosure
        draw_text(draw, self.provenance, (x, footer_y, right - x, height * .035),
                  round((17 if self.portrait else 17) * self.scale), t["muted"], max_lines=2 if self.portrait else 1,
                  min_size=round(12 * self.scale))
        self.title_layer = Image.new("RGBA", (width, height))
        draw = ImageDraw.Draw(self.title_layer)
        eyebrow = EYEBROWS.get(scene.get("visual"), "THE RESEARCH")
        if index == 0:
            eyebrow = "ONE RESEARCH IDEA"
        ey = height * (.149 if self.portrait else .19)
        level = scene.get("explanation_level")
        levels = {"technical": (0, "TECHNICAL"), "plain": (1, "IN PLAIN LANGUAGE"),
                  "simple": (2, "THE SIMPLEST VIEW")}
        if level in levels:
            position, label = levels[level]
            badge = f"{position + 1} / 3  ·  {label}"
            draw_text(draw, badge, (x, ey, (right - x) * .80, height * .033),
                      round(19 * self.scale), t["accent"], bold=True, max_lines=1,
                      min_size=max(8, round(11 * self.scale)))
            segment_w = 21 * self.scale
            start = right - segment_w * 3 - 12 * self.scale
            for step in range(3):
                bx = start + step * (segment_w + 6 * self.scale)
                by = ey + 8 * self.scale
                draw.rounded_rectangle((bx, by, bx + segment_w, by + 5 * self.scale),
                                       radius=max(1, round(2 * self.scale)),
                                       fill=t["accent"] if step <= position else t["line"])
            self.level_label = label
        else:
            draw_text(draw, eyebrow, (x, ey, right - x, height * .033), round(20 * self.scale),
                      t["accent"], bold=True, max_lines=1,
                      min_size=max(8, round(12 * self.scale)))
            self.level_label = None
        self.heading_fit = draw_text(draw, str(scene.get("heading", "")), self.heading_box,
                                    round((70 if self.portrait else 83) * self.scale), t["foreground"],
                                    bold=True, max_lines=3 if self.portrait else 4,
                                    min_size=round(38 * self.scale), break_long_words=False)
        # A short accent rule connects the editorial title to its visual.
        hy = self.heading_box[1] + self.heading_fit["height"] + height * .022
        if hy < self.diagram_box[1] - height * .01 or not self.portrait:
            draw.rounded_rectangle((x, hy, x + width * .063, hy + 5 * self.scale),
                                   radius=max(1, round(2 * self.scale)), fill=t["accent"])
        self.caption_layers: OrderedDict[str, Any] = OrderedDict()
        self._intro_layers: OrderedDict[int, Any] = OrderedDict()

    def _caption(self, text: str):
        layer = self.caption_layers.get(text)
        if layer is not None:
            self.caption_layers.move_to_end(text)
            return layer
        _, _, width, height = self.caption_box
        x = y = 0
        layer = Image.new("RGBA", (math.ceil(width) + 1, math.ceil(height) + 1))
        draw = ImageDraw.Draw(layer)
        # Caption color stays stable across alternating scene themes.
        background = self.design["palette"]["midnight"]
        draw.rounded_rectangle((x, y, x + width, y + height), radius=round(20 * self.scale), fill=background)
        draw.rounded_rectangle((x, y + height * .22, x + 4 * self.scale, y + height * .78),
                               radius=max(1, round(2 * self.scale)), fill=self.design["palette"]["lime"])
        draw_text(draw, text, (x + width * .035, y + height * .12, width * .93, height * .76),
                  round((48 if self.portrait else 48) * self.scale), self.design["palette"]["ivory"],
                  bold=True, max_lines=self.design["layout"]["caption_max_lines_portrait" if self.portrait else "caption_max_lines_landscape"],
                  min_size=max(10, round(22 * self.scale)), vertical="center")
        self.caption_layers[text] = layer
        if len(self.caption_layers) > 16:
            self.caption_layers.popitem(last=False)
        return layer

    def frame(self, elapsed: float, caption: str):
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("Frame time must be finite and nonnegative")
        try:
            from .video_diagrams import draw_diagram
        except ImportError:
            from video_diagrams import draw_diagram
        frame = self.base.copy()
        t, w, h = self.theme, self.width, self.height
        # Intro transforms are quantized and cached. Only a few dozen full-size
        # layers are ever needed, and the steady state bypasses compositing.
        intro = _ease(elapsed / self.design["motion"]["intro_seconds"])
        if intro >= .999:
            frame.paste(self.title_layer, (0, 0), self.title_layer)
            self._intro_layers.clear()
        else:
            bucket = round(intro * 24)
            title = self._intro_layers.get(bucket)
            if title is None:
                title = self.title_layer.copy()
                title.putalpha(title.getchannel("A").point(lambda a: round(a * bucket / 24)))
                self._intro_layers[bucket] = title
                if len(self._intro_layers) > 8:
                    self._intro_layers.popitem(last=False)
            frame.paste(title, (0, round((1 - intro) * 18 * self.scale)), title)
        beat = min(1., elapsed / self.duration)
        draw_diagram(frame, self.scene, self.diagram_box, elapsed, self.duration, self.portrait,
                     self.script.get("credits", {}), t, beat=beat)
        outro_seconds = self.design["motion"]["exit_seconds"]
        if outro_seconds and elapsed > self.duration - outro_seconds:
            outro = _ease((self.duration - elapsed) / outro_seconds)
            frame = Image.blend(self.base, frame, outro)
        draw = ImageDraw.Draw(frame)
        # Tiny travelling points keep the visual field alive after initial reveals.
        # They stay outside the diagram's content and never imply measured data.
        sx = self.content_box[0]
        ex = self.content_box[1]
        y = h * (.721 if self.portrait else .738)
        draw.line((sx, y, ex, y), fill=t["line"], width=max(1, round(self.scale)))
        ambient = self.design["motion"]["ambient_speed"]
        for index in range(3):
            position = ((index / 3 + elapsed * ambient * .1) % 1)
            xx = sx + (ex - sx) * position
            radius = 2 * self.scale
            draw.ellipse((xx - radius, y - radius, xx + radius, y + radius), fill=t["accent"])
        if caption:
            layer = self._caption(str(caption))
            frame.paste(layer, (round(self.caption_box[0]), round(self.caption_box[1])), layer)
        progress = (self.index + min(1., elapsed / self.duration)) / len(self.script["scenes"])
        py = h * (.969 if self.portrait else .98)
        draw = ImageDraw.Draw(frame)
        draw.rectangle((sx, py, ex, py + 3 * self.scale), fill=t["line"])
        draw.rectangle((sx, py, sx + (ex - sx) * progress, py + 3 * self.scale), fill=t["accent"])
        return frame
