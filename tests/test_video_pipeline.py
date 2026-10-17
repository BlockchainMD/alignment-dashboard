"""Offline queue, evidence, artifact, and render-generation integration tests."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import video_pipeline as pipeline
from tests.test_video_draft import draft_fixture, source_fixture


class VideoPipelineTests(unittest.TestCase):
    def setUp(self):
        (pipeline.ROOT / "artifacts").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=pipeline.ROOT / "artifacts")
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name) / "videos"
        self.article = {"id": "research-one", "title": "Checking a model's work", "url": "https://example.org/research",
                        "authors": ["An Author"], "relevance": "relevant", "published_at": "2026-10-17T01:00:00Z"}
        self.source = source_fixture()
        self.script = draft_fixture()
        self.render_calls = []

    def capture(self, article, directory):
        return deepcopy(self.source)

    def renderer(self, script, directory, *, formats, voice, rate, preview, speech_provider, speech_speed):
        self.render_calls.append({"directory": directory, "formats": formats, "voice": voice, "rate": rate, "preview": preview,
                                  "speech_provider": speech_provider, "speech_speed": speech_speed})
        dimensions = {"landscape": (1280, 720) if preview else (1920, 1080),
                      "portrait": (720, 1280) if preview else (1080, 1920)}
        outputs = {}
        for name in formats:
            path = directory / f"{name}.mp4"
            body = (name + json.dumps(script, sort_keys=True)).encode()
            path.write_bytes(body)
            poster = directory / f"{name}-poster.png"
            poster.write_bytes(b"poster")
            outputs[name] = {"path": str(path), "poster": str(poster), "width": dimensions[name][0], "height": dimensions[name][1],
                             "duration_seconds": 60.5, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
        (directory / "audio").mkdir()
        (directory / "audio/scene-01.wav").write_bytes(b"scene-audio")
        for name in ("captions.srt", "captions.vtt", "narration.wav"):
            (directory / name).write_text(name)
        (directory / "index.html").write_text('<a href="script.json">Review this generation</a>')
        return {"schema_version": 1, "article_id": script["article_id"], "source_sha256": script["source_sha256"],
                "title": script["title"], "formats": outputs, "duration_seconds": 60.5,
                "voice": {"synthetic": True, **pipeline.voice_settings(speech_provider, voice, rate, speech_speed)},
                "visual_design": pipeline.visual_settings(), "fps": 24,
                "timeline": [{"audio_path": str(directory / "audio/scene-01.wav")}],
                "narration_path": str(directory / "narration.wav"), "srt_path": str(directory / "captions.srt"),
                "vtt_path": str(directory / "captions.vtt"), "manifest_path": str(directory / "manifest.json"),
                "preview_path": str(directory / "index.html")}

    def produce(self, **kwargs):
        return pipeline.produce(self.article, self.work, capture=self.capture, draft_generator=lambda source: deepcopy(self.script),
                                renderer=self.renderer, **kwargs)

    def job_directory(self):
        return pipeline._work_path(self.work, pipeline.make_packet(self.article)["job_id"])

    def test_candidate_and_uncertain_date_are_held_before_capture(self):
        for changes in ({"relevance": "candidate"}, {"temporal_status": "publication_date_unverified"}):
            article = {**self.article, **changes}
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "review first"):
                pipeline.prepare_job(article, self.work, capture=lambda *_: self.fail("Candidate was fetched"))
        candidate = {**self.article, "relevance": "candidate"}
        _, source = pipeline.prepare_job(candidate, self.work, allow_candidate=True, capture=self.capture)
        self.assertEqual(source["article_id"], candidate["id"])

    def test_cached_sections_cannot_change_without_changing_bound_text(self):
        directory, _ = pipeline.prepare_job(self.article, self.work, capture=self.capture)
        corrupted = deepcopy(self.source)
        corrupted["sections"][0]["text"] = "Fabricated evidence replacing the genuine captured text."
        pipeline.atomic_write(directory / "source.json", corrupted)
        with self.assertRaisesRegex(ValueError, "sections|checksum|SHA"):
            pipeline.prepare_job(self.article, self.work, capture=lambda *_: self.fail("Cached source should be validated"))

    def test_success_commits_complete_immutable_generation_and_relocates_paths(self):
        state = self.produce()
        directory = self.job_directory()
        generation = Path(state["current_render_dir"])
        self.assertTrue(generation.is_relative_to(directory / "renders"))
        self.assertTrue(pipeline.render_is_current(self.article, self.work))
        self.assertEqual(pipeline.read_json(generation / "source.json"), self.source)
        self.assertEqual(pipeline.read_json(generation / "script.json"), self.script)
        self.assertEqual(pipeline.read_json(generation / "manifest.json"), state["render"])
        self.assertNotIn(".staging-", json.dumps(state))
        self.assertIn(f"renders/{generation.name}/index.html", (directory / "index.html").read_text())
        for output in state["render"]["formats"].values():
            self.assertTrue(Path(output["path"]).is_file())
            self.assertTrue(Path(output["poster"]).is_file())
        self.assertFalse(state["publishing"]["approved"])

    def test_settings_source_script_and_media_integrity_control_reuse(self):
        state = self.produce()
        for settings in ({"formats": ("portrait",)}, {"voice": "af_bella"}, {"rate": 140}, {"preview": True},
                         {"speech_speed": 1.05}, {"speech_provider": "system"}):
            with self.subTest(settings=settings):
                self.assertFalse(pipeline.render_is_current(self.article, self.work, **settings))
        directory = self.job_directory()
        changed_script = deepcopy(self.script)
        changed_script["title"] = "A corrected title"
        pipeline.atomic_write(directory / "script.json", changed_script)
        self.assertFalse(pipeline.render_is_current(self.article, self.work))
        pipeline.atomic_write(directory / "script.json", self.script)
        path = Path(state["render"]["formats"]["landscape"]["path"])
        original = path.read_bytes()
        path.write_bytes(b"corrupted")
        self.assertFalse(pipeline.render_is_current(self.article, self.work))
        path.write_bytes(original)
        self.assertTrue(pipeline.render_is_current(self.article, self.work))
        path.unlink()
        self.assertFalse(pipeline.render_is_current(self.article, self.work))

    def test_design_and_font_changes_require_a_new_render(self):
        self.produce()
        original = pipeline.visual_settings()
        changed = deepcopy(original)
        changed["design"]["palette"]["midnight"] = "#102E35"
        with patch.object(pipeline, "visual_settings", return_value=changed):
            self.assertFalse(pipeline.render_is_current(self.article, self.work))
        changed = deepcopy(original)
        changed["fonts"]["bold"]["sha256"] = "0" * 64
        with patch.object(pipeline, "visual_settings", return_value=changed):
            self.assertFalse(pipeline.render_is_current(self.article, self.work))
        self.assertTrue(pipeline.render_is_current(self.article, self.work))

    def test_voice_profile_model_and_implementation_identity_invalidate_reuse(self):
        self.produce()
        current = pipeline.voice_settings()
        for section, changed in (("voice", "af_bella"), ("model", {"sha256": "a" * 64}),
                                 ("pronunciation", {"profile": "revised", "substitutions": {"KL": "Kay ell"}}),
                                 ("mastering", {"integrated_loudness_lufs": -15})):
            with self.subTest(section=section), patch.object(pipeline, "voice_settings", return_value={**current, section: changed}):
                self.assertFalse(pipeline.render_is_current(self.article, self.work))
        digest = pipeline._file_digest
        for name in ("video_voice.py", "video-voice.json"):
            def changed_digest(path):
                return "changed code or profile" if Path(path).name == name else digest(path)
            with self.subTest(file=name), patch.object(pipeline, "_file_digest", side_effect=changed_digest):
                self.assertFalse(pipeline.render_is_current(self.article, self.work))

    def test_invalid_voice_does_not_invalidate_a_complete_generation(self):
        state = self.produce()
        directory = self.job_directory()
        preview = (directory / "index.html").read_bytes()
        with patch.object(pipeline, "voice_settings", side_effect=ValueError("Unknown voice")), \
                self.assertRaisesRegex(ValueError, "Unknown voice"):
            self.produce(voice="unknown")
        self.assertEqual((directory / "index.html").read_bytes(), preview)
        self.assertEqual(pipeline.read_json(directory / "job.json"), state)
        self.assertTrue(pipeline.render_is_current(self.article, self.work))

    def test_neural_voice_and_speed_options_reach_the_renderer(self):
        state = self.produce(voice="af_bella", speech_speed=1.03)
        self.assertEqual(self.render_calls[0]["speech_provider"], "kokoro")
        self.assertEqual(self.render_calls[0]["speech_speed"], 1.03)
        self.assertEqual(state["render_settings"]["speech"]["voice"], "af_bella")
        self.assertEqual(state["render_settings"]["speech"]["speed"], 1.03)
        with patch.object(pipeline, "load_archive", return_value={"items": [self.article]}), \
                patch.object(pipeline, "produce", return_value={"job_id": "new"}) as produce:
            self.assertEqual(pipeline.main(["batch", "--work-dir", str(self.work), "--voice", "af_nicole",
                                           "--speech-provider", "kokoro", "--speech-speed", "1.02"]), 0)
        self.assertEqual(produce.call_args.kwargs["speech_provider"], "kokoro")
        self.assertEqual(produce.call_args.kwargs["speech_speed"], 1.02)

    def test_batch_skips_matching_render_and_retries_different_formats(self):
        self.produce(formats=("portrait",))
        archive = {"items": [self.article]}
        with patch.object(pipeline, "load_archive", return_value=archive), patch.object(pipeline, "produce", return_value={"job_id": "retry"}) as produce:
            self.assertEqual(pipeline.main(["batch", "--work-dir", str(self.work), "--format", "portrait"]), 0)
            produce.assert_not_called()
            self.assertEqual(pipeline.main(["batch", "--work-dir", str(self.work)]), 0)
            self.assertEqual(produce.call_count, 1)

    def test_failed_rerender_cannot_mix_new_script_with_previous_video_package(self):
        old_state = self.produce()
        generation = Path(old_state["current_render_dir"])
        old_files = {str(path.relative_to(generation)): path.read_bytes() for path in generation.rglob("*") if path.is_file()}
        changed_script = deepcopy(self.script)
        changed_script["title"] = "A revised explanation"
        script_path = self.work / "revised-script.json"
        pipeline.atomic_write(script_path, changed_script)

        def failing_renderer(script, directory, **kwargs):
            (directory / "captions.srt").write_text("Partial new captions")
            raise RuntimeError("Encoding interrupted")

        with self.assertRaisesRegex(RuntimeError, "Encoding interrupted"):
            pipeline.produce(self.article, self.work, script_path=script_path, renderer=failing_renderer)
        directory = self.job_directory()
        failed = pipeline.read_json(directory / "job.json")
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("current_render_dir", failed)
        self.assertNotIn("render", failed)
        self.assertFalse((directory / "index.html").exists())
        self.assertFalse((directory / "publish-package.json").exists())
        self.assertFalse(list((directory / "renders").glob(".staging-*")))
        self.assertEqual(old_files, {str(path.relative_to(generation)): path.read_bytes() for path in generation.rglob("*") if path.is_file()})
        self.assertIn("failed", (self.work / "index.html").read_text())
        self.assertFalse(pipeline.render_is_current(self.article, self.work))

    def test_empty_batch_and_studio_cannot_write_into_public_site(self):
        public_index = pipeline.ROOT / "site/index.html"
        original = public_index.read_bytes()
        with patch.object(pipeline, "load_archive", return_value={"items": []}):
            self.assertEqual(pipeline.main(["batch", "--work-dir", str(pipeline.ROOT / "site")]), 1)
        with self.assertRaisesRegex(ValueError, "artifacts"):
            pipeline.write_studio(pipeline.ROOT / "site")
        self.assertEqual(public_index.read_bytes(), original)

    def test_job_directory_symlink_cannot_escape_work_root(self):
        self.work.mkdir()
        escaped = self.work / pipeline.make_packet(self.article)["job_id"]
        escaped.symlink_to(pipeline.ROOT / "site", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "escape"):
            pipeline.prepare_job(self.article, self.work, capture=self.capture)
        with self.assertRaisesRegex(ValueError, "artifacts"):
            pipeline.write_studio(self.work / escaped.name)

    def test_batch_limits_attempts_including_failures_and_keeps_candidates_held(self):
        articles = [{**self.article, "id": f"research-{index}"} for index in range(4)]
        articles.insert(0, {**self.article, "id": "candidate", "relevance": "candidate"})
        with patch.object(pipeline, "load_archive", return_value={"items": articles}), \
                patch.object(pipeline, "produce", side_effect=[ValueError("Source unavailable"), {"job_id": "second"}]) as produce:
            self.assertEqual(pipeline.main(["batch", "--work-dir", str(self.work), "--limit", "2"]), 1)
        self.assertEqual(produce.call_count, 2)
        self.assertEqual([call.args[0]["id"] for call in produce.call_args_list], ["research-0", "research-1"])

    def test_explicit_codex_cli_defaults_are_forwarded_without_fallback(self):
        with patch("scripts.video_draft.generate_codex_draft", return_value=self.script) as generate:
            pipeline.produce(self.article, self.work, capture=self.capture, renderer=self.renderer, codex_cli_defaults=True)
        generate.assert_called_once_with(self.source, use_cli_defaults=True)

    def test_revision_queue_keeps_prior_fingerprints_as_superseded(self):
        archive = {"generated_at": "2026-10-17T01:00:00Z", "items": [self.article]}
        first = pipeline.sync_queue(archive)
        changed = {**self.article, "summary": "The source metadata was corrected."}
        second = pipeline.sync_queue({**archive, "items": [changed]}, first)
        states = {job["job_id"]: job["status"] for job in second["jobs"]}
        self.assertEqual(states[first["jobs"][0]["job_id"]], "superseded")
        self.assertEqual(second["stats"], {"queued": 1, "needs_relevance_review": 0, "retired": 1})
        candidate = pipeline.sync_queue({**archive, "items": [{**self.article, "relevance": "candidate"}]})
        self.assertEqual(candidate["jobs"][0]["status"], "needs_relevance_review")


if __name__ == "__main__":
    unittest.main()
