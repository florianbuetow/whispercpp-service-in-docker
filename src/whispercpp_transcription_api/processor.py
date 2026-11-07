"""FFmpeg preparation, silence removal and whisper.cpp execution."""

import os
import re
import subprocess
import tempfile
from pathlib import Path


def run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a media tool and fail with its diagnostic output."""
    return subprocess.run(args, check=True, capture_output=True, text=True)


def probe_duration(path: Path) -> float:
    """Read original media length before it enters the queue."""
    return float(
        run(
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)
        ).stdout.strip()
    )


def silence_intervals(log: str, duration: float) -> list[tuple[float, float]]:
    """Read FFmpeg silencedetect events, including trailing silence."""
    intervals: list[tuple[float, float]] = []
    start: float | None = None
    for line in log.splitlines():
        found_start = re.search(r"silence_start:\s*([\d.]+)", line)
        found_end = re.search(r"silence_end:\s*([\d.]+)", line)
        if found_start:
            start = float(found_start.group(1))
        if found_end and start is not None:
            intervals.append((start, float(found_end.group(1))))
            start = None
    if start is not None:
        intervals.append((start, duration))
    return intervals


def kept_segments(intervals: list[tuple[float, float]], duration: float) -> list[tuple[float, float, float]]:
    """Return original start/end and new start for each speech segment."""
    kept: list[tuple[float, float, float]] = []
    previous = 0.0
    cursor = 0.0
    for start, end in intervals + [(duration, duration)]:
        start = min(max(start, previous), duration)
        end = min(max(end, start), duration)
        if start - previous >= 0.01:
            kept.append((previous, start, cursor))
            cursor += start - previous
        previous = end
    return kept


def remap_seconds(seconds: float, segments: list[tuple[float, float, float]]) -> float:
    """Translate a timestamp in trimmed audio to its original position."""
    for start, end, cursor in segments:
        if seconds < cursor + end - start:
            return start + max(0.0, seconds - cursor)
    return segments[-1][1] if segments else seconds


def _srt_time(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, milliseconds = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(milliseconds) / 1000


def _format_srt(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, ms = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"


def remap_srt(content: str, segments: list[tuple[float, float, float]]) -> str:
    """Restore subtitle timestamps after silence removal."""
    pattern = re.compile(r"(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})")

    def replace(match: re.Match[str]) -> str:
        start = remap_seconds(_srt_time(match.group(1)), segments)
        end = remap_seconds(_srt_time(match.group(2)), segments)
        return f"{_format_srt(start)} --> {_format_srt(end)}"

    return pattern.sub(replace, content)


def transcribe(
    upload: Path, output_base: Path, language: str, model: str, model_dir: Path, whisper_bin: str, threads: int = 4
) -> tuple[float, int]:
    """Prepare one upload and produce UTF-8 text and SRT files."""
    model_path = model_dir / f"ggml-{model}.bin"
    if not model_path.is_file():
        downloader = Path(__file__).resolve().parents[2] / "scripts" / "download-model.sh"
        subprocess.run(
            ["/bin/sh", str(downloader), model],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "MODEL_DIR": str(model_dir)},
        )
        if not model_path.is_file():
            raise FileNotFoundError(f"Model download did not create {model_path}")
    with tempfile.TemporaryDirectory(prefix="transcribe-") as work:
        work_dir = Path(work)
        wav = work_dir / "converted.wav"
        detected = run(
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            str(upload),
            "-vn",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            "-af",
            "silencedetect=noise=-40dB:d=1",
            str(wav),
        )
        duration = float(
            run(
                "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(wav)
            ).stdout.strip()
        )
        segments = kept_segments(silence_intervals(detected.stderr, duration), duration)
        trimmed = work_dir / "trimmed.wav"
        if not segments:
            text, srt = "", ""
        else:
            if len(segments) == 1 and segments[0][0] == 0 and segments[0][1] >= duration - 0.01:
                trimmed = wav
            else:
                concat = work_dir / "concat.txt"
                lines: list[str] = []
                for index, (start, end, _) in enumerate(segments):
                    part = work_dir / f"part-{index:06}.wav"
                    run(
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-y",
                        "-ss",
                        str(start),
                        "-i",
                        str(wav),
                        "-t",
                        str(end - start),
                        "-c:a",
                        "pcm_s16le",
                        str(part),
                    )
                    lines.append(f"file '{part}'")
                concat.write_text("\n".join(lines) + "\n")
                run("ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(trimmed))
            result_base = work_dir / "result"
            run(
                whisper_bin,
                "-m",
                str(model_path),
                "-f",
                str(trimmed),
                "-l",
                language,
                "-t",
                str(threads),
                "-otxt",
                "-osrt",
                "-of",
                str(result_base),
                "-np",
            )
            text = result_base.with_suffix(".txt").read_text(encoding="utf-8")
            srt = remap_srt(result_base.with_suffix(".srt").read_text(encoding="utf-8"), segments)
        output_base.parent.mkdir(parents=True, exist_ok=True)
        text_path = output_base.with_suffix(".txt")
        srt_path = output_base.with_suffix(".srt")
        text_path.write_text(text, encoding="utf-8")
        srt_path.write_text(srt, encoding="utf-8")
        return duration, text_path.stat().st_size + srt_path.stat().st_size
