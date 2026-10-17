"""Design invariants: portable identities, complete text, and safe reading zones."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import video_design as design


def fixture():
    return {
        "research_type": "commentary",
        "credits": {"title": "What would make a neuron explanation work?", "authors": ["Research Author"],
                    "source_url": "https://example.org/research"},
        "scenes": [{"id": f"s{i}", "heading": heading, "visual": kind,
                    "labels": ["Predict the response", "Test the behavior", "Keep the limits"],
                    "narration": "Understand the signal, then test the claim."}
                   for i, (kind, heading) in enumerate([
                       ("neuron", "What does this neuron do?"), ("analogy", "A useful analogy"),
                       ("comparison", "A pattern is a starting point"), ("test", "Prediction makes a test"),
                       ("takeaway", "Good explanations earn our trust"), ("source", "Read the original research")])],
    }


class ConfigTests(unittest.TestCase):
    def test_design_settings_are_canonical_and_describe_bundled_fonts(self):
        first = design.visual_settings()
        self.assertEqual(first, design.visual_settings())
        self.assertIn("video_design.py", first["modules"])
        for item in first["fonts"].values():
            if item["bundled"]:
                self.assertTrue(item["name"].startswith("assets/fonts/"))
                self.assertFalse(Path(item["name"]).is_absolute())
                self.assertEqual(len(item["sha256"]), 64)
                self.assertGreater(item["bytes"], 10000)

    def test_invalid_color_motion_and_layout_fail_before_render(self):
        valid = design.load_design()
        cases = [({"palette": {**valid["palette"], "lime": "red"}}),
                 ({"schema_version": True}),
                 ({"motion": {**valid["motion"], "intro_seconds": float("nan")}}),
                 ({"layout": {**valid["layout"], "portrait_content_right": .99}}),
                 ({"motion": {**valid["motion"], "unknown": 1}})]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "design.json"
            for changes in cases:
                target.write_text(json.dumps({**valid, **changes}))
                with self.assertRaises(ValueError):
                    design.load_design(target)

    def test_hourly_import_does_not_require_pillow(self):
        # Reading the style fingerprint remains possible in collection-only CI.
        with patch.object(design, "ImageFont", None):
            self.assertEqual(design.visual_settings()["design"]["schema_version"], 1)
            design.font.cache_clear()
            with self.assertRaises(RuntimeError):
                design.font(28)
        design.font.cache_clear()


@unittest.skipIf(design.Image is None, "Pillow is an optional video dependency")
class TypographyTests(unittest.TestCase):
    def test_wrap_preserves_long_words_and_never_inserts_ellipsis(self):
        text = "Every important detail antidisestablishmentarianism stays visible."
        lines = design.wrap_text(text, design.font(32), 240, max_lines=1)
        self.assertEqual("".join("".join(lines).split()), "".join(text.split()))
        self.assertNotIn("…", "".join(lines))
        self.assertTrue(all(design.font(32).getlength(line) <= 240 for line in lines))

    def test_caption_fitting_keeps_all_words_within_bounds(self):
        image = design.Image.new("RGB", (720, 220))
        text = "A small neural signal can still change the final answer."
        result = design.draw_text(design.ImageDraw.Draw(image), text, (20, 10, 660, 190), 55,
                                  "#FFFFFF", bold=True, max_lines=3, min_size=22)
        self.assertEqual(" ".join(result["lines"]), text)
        self.assertLessEqual(result["height"], 190)
        self.assertTrue(all(design.font(result["font_size"], True).getlength(line) <= 660
                            for line in result["lines"]))

    def test_impossible_text_fails_instead_of_overflowing_or_omitting(self):
        draw = design.ImageDraw.Draw(design.Image.new("RGB", (64, 64)))
        with self.assertRaises(ValueError):
            design.draw_text(draw, "W", (0, 0, 1, 30), 18, "#FFFFFF", min_size=8)
        with self.assertRaises(ValueError):
            design.draw_text(draw, "This sentence cannot possibly fit inside a tiny box.",
                             (0, 0, 8, 8), 18, "#FFFFFF", max_lines=1)


@unittest.skipIf(design.Image is None, "Pillow is an optional video dependency")
class SceneTests(unittest.TestCase):
    def test_all_visuals_render_deterministically_in_both_aspects(self):
        script = fixture()
        for width, height in ((640, 360), (360, 640), (1280, 720), (720, 1280)):
            for index, scene in enumerate(script["scenes"]):
                with self.subTest(canvas=(width, height), visual=scene["visual"]):
                    painter = design.ScenePainter(script, scene, index, width, height, 12)
                    first = painter.frame(3, "A useful explanation makes a testable prediction.")
                    self.assertEqual(first.mode, "RGB")
                    self.assertEqual(first.size, (width, height))
                    self.assertEqual(first.tobytes(), painter.frame(3, "A useful explanation makes a testable prediction.").tobytes())
                    self.assertNotEqual(first.tobytes(), painter.frame(8, "A useful explanation makes a testable prediction.").tobytes())

    def test_safe_regions_do_not_overlap_and_avoid_social_buttons(self):
        script = fixture()
        for width, height in ((1920, 1080), (1080, 1920)):
            painter = design.ScenePainter(script, script["scenes"][0], 0, width, height, 10)
            hx, hy, hw, hh = painter.heading_box
            dx, dy, dw, dh = painter.diagram_box
            cx, cy, cw, ch = painter.caption_box
            self.assertLessEqual(dy + dh, cy)
            if height > width:
                self.assertLessEqual(hy + hh, dy)
                self.assertLessEqual(dx + dw, width * .88)
                self.assertLessEqual(cx + cw, width * .88)
                self.assertLessEqual(cy + ch, height * .89)
            else:
                self.assertLessEqual(hx + hw, dx)
            self.assertIn("RESEARCH DISCUSSION", painter.provenance)
            self.assertIn("AI NARRATION", painter.provenance)

    def test_three_levels_have_explicit_respectful_badges_and_legacy_still_renders(self):
        script = fixture()
        for level, label in (("technical", "TECHNICAL"), ("plain", "IN PLAIN LANGUAGE"),
                             ("simple", "THE SIMPLEST VIEW"), (None, None)):
            scene = copy.deepcopy(script["scenes"][0])
            scene["explanation_level"] = level
            scene["concept_id"] = "prediction"
            scene["concept_title"] = "What makes an explanation useful?"
            painter = design.ScenePainter(script, scene, 0, 720, 1280, 10)
            self.assertEqual(painter.level_label, label)
            self.assertEqual(painter.concept_label, "IDEA 01  ·  What makes an explanation useful?")
            self.assertLess(painter.concept_box[1] + painter.concept_box[3], painter.heading_box[1])
            self.assertEqual(painter.frame(2, "A test makes the claim clearer.").mode, "RGB")

    def test_caption_cache_is_bounded_and_uses_small_layers(self):
        script = fixture()
        painter = design.ScenePainter(script, script["scenes"][0], 0, 720, 1280, 10)
        for index in range(20):
            painter.frame(2, f"Keep all the words in caption number {index}.")
        self.assertEqual(len(painter.caption_layers), 16)
        self.assertTrue(all(layer.height < painter.height * .12 for layer in painter.caption_layers.values()))
        self.assertEqual(len(painter._intro_layers), 0)

    def test_bad_canvas_and_nonfinite_time_fail(self):
        script = fixture()
        for width, height, duration in ((100, 100, 10), (640, 360, 0), (640, 360, float("inf"))):
            with self.assertRaises(ValueError):
                design.ScenePainter(script, script["scenes"][0], 0, width, height, duration)
        painter = design.ScenePainter(script, script["scenes"][0], 0, 640, 360, 10)
        for elapsed in (-1, float("nan")):
            with self.assertRaises(ValueError):
                painter.frame(elapsed, "")


if __name__ == "__main__":
    unittest.main()
