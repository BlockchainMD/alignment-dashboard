"""Offline checks for stable voice identity, explicit setup, and audio quality gates."""

from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import wave


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("video_voice", ROOT / "scripts" / "video_voice.py")
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)


def write_wav(path, *, sample_rate=48000, channels=1, silent=False):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(struct.pack("<h", 0 if silent else 1000) * 100 * channels)


class VoiceProfileTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"ALIGNMENT_KOKORO_MODEL": "fp32"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def test_default_identity_is_neural_and_contains_full_profile(self):
        settings = voice.voice_settings()
        self.assertEqual(settings["engine"], "kokoro-onnx")
        self.assertEqual(settings["voice"], "af_heart")
        self.assertEqual(settings["speed"], .96)
        self.assertIsNone(settings["rate"])
        self.assertEqual(settings["model"]["sha256"], voice.ASSETS["model"]["sha256"])
        self.assertEqual(settings["mastering"]["sample_rate"], 48000)
        self.assertEqual(settings["pause_seconds"], .32)
        self.assertEqual(json.loads(json.dumps(settings)), settings)
        settings["pronunciation"]["substitutions"]["AI"] = "wrong"
        self.assertEqual(voice.voice_settings()["pronunciation"]["substitutions"]["AI"], "A I")

    def test_settings_does_not_import_runtime_or_read_assets(self):
        with patch.object(voice, "_kokoro_engine", side_effect=AssertionError("runtime")), \
                patch.object(voice, "_verify_asset", side_effect=AssertionError("asset")), \
                patch.object(voice.urllib.request, "urlopen", side_effect=AssertionError("network")):
            self.assertEqual(voice.voice_settings()["engine_version"], "0.6.1")

    def test_explicit_int8_changes_identity_and_setup_selection(self):
        original = voice.voice_settings()
        with patch.dict(os.environ, {"ALIGNMENT_KOKORO_MODEL": "int8"}):
            quantized = voice.voice_settings()
        self.assertNotEqual(original["model"], quantized["model"])
        self.assertEqual(quantized["model"]["filename"], "kokoro-v1.0.int8.onnx")
        self.assertEqual(quantized["voices"], original["voices"])
        with patch.dict(os.environ, {"ALIGNMENT_KOKORO_MODEL": "automatic"}):
            with self.assertRaisesRegex(ValueError, "fp32 or int8"):
                voice.voice_settings()

    def test_kokoro_uses_natural_speed_and_english_voice(self):
        self.assertEqual(voice.voice_settings(voice="bm_george", speech_speed=1.04)["language"], "en-gb")
        for kwargs in ({"rate": 140}, {"rate": True}, {"speech_speed": float("nan")},
                       {"speech_speed": True}, {"speech_speed": 2}, {"voice": "../../voice"},
                       {"speech_provider": "auto"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                voice.voice_settings(**kwargs)

    def test_explicit_system_provider_records_engine_and_honors_voice(self):
        with patch.object(voice, "_system_identity", return_value=("espeak-ng", "/usr/bin/espeak-ng", "version 1")):
            settings = voice.voice_settings("system", "en-gb", 140)
        self.assertEqual(settings["engine"], "espeak-ng")
        self.assertEqual(settings["voice"], "en-gb")
        self.assertEqual(settings["engine_version"], "version 1")
        self.assertEqual(settings["rate"], 140)
        self.assertIsNone(settings["model"])
        self.assertIsNone(settings["sentence_pause_seconds"])
        with self.assertRaisesRegex(ValueError, "uses rate"):
            voice.voice_settings("system", speech_speed=1.)

    def test_config_rejects_unknown_missing_and_invalid_fields(self):
        base = voice.load_config()
        cases = []
        value = deepcopy(base); value["unrecognized"] = True; cases.append(value)
        value = deepcopy(base); del value["mastering"]["channels"]; cases.append(value)
        value = deepcopy(base); value["kokoro"]["model_sha256"] = "0" * 64; cases.append(value)
        value = deepcopy(base); value["mastering"]["channels"] = True; cases.append(value)
        value = deepcopy(base); value["mastering"]["sample_rate"] = 24000; cases.append(value)
        value = deepcopy(base); value["pause_seconds"] = float("inf"); cases.append(value)
        value = deepcopy(base); value["default_provider"] = "system"; cases.append(value)
        value = deepcopy(base); value["pronunciation"]["substitutions"] = []; cases.append(value)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            for index, config in enumerate(cases):
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.subTest(case=index), self.assertRaises(ValueError):
                    voice.load_config(path)

    def test_pronunciation_is_case_sensitive_whole_word_and_single_pass(self):
        original = "AI uses KL divergence with RLHF. LLMs explain TRAIN and AIs."
        self.assertEqual(voice.prepare_text(original, voice.voice_settings()),
                         "A I uses kay ell divergence with R L H F. L L Ms explain TRAIN and AIs.")
        self.assertEqual(original, "AI uses KL divergence with RLHF. LLMs explain TRAIN and AIs.")
        for text in ("", " \t", "bad\x00text"):
            with self.assertRaises(ValueError):
                voice.prepare_text(text, voice.voice_settings())


class VoiceAssetTests(unittest.TestCase):
    def test_missing_assets_are_actionable_and_never_download_implicitly(self):
        voice._kokoro_engine.cache_clear()
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(voice.urllib.request, "urlopen", side_effect=AssertionError("network")):
            with self.assertRaisesRegex(voice.VoiceError, "setup explicitly"):
                voice._kokoro_engine(temporary, "fp32")

    def test_setup_downloads_verified_assets_atomically_and_reuses_them(self):
        contents = {"model": b"model bytes", "voices": b"voice bytes"}
        assets = {name: {"filename": name + ".bin", "bytes": len(data),
                         "sha256": hashlib.sha256(data).hexdigest()} for name, data in contents.items()}
        def download(request, **kwargs):
            self.assertTrue(request.full_url.startswith(voice.RELEASE_URL + "/"))
            return io.BytesIO(contents[Path(request.full_url).stem])
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(voice, "_assets_dir", return_value=Path(temporary)), \
                patch.object(voice, "_selected_assets", return_value=assets), \
                patch.object(voice.urllib.request, "urlopen", side_effect=download) as network:
            paths = voice.setup_assets("int8")
            self.assertEqual(network.call_count, 2)
            self.assertEqual(Path(paths["model"]).read_bytes(), contents["model"])
            self.assertFalse(list(Path(temporary).glob(".kokoro-download-*")))
            voice.setup_assets("int8")
            self.assertEqual(network.call_count, 2)

    def test_bad_download_does_not_replace_existing_file(self):
        asset = {"filename": "model.bin", "bytes": 4, "sha256": hashlib.sha256(b"good").hexdigest()}
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(voice, "_assets_dir", return_value=Path(temporary)), \
                patch.object(voice, "_selected_assets", return_value={"model": asset}), \
                patch.object(voice.urllib.request, "urlopen", return_value=io.BytesIO(b"evil")):
            destination = Path(temporary) / "model.bin"
            destination.write_bytes(b"old")
            with self.assertRaisesRegex(voice.VoiceError, "checksum mismatch"):
                voice.setup_assets()
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertEqual(list(Path(temporary).iterdir()), [destination])


class VoiceAudioTests(unittest.TestCase):
    def test_loudness_measurements_reject_silent_and_missing_results(self):
        for result in (b"no report", b'{"input_i":"-inf"}', b'{}'):
            with self.assertRaises(voice.VoiceError):
                voice._loudness_measurements(result)

    def test_mastering_is_two_pass_measured_and_does_not_change_tempo(self):
        measurements = {"input_i": "-22.4", "input_tp": "-6.2", "input_lra": "3.4",
                        "input_thresh": "-32.4", "target_offset": "0.1"}
        calls = []
        with tempfile.TemporaryDirectory() as temporary:
            source, destination = Path(temporary) / "source.wav", Path(temporary) / "output.wav"
            def run(arguments, **kwargs):
                calls.append(arguments)
                if len(calls) == 2:
                    write_wav(destination)
                return subprocess.CompletedProcess(arguments, 0, b"", json.dumps(measurements).encode())
            with patch.object(voice.shutil, "which", return_value="/bin/ffmpeg"), patch.object(voice, "_run", side_effect=run):
                voice.master_audio(source, destination, voice.voice_settings())
            self.assertEqual(len(calls), 2)
            self.assertIn("highpass=f=70,loudnorm=I=-16.0:TP=-1.5:LRA=7.0:print_format=json", calls[0])
            normalization = calls[1][calls[1].index("-af") + 1]
            self.assertIn("measured_I=-22.4", normalization)
            self.assertIn("measured_TP=-6.2", normalization)
            self.assertIn("linear=true", normalization)
            self.assertNotIn("atempo", normalization)
            self.assertNotIn("-t", calls[1])
            self.assertIn("pcm_s16le", calls[1])

    def test_pcm_validation_rejects_wrong_format_silence_and_empty_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "voice.wav"
            for kwargs in ({"sample_rate": 24000}, {"channels": 2}, {"silent": True}):
                write_wav(path, **kwargs)
                with self.subTest(kwargs=kwargs), self.assertRaises(voice.VoiceError):
                    voice._validate_pcm(path)
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(48000)
            with self.assertRaises(voice.VoiceError):
                voice._validate_pcm(path)

    def test_failed_neural_synthesis_never_uses_system_or_overwrites_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "narration.wav"
            output.write_bytes(b"previous valid generation")
            with patch.object(voice, "_write_neural_audio", side_effect=voice.VoiceError("model failure")), \
                    patch.object(voice, "_write_system_audio", side_effect=AssertionError("fallback")), \
                    patch.object(voice, "master_audio", side_effect=AssertionError("master")):
                with self.assertRaisesRegex(voice.VoiceError, "model failure"):
                    voice.synthesize("A neural network.", output, voice.voice_settings())
            self.assertEqual(output.read_bytes(), b"previous valid generation")
            self.assertEqual(list(Path(temporary).iterdir()), [output])

    def test_synthesize_uses_spoken_pronunciation_and_commits_mastered_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "narration.wav"
            def mastering(source, destination, settings):
                write_wav(destination)
            with patch.object(voice, "_write_neural_audio") as neural, \
                    patch.object(voice, "master_audio", side_effect=mastering):
                voice.synthesize("KL divergence changes.", output, voice.voice_settings())
            self.assertEqual(neural.call_args.args[0], "kay ell divergence changes.")
            voice._validate_pcm(output)

    def test_changed_or_extra_settings_cannot_bypass_validation(self):
        for key, value in (("speed", float("inf")), ("provider", "auto"), ("extra", True)):
            settings = voice.voice_settings()
            settings[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                voice.synthesize("Narration", Path("unused.wav"), settings)

    def test_subprocess_failure_is_a_clear_voice_error(self):
        error = subprocess.CalledProcessError(1, ["ffmpeg"], stderr=b"invalid waveform")
        with patch.object(voice.subprocess, "run", side_effect=error):
            with self.assertRaisesRegex(voice.VoiceError, "invalid waveform"):
                voice._run(["ffmpeg", "-version"])

    def test_neural_waveform_finite_nonempty_and_mono_checks_with_fakes(self):
        class Waveform:
            ndim = 1
            size = 3
        waveform = Waveform()
        array = types.SimpleNamespace(asarray=lambda value: value,
                                      isfinite=lambda value: types.SimpleNamespace(all=lambda: True),
                                      abs=lambda value: value, max=lambda value: .5)
        writer = types.SimpleNamespace(write=Mock())
        engine = Mock()
        engine.create.return_value = (waveform, 24000)
        with patch.dict(sys.modules, {"numpy": array, "soundfile": writer}), \
                patch.object(voice, "_kokoro_engine", return_value=engine):
            voice._write_neural_audio("Clear speech.", Path("fake.wav"), voice.voice_settings())
            self.assertEqual(writer.write.call_count, 1)
            for field, value in (("size", 0), ("ndim", 2)):
                original = getattr(waveform, field)
                setattr(waveform, field, value)
                with self.subTest(field=field), self.assertRaisesRegex(voice.VoiceError, "empty, non-finite, or non-mono"):
                    voice._write_neural_audio("Clear speech.", Path("fake.wav"), voice.voice_settings())
                setattr(waveform, field, original)
            array.isfinite = lambda value: types.SimpleNamespace(all=lambda: False)
            with self.assertRaisesRegex(voice.VoiceError, "non-finite"):
                voice._write_neural_audio("Clear speech.", Path("fake.wav"), voice.voice_settings())


if __name__ == "__main__":
    unittest.main()
