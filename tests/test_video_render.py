"""Renderer contracts that do not require a full media encode in collection CI."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


from scripts import video_render as render


def speech_fixture(**changes):
    return {"provider": "kokoro", "engine": "kokoro-onnx", "voice": "af_heart", "rate": None,
            "speed": .96, "pause_seconds": .32, "mastering": {"integrated_loudness_lufs": -16}, **changes}


def sample_script():
    return {
        "schema_version": 1,
        "article_id": "sample",
        "source_sha256": "f" * 64,
        "title": "What does a test tell us?",
        "description": "A simple explanation with a deliberately limited conclusion.",
        "research_type": "commentary",
        "credits": {"title": "An original source", "authors": ["Research Author"], "source_url": "https://example.org/research"},
        "limitations": ["An illustration cannot establish how a real system behaves."],
        "scenes": [
            {"id": "s1", "heading": "A familiar test can miss a new problem", "narration": "A familiar test can miss a new problem. Try another situation, then check again.", "visual": "comparison", "labels": ["Familiar test", "New situation", "Evidence has limits"], "claim_ids": ["c1"]},
            {"id": "s2", "heading": "Read the source", "narration": "Read the original research for its evidence and limits.", "visual": "source", "labels": [], "claim_ids": []},
        ],
    }


class CaptionTests(unittest.TestCase):
    def test_chunks_preserve_every_word_and_end_at_sentence_boundaries(self):
        text = "A familiar test can miss a new problem. Try another situation, then check again."
        chunks = render.caption_chunks(text, max_words=8, max_chars=48)
        self.assertEqual(" ".join(chunks), text)
        self.assertTrue(all(len(chunk.split()) <= 8 for chunk in chunks))
        self.assertTrue(all(len(chunk) <= 48 for chunk in chunks))
        self.assertTrue(any(chunk.endswith("problem.") for chunk in chunks))

    def test_measured_caption_bounds_are_contiguous_and_exact(self):
        text = "One two three four five six seven eight nine ten eleven twelve. A second sentence follows."
        captions = render.time_captions(text, 7.25, 10.75)
        self.assertEqual(captions[0]["start"], 7.25)
        self.assertEqual(captions[-1]["end"], 18.0)
        self.assertEqual(" ".join(c["text"] for c in captions), text)
        for previous, current in zip(captions, captions[1:]):
            self.assertEqual(previous["end"], current["start"])
        self.assertTrue(all(c["end"] > c["start"] for c in captions))

    def test_empty_narration_has_no_caption_and_invalid_timing_rejected(self):
        self.assertEqual(render.time_captions("", 0, 1), [])
        for start, duration in [(-1, 2), (0, 0), (0, float("inf")), (float("nan"), 3)]:
            with self.assertRaises(ValueError):
                render.time_captions("A phrase", start, duration)

    def test_subtitle_rounding_carries_to_the_next_minute(self):
        self.assertEqual(render.subtitle_timestamp(59.9998), "00:01:00,000")
        self.assertEqual(render.subtitle_timestamp(3600.125, True), "01:00:00.125")

    def test_subtitle_files_have_portable_srt_and_vtt_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            srt, vtt = render.write_subtitles([{"start": 0.5, "end": 2.25, "text": "Simple words."}], Path(directory))
            self.assertIn("00:00:00,500 --> 00:00:02,250", srt.read_text())
            self.assertTrue(vtt.read_text().startswith("WEBVTT\n\n00:00:00.500"))


class RenderValidationTests(unittest.TestCase):
    def test_analogy_selector_reserves_traffic_signal_for_relevant_scripts(self):
        self.assertEqual(render.analogy_mode({"heading": "The bicycle-dog trap", "narration": "Dogs on bicycles might require sunny weather.", "labels": ["Dogs", "Bicycles", "Sunny days"]}), "generic")
        self.assertEqual(render.analogy_mode({"narration": "Guess traffic-light rules from a few photos."}), "traffic_light")
        self.assertEqual(render.analogy_mode({"labels": ["An intersection", "A new situation"]}), "traffic_light")
        self.assertEqual(render.analogy_mode({"narration": "Red, stopped cars: seems simple."}), "traffic_light")
        self.assertEqual(render.analogy_mode({"narration": "A red neuron responds to a stopped car."}), "generic")

    def test_comparison_semantics_select_explanatory_marks(self):
        self.assertEqual(render.comparison_mode({"heading": "Measure what changes", "labels": ["Original", "Prediction", "Output probabilities"]}), "probability")
        self.assertEqual(render.comparison_mode({"heading": "Two ways a label can fail", "labels": ["Which examples?", "How strong a response?"]}), "activation")
        self.assertEqual(render.comparison_mode({"heading": "Two environments", "labels": ["Familiar", "New"]}), "generic")

    def test_invalid_parameters_fail_without_media_or_speech_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            for parameters in [{"fps": 0}, {"rate": 250}, {"max_duration": 121},
                               {"formats": ("square",)}, {"formats": ("portrait", "portrait")}]:
                with self.subTest(parameters=parameters), patch.object(render, "create_narration") as speech:
                    with self.assertRaises(ValueError):
                        render.render_video(sample_script(), Path(directory), **parameters)
                    speech.assert_not_called()

    @unittest.skipIf(render.Image is None, "Pillow is an optional video dependency")
    def test_overlong_measured_audio_rejected_before_encoding(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(render.shutil, "which", return_value="tool"), \
                patch.object(render, "voice_settings", return_value=speech_fixture()), \
                patch.object(render, "create_narration", side_effect=[61, 61]), patch.object(render, "_encode_scene") as encode:
            with self.assertRaisesRegex(ValueError, "Shorten the script"):
                render.render_video(sample_script(), Path(directory))
            encode.assert_not_called()
            self.assertFalse(list(Path(directory).glob("*.mp4")))

    def test_speech_synthesis_uses_resolved_settings_without_a_system_fallback(self):
        settings = speech_fixture(voice="af_bella")
        with tempfile.TemporaryDirectory() as directory, patch.object(render, "synthesize") as synthesize, \
                patch.object(render, "voice_settings") as resolve, patch.object(render, "media_duration", return_value=3.25):
            output = Path(directory) / "narration.wav"
            self.assertEqual(render.create_narration("A clear idea.", output, speech_settings=settings), 3.25)
            synthesize.assert_called_once_with("A clear idea.", output, settings)
            resolve.assert_not_called()
        with tempfile.TemporaryDirectory() as directory, patch.object(render, "synthesize", side_effect=RuntimeError("Neural model unavailable")), \
                patch.object(render.shutil, "which") as system_engine:
            with self.assertRaisesRegex(RuntimeError, "Neural model unavailable"):
                render.create_narration("No silent fallback.", Path(directory) / "voice.wav", speech_settings=settings)
            system_engine.assert_not_called()

    def test_supplied_audio_does_not_resolve_or_synthesize_a_neural_voice(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(render, "synthesize") as synthesize, \
                patch.object(render, "voice_settings") as resolve, patch.object(render, "_run") as convert, \
                patch.object(render, "media_duration", return_value=2.5):
            source = Path(directory) / "supplied.wav"
            source.write_bytes(b"supplied recording")
            output = Path(directory) / "converted.wav"
            self.assertEqual(render.create_narration("Transcript.", output, external_audio=source), 2.5)
            synthesize.assert_not_called()
            resolve.assert_not_called()
            command = convert.call_args.args[0]
            self.assertIn(str(source), command)
            self.assertIn("48000", command)

    @unittest.skipIf(render.Image is None, "Pillow is an optional video dependency")
    def test_both_exports_share_one_resolved_voice_and_scene_narration(self):
        speech = speech_fixture()

        def narration(text, path, **kwargs):
            path.write_bytes(b"measured speech")
            return 2.0

        def encode(painter, audio, output, *args):
            output.write_bytes(b"encoded scene")

        def ffmpeg(command):
            Path(command[-1]).write_bytes(b"final media")

        with tempfile.TemporaryDirectory() as directory, patch.object(render.shutil, "which", return_value="tool"), \
                patch.object(render, "voice_settings", return_value=speech) as resolve, \
                patch.object(render, "create_narration", side_effect=narration) as narrate, \
                patch.object(render, "_encode_scene", side_effect=encode) as encode_scene, \
                patch.object(render, "_run", side_effect=ffmpeg), \
                patch.object(render, "media_duration", return_value=4.667):
            report = render.render_video(sample_script(), Path(directory), preview=True)
            resolve.assert_called_once_with(speech_provider="kokoro", voice=None, rate=155, speech_speed=None)
            self.assertEqual(narrate.call_count, 2)
            self.assertEqual(report["voice"], {"synthetic": True, **speech})
            self.assertEqual(report["visual_design"], render.visual_settings())
            self.assertTrue(all(call.kwargs["speech_settings"] == speech for call in narrate.call_args_list))
            self.assertEqual([call.args[1] for call in encode_scene.call_args_list[:2]],
                             [call.args[1] for call in encode_scene.call_args_list[2:]])
            self.assertAlmostEqual(report["timeline"][1]["start"], 56 / 24, places=5)
            self.assertEqual(report["captions"][-1]["end"], round(56 / 24 + 2, 6))
            self.assertIn("Kokoro neural speech engine", (Path(directory) / "index.html").read_text())

    @unittest.skipIf(render.Image is None, "Pillow is an optional video dependency")
    def test_three_level_timeline_leaves_room_between_levels_and_concepts(self):
        from tests.test_video_draft import draft_fixture

        def narration(text, path, **kwargs):
            path.write_bytes(b"measured speech")
            return 1.0

        def encode(painter, audio, output, *args):
            output.write_bytes(b"encoded scene")

        def ffmpeg(command):
            Path(command[-1]).write_bytes(b"final media")

        with tempfile.TemporaryDirectory() as directory, patch.object(render.shutil, "which", return_value="tool"), \
                patch.object(render, "voice_settings", return_value=speech_fixture()), \
                patch.object(render, "create_narration", side_effect=narration), \
                patch.object(render, "_encode_scene", side_effect=encode), \
                patch.object(render, "_run", side_effect=ffmpeg), \
                patch.object(render, "media_duration", return_value=12.0):
            report = render.render_video(draft_fixture(), Path(directory), formats=("landscape",), preview=True)
        timeline = report["timeline"]
        self.assertEqual([item["explanation_level"] for item in timeline[:3]], ["technical", "plain", "simple"])
        for item in timeline:
            if item["explanation_level"] in {"technical", "plain"}:
                self.assertGreaterEqual(item["duration"] - item["audio_duration"], .55)
            elif item["explanation_level"] == "simple":
                self.assertGreaterEqual(item["duration"] - item["audio_duration"], .85)
        for left, right in zip(timeline, timeline[1:]):
            self.assertAlmostEqual(left["end"], right["start"])
        self.assertEqual(report["teaching_format"], "three_levels")

    def test_html_escapes_script_and_restricts_source_link_scheme(self):
        script = sample_script()
        script["title"] = '<script>alert("bad")</script>'
        script["credits"]["source_url"] = "javascript:alert(1)"
        with tempfile.TemporaryDirectory() as directory:
            path = render._preview_html(script, {}, Path(directory), {})
            page = path.read_text()
            self.assertNotIn("<script>", page)
            self.assertNotIn("javascript:", page)
            self.assertIn("Draft for human review", page)
            self.assertIn(render.CAPTION_TIMING, page)

    def test_preview_discloses_the_actual_voice_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            page = render._preview_html(sample_script(), {}, destination,
                                        {"voice": speech_fixture(voice="<unsafe>")}).read_text()
            self.assertIn("Kokoro neural speech engine", page)
            self.assertIn("&lt;unsafe&gt;", page)
            self.assertNotIn("voice <unsafe>", page)
            page = render._preview_html(sample_script(), {}, destination,
                                        {"voice": {"provider": "external", "engine": "external audio"}}).read_text()
            self.assertIn("Narration supplied by the operator", page)
            self.assertNotIn("Kokoro", page)


@unittest.skipIf(render.Image is None, "Pillow is an optional video dependency")
class DiagramTests(unittest.TestCase):
    def test_measured_wrap_handles_long_tokens_without_overflow(self):
        face = render.font(24)
        lines = render.wrap_text("Interpretability " + "x" * 60, face, 160)
        self.assertTrue(all(face.getlength(line) <= 160 for line in lines))
        self.assertEqual("".join(lines).replace(" ", ""), ("Interpretability " + "x" * 60).replace(" ", ""))

    def test_both_layouts_draw_all_visuals_and_have_animated_frames(self):
        script = sample_script()
        for width, height in [(640, 360), (360, 640)]:
            for visual in render.VISUALS:
                with self.subTest(width=width, visual=visual):
                    scene = dict(script["scenes"][0], visual=visual)
                    painter = render.ScenePainter(script, scene, 0, width, height, 8)
                    first = painter.frame(0, "One clear idea at a time.")
                    later = painter.frame(2, "One clear idea at a time.")
                    self.assertEqual(first.size, (width, height))
                    self.assertEqual(first.mode, "RGB")
                    self.assertNotEqual(first.tobytes(), later.tobytes())
                    self.assertGreater(len(later.resize((64, 64)).getcolors(4096)), 20)


if __name__ == "__main__":
    unittest.main()
