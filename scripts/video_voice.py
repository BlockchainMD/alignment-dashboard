#!/usr/bin/env python3
"""Local neural narration with explicit setup, pronunciation, and loudness mastering.

Importing this module and resolving settings use only the standard library.
Model assets are downloaded only by the explicit ``setup`` command. A neural
runtime or model failure never silently changes the voice to a system engine.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from functools import lru_cache
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
from typing import Any
import urllib.request
import wave


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "video-voice.json"
DEFAULT_CACHE_DIR = ROOT / ".cache" / "kokoro"
RELEASE = "model-files-v1.1"
ENGINE_VERSION = "0.6.1"
ASSETS = {
    "model": {
        "filename": "kokoro-v1.0.onnx",
        "sha256": "beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a",
        "bytes": 325505369,
    },
    "voices": {
        "filename": "voices-v1.0.bin",
        "sha256": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
        "bytes": 28214398,
    },
}
MODEL_VARIANTS = {
    "fp32": ASSETS["model"],
    "int8": {
        "filename": "kokoro-v1.0.int8.onnx",
        "sha256": "ae315a79b623f244700e4afb9246c46a26066782e049ba174bf3ba433970ee9c",
        "bytes": 114119327,
    },
}
RELEASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/" + RELEASE


class VoiceError(RuntimeError):
    """A requested voice could not be synthesized or mastered."""


def _keys(value: Any, expected: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} must contain exactly: {', '.join(sorted(expected))}")
    return value


def _number(value: Any, lower: float, upper: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    if not lower <= value <= upper:
        raise ValueError(f"{label} must be between {lower} and {upper}")
    return float(value)


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ValueError(f"{label} must be nonempty text without control characters")
    return value


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Validate the versioned profile before it enters a render fingerprint."""
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    _keys(config, {"schema_version", "default_provider", "kokoro", "system", "pronunciation",
                   "sentence_pause_seconds", "clause_pause_seconds", "pause_seconds", "mastering"}, "Voice config")
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise ValueError("Unsupported voice config schema_version")
    if config["default_provider"] != "kokoro":
        raise ValueError("The default provider must be kokoro; choose system explicitly per render")
    neural = _keys(config["kokoro"], {"engine_version", "voice", "speed", "model_release", "model_filename",
                                      "model_sha256", "voices_filename", "voices_sha256"}, "Kokoro config")
    if neural["engine_version"] != ENGINE_VERSION or neural["model_release"] != RELEASE:
        raise ValueError("Kokoro runtime and model release must match the supported pinned version")
    for name, asset in ASSETS.items():
        if neural[name + "_filename"] != asset["filename"] or neural[name + "_sha256"] != asset["sha256"]:
            raise ValueError(f"{name} must match the pinned official Kokoro asset")
    _kokoro_voice(neural["voice"])
    _number(neural["speed"], .5, 1.5, "Kokoro speed")
    system = _keys(config["system"], {"macos_voice", "espeak_voice"}, "System config")
    for name, value in system.items():
        _nonempty(value, name)
    pronunciation = _keys(config["pronunciation"], {"profile", "substitutions"}, "Pronunciation config")
    _nonempty(pronunciation["profile"], "Pronunciation profile")
    substitutions = pronunciation["substitutions"]
    if not isinstance(substitutions, dict) or not substitutions or len(substitutions) > 100:
        raise ValueError("Pronunciation substitutions must be a nonempty map of at most 100 entries")
    for original, spoken in substitutions.items():
        _nonempty(original, "Pronunciation key")
        _nonempty(spoken, "Pronunciation substitution")
    for name in ("sentence_pause_seconds", "clause_pause_seconds", "pause_seconds"):
        _number(config[name], 0, 2, name)
    mastering = _keys(config["mastering"], {"integrated_loudness_lufs", "true_peak_db", "loudness_range_lu",
                                           "highpass_hz", "sample_rate", "channels"}, "Mastering config")
    _number(mastering["integrated_loudness_lufs"], -24, -12, "Integrated loudness")
    _number(mastering["true_peak_db"], -6, -1, "True peak")
    _number(mastering["loudness_range_lu"], 1, 20, "Loudness range")
    _number(mastering["highpass_hz"], 20, 120, "High-pass frequency")
    if type(mastering["sample_rate"]) is not int or mastering["sample_rate"] != 48000:
        raise ValueError("Mastered speech must use a 48000 Hz sample rate")
    if type(mastering["channels"]) is not int or mastering["channels"] != 1:
        raise ValueError("Mastered speech must be mono")
    return config


def _kokoro_voice(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[ab][fm]_[a-z]+", value):
        raise ValueError("Choose an English Kokoro voice such as af_heart, af_bella, or bm_george")
    return value


def _system_identity() -> tuple[str, str, str]:
    if platform.system() == "Darwin" and (executable := shutil.which("say")):
        return "say", executable, "macOS " + (platform.mac_ver()[0] or platform.release())
    for engine in ("espeak-ng", "espeak"):
        if executable := shutil.which(engine):
            result = _run([executable, "--version"], timeout=10)
            version = result.stdout.decode("utf-8", errors="replace").splitlines()
            return engine, executable, version[0].strip() if version else engine
    raise VoiceError("The explicit system provider needs macOS say or espeak-ng installed")


def _selected_model(model: str | None = None) -> str:
    selected = model if model is not None else os.environ.get("ALIGNMENT_KOKORO_MODEL", "fp32")
    if selected not in MODEL_VARIANTS:
        raise ValueError("Kokoro model must be fp32 or int8 (ALIGNMENT_KOKORO_MODEL)")
    return selected


def _selected_assets(model: str | None = None) -> dict:
    return {"model": MODEL_VARIANTS[_selected_model(model)], "voices": ASSETS["voices"]}


def voice_settings(speech_provider: str = "kokoro", voice: str | None = None,
                   rate: int = 155, speech_speed: float | None = None) -> dict:
    """Return canonical, complete settings without importing the neural runtime.

    ``rate`` is the words-per-minute control of system engines. Kokoro instead
    uses a natural synthesis speed; changing one never silently changes the other.
    """
    config = load_config()
    if speech_provider not in {"kokoro", "system"}:
        raise ValueError("speech_provider must be kokoro or system")
    if isinstance(rate, bool) or not isinstance(rate, int) or not 80 <= rate <= 300:
        raise ValueError("System speech rate must be an integer between 80 and 300")
    settings = {
        "schema_version": 1, "provider": speech_provider, "engine": None, "engine_version": None,
        "executable": None, "platform": None, "voice": None, "language": None,
        "rate": None, "speed": None, "model": None, "voices": None,
        **{name: deepcopy(config[name]) for name in ("pronunciation", "sentence_pause_seconds",
              "clause_pause_seconds", "pause_seconds", "mastering")},
    }
    if speech_provider == "kokoro":
        if rate != 155:
            raise ValueError("Kokoro uses speech_speed, not rate; use --speech-speed or choose --speech-provider system")
        selected = _kokoro_voice(voice if voice is not None else config["kokoro"]["voice"])
        settings.update(engine="kokoro-onnx", engine_version=ENGINE_VERSION, voice=selected,
                        language="en-gb" if selected.startswith("b") else "en-us",
                        speed=_number(config["kokoro"]["speed"] if speech_speed is None else speech_speed,
                                      .5, 1.5, "Speech speed"))
        for name, asset in _selected_assets().items():
            settings[name] = {"filename": asset["filename"], "release": RELEASE, "sha256": asset["sha256"]}
    else:
        if speech_speed is not None:
            raise ValueError("The system provider uses rate, not speech_speed")
        engine, executable, version = _system_identity()
        selected = _nonempty(voice, "System voice") if voice is not None else config["system"][
            "macos_voice" if engine == "say" else "espeak_voice"]
        settings.update(engine=engine, engine_version=version, executable=executable,
                        platform=platform.platform(), voice=selected, language="engine voice selection", rate=rate,
                        sentence_pause_seconds=None, clause_pause_seconds=None)
    return settings


def prepare_text(text: str, settings: dict) -> str:
    """Normalize only spoken acronyms; display captions keep the original text."""
    if not isinstance(text, str) or not text.strip() or len(text) > 20000:
        raise ValueError("Narration must be nonempty text of at most 20000 characters")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in text):
        raise ValueError("Narration must not contain control characters")
    substitutions = settings["pronunciation"]["substitutions"]
    # One pass avoids matching inside replacement text, and boundaries avoid
    # changing words like 'AI' within 'TRAIN' or 'KL' within an author name.
    pattern = r"(?<!\w)(?:" + "|".join(re.escape(key) for key in sorted(substitutions, key=len, reverse=True)) + r")(?!\w)"
    return re.sub(pattern, lambda match: substitutions[match.group(0)], text).strip()


def _assets_dir() -> Path:
    return Path(os.environ.get("ALIGNMENT_KOKORO_DIR", str(DEFAULT_CACHE_DIR))).expanduser().resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_asset(path: Path, asset: dict) -> None:
    if not path.is_file():
        raise VoiceError(f"Missing Kokoro asset {path}. Run python scripts/video_voice.py setup explicitly first.")
    if path.stat().st_size != asset["bytes"] or _sha256(path) != asset["sha256"]:
        raise VoiceError(f"Kokoro asset checksum mismatch: {path}. Run the explicit setup command to replace it.")


def setup_assets(model: str | None = None) -> dict:
    """Explicit, atomic downloads of official, checksum-pinned assets only."""
    load_config()
    directory = _assets_dir()
    directory.mkdir(parents=True, exist_ok=True)
    result = {}
    for name, asset in _selected_assets(model).items():
        destination = directory / asset["filename"]
        try:
            _verify_asset(destination, asset)
        except VoiceError:
            descriptor, temporary = tempfile.mkstemp(prefix=".kokoro-download-", dir=directory)
            os.close(descriptor)
            temporary_path = Path(temporary)
            try:
                request = urllib.request.Request(RELEASE_URL + "/" + asset["filename"],
                                                 headers={"User-Agent": "alignment-dashboard-video-voice/1"})
                with urllib.request.urlopen(request, timeout=60) as response, temporary_path.open("wb") as target:
                    total = 0
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > asset["bytes"]:
                            raise VoiceError(f"Official download exceeded its pinned size: {asset['filename']}")
                        target.write(chunk)
                _verify_asset(temporary_path, asset)
                temporary_path.replace(destination)
            except Exception as error:
                raise VoiceError(f"Could not download verified Kokoro asset {asset['filename']}: {error}") from error
            finally:
                temporary_path.unlink(missing_ok=True)
        result[name] = str(destination)
    return result


@lru_cache(maxsize=2)
def _kokoro_engine(directory: str, model: str):
    selected_assets = _selected_assets(model)
    assets = {name: Path(directory) / asset["filename"] for name, asset in selected_assets.items()}
    for name, path in assets.items():
        _verify_asset(path, selected_assets[name])
    try:
        if importlib.metadata.version("kokoro-onnx") != ENGINE_VERSION:
            raise VoiceError(f"Install the pinned kokoro-onnx=={ENGINE_VERSION} from requirements-voice.txt")
        from kokoro_onnx import Kokoro
    except (ImportError, importlib.metadata.PackageNotFoundError) as error:
        raise VoiceError("Neural narration needs requirements-voice.txt in a Python 3.10-3.13 environment (3.12 recommended)") from error
    try:
        return Kokoro(str(assets["model"]), str(assets["voices"]))
    except Exception as error:
        raise VoiceError(f"Kokoro model initialization failed; no fallback was used: {error}") from error


def _run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(arguments, capture_output=True, check=True, **kwargs)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        details = getattr(error, "stderr", b"") or b""
        if isinstance(details, bytes):
            details = details.decode("utf-8", errors="replace")
        raise VoiceError(f"Speech/audio command failed ({Path(arguments[0]).name}): {str(details)[-1500:] or error}") from error


def _write_neural_audio(text: str, destination: Path, settings: dict) -> None:
    try:
        import numpy as np
        import soundfile as sf
    except ImportError as error:
        raise VoiceError("Install requirements-voice.txt in Python 3.10-3.13 for neural narration") from error
    engine = _kokoro_engine(str(_assets_dir()), _selected_model())
    try:
        audio, sample_rate = engine.create(text, voice=settings["voice"], speed=settings["speed"],
                                           lang=settings["language"], sentence_pause=settings["sentence_pause_seconds"],
                                           clause_pause=settings["clause_pause_seconds"])
        waveform = np.asarray(audio)
        if waveform.ndim != 1 or not waveform.size or not np.isfinite(waveform).all():
            raise ValueError("Kokoro returned empty, non-finite, or non-mono audio")
        if type(sample_rate) is not int or not 8000 <= sample_rate <= 192000:
            raise ValueError("Kokoro returned an invalid sample rate")
        if float(np.max(np.abs(waveform))) <= 1e-8:
            raise ValueError("Kokoro returned silent audio")
        sf.write(str(destination), waveform, sample_rate, subtype="FLOAT")
    except Exception as error:
        raise VoiceError(f"Kokoro synthesis failed; no fallback was used: {error}") from error


def _write_system_audio(text: str, destination: Path, settings: dict) -> None:
    if settings["engine"] == "say":
        text_path = destination.with_suffix(".txt")
        text_path.write_text(text, encoding="utf-8")
        _run([settings["executable"], "-v", settings["voice"], "-r", str(settings["rate"]),
              "-f", str(text_path), "-o", str(destination)], timeout=180)
    else:
        _run([settings["executable"], "-v", settings["voice"], "-s", str(settings["rate"]),
              "-w", str(destination), "--stdin"], input=text.encode("utf-8"), timeout=180)


def _loudness_measurements(stderr: bytes | str) -> dict:
    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr
    matches = re.findall(r"\{[^{}]*\}", text, flags=re.S)
    if not matches:
        raise VoiceError("FFmpeg did not report loudness measurements")
    try:
        measurement = json.loads(matches[-1])
        for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
            if not math.isfinite(float(measurement[key])):
                raise ValueError(f"non-finite {key}")
    except (KeyError, TypeError, ValueError) as error:
        raise VoiceError(f"Speech loudness measurements are invalid or silent: {error}") from error
    return measurement


def _validate_pcm(path: Path) -> None:
    """Check the actual exported WAV, without importing numpy in system mode."""
    try:
        with wave.open(str(path), "rb") as audio:
            if audio.getnchannels() != 1 or audio.getframerate() != 48000 or audio.getsampwidth() != 2:
                raise ValueError("Mastered audio must be 48000 Hz mono 16-bit PCM")
            if audio.getnframes() < 1:
                raise ValueError("Mastered audio is empty")
            nonzero = False
            while chunk := audio.readframes(48000):
                nonzero = nonzero or any(chunk)
            if not nonzero:
                raise ValueError("Mastered audio is silent")
    except (EOFError, wave.Error, OSError, ValueError) as error:
        raise VoiceError(f"Invalid mastered narration: {error}") from error


def master_audio(source: Path, destination: Path, settings: dict) -> None:
    """High-pass and two-pass loudness normalization; never change speaking rate."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VoiceError("FFmpeg is required for speech loudness mastering")
    mastering = settings["mastering"]
    targets = (f"I={mastering['integrated_loudness_lufs']}:TP={mastering['true_peak_db']}:"
               f"LRA={mastering['loudness_range_lu']}")
    highpass = f"highpass=f={mastering['highpass_hz']}"
    measured = _run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(source), "-af",
                     f"{highpass},loudnorm={targets}:print_format=json", "-f", "null", "-"], timeout=180)
    values = _loudness_measurements(measured.stderr)
    normalization = (f"loudnorm={targets}:measured_I={float(values['input_i'])}:"
                     f"measured_TP={float(values['input_tp'])}:measured_LRA={float(values['input_lra'])}:"
                     f"measured_thresh={float(values['input_thresh'])}:offset={float(values['target_offset'])}:"
                     "linear=true:print_format=json")
    _run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(source), "-af", f"{highpass},{normalization}",
          "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(destination)], timeout=180)
    _validate_pcm(destination)


def synthesize(text: str, output: Path, settings: dict) -> None:
    """Write mastered speech atomically, using exactly the requested provider."""
    if not isinstance(settings, dict):
        raise ValueError("Speech settings must be a canonical voice_settings() dictionary")
    try:
        expected = voice_settings(settings["provider"], settings["voice"],
                                  155 if settings["rate"] is None else settings["rate"], settings["speed"])
    except KeyError as error:
        raise ValueError("Incomplete speech settings; use voice_settings()") from error
    if settings != expected:
        raise ValueError("Speech settings do not match the current validated voice profile")
    spoken = prepare_text(text, settings)
    output = Path(output)
    if output.suffix.lower() != ".wav":
        raise ValueError("Narration output must have a .wav extension")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".alignment-voice-", dir=output.parent) as temporary:
        directory = Path(temporary)
        raw = directory / ("raw.aiff" if settings["engine"] == "say" else "raw.wav")
        if settings["provider"] == "kokoro":
            _write_neural_audio(spoken, raw, settings)
        else:
            _write_system_audio(spoken, raw, settings)
        mastered = directory / "mastered.wav"
        master_audio(raw, mastered, settings)
        mastered.replace(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Explicitly download and verify the pinned local neural voice assets")
    setup.add_argument("--model", choices=tuple(MODEL_VARIANTS), help="Explicit model selection (default: ALIGNMENT_KOKORO_MODEL or fp32)")
    sample = commands.add_parser("sample", help="Create a mastered voice audition (no implicit asset downloads)")
    sample.add_argument("--text", required=True)
    sample.add_argument("--output", type=Path, required=True)
    sample.add_argument("--voice")
    sample.add_argument("--speech-provider", choices=("kokoro", "system"), default="kokoro")
    sample.add_argument("--rate", type=int, default=155)
    sample.add_argument("--speech-speed", type=float)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "setup":
            result = setup_assets(arguments.model)
        else:
            settings = voice_settings(arguments.speech_provider, arguments.voice, arguments.rate, arguments.speech_speed)
            synthesize(arguments.text, arguments.output, settings)
            result = {"output": str(arguments.output.resolve()), "voice": settings}
    except (ValueError, VoiceError, OSError) as error:
        parser.exit(1, f"Voice error: {error}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
