"""Silence and transcript timestamp behavior."""

import math
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

import pytest

from whispercpp_transcription_api import processor
from whispercpp_transcription_api.processor import kept_segments, remap_srt, silence_intervals, transcribe


def test_silence_map_and_srt_remap() -> None:
    log = "silence_start: 0\nsilence_end: 2.0 | silence_duration: 2\nsilence_start: 4.0\n"
    segments = kept_segments(silence_intervals(log, 6.0), 6.0)
    assert segments == [(2.0, 4.0, 0.0)]
    assert remap_srt("1\n00:00:00,500 --> 00:00:01,500\nHello\n", segments) == ("1\n00:00:02,500 --> 00:00:03,500\nHello\n")


def test_transcribe_removes_silence_and_uses_original_timestamps() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "audio.wav"
        with wave.open(str(source), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            samples = [0] * 32000 + [int(10000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(32000)]
            wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        model_dir = root / "models"
        model_dir.mkdir()
        (model_dir / "ggml-medium.en.bin").write_bytes(b"fake")
        fake_whisper = root / "whisper-cli"
        fake_whisper.write_text(
            "#!/bin/sh\nwhile [ \"$1\" != '-of' ]; do shift; done\nshift\n"
            "printf 'Speech\\n' > \"$1.txt\"\n"
            "printf '1\\n00:00:00,000 --> 00:00:00,500\\nSpeech\\n' > \"$1.srt\"\n"
        )
        fake_whisper.chmod(0o755)
        duration, size = transcribe(source, root / "result", "en", "medium.en", model_dir, str(fake_whisper))
        assert 3.9 <= duration <= 4.1
        assert size > 0
        assert (root / "result.txt").read_text() == "Speech\n"
        assert "00:00:02,000 --> 00:00:02,500" in (root / "result.srt").read_text()


def test_missing_model_is_downloaded_to_configured_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "silence.wav"
        with wave.open(str(source), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 32000)
        model_dir = root / "models"
        model_dir.mkdir()
        real_run = subprocess.run
        downloads: list[str] = []

        def fake_run(args: list[str] | tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[0] == "/bin/sh":
                downloads.append(args[2])
                assert kwargs["env"]["MODEL_DIR"] == str(model_dir)
                (model_dir / "ggml-tiny.en.bin").write_bytes(b"model")
                return subprocess.CompletedProcess(args, 0, "", "")
            return real_run(args, **kwargs)

        monkeypatch.setattr(processor.subprocess, "run", fake_run)
        duration, size = transcribe(source, root / "result", "en", "tiny.en", model_dir, "unused")
        assert downloads == ["tiny.en"]
        assert 1.9 <= duration <= 2.1
        assert size == 0
