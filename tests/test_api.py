"""HTTP queue behavior."""

import io
import tempfile
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whispercpp_transcription_api.api import create_app


def sample_wav() -> bytes:
    """Make a valid one-second PCM upload."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000)
    return buffer.getvalue()


def test_submit_deduplicate_status_and_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_MODEL", "medium.en")
    with tempfile.TemporaryDirectory() as directory:
        audio = sample_wav()
        app = create_app(Path(directory), start_worker=False)
        with TestClient(app) as client:
            first = client.post("/jobs", files={"file": ("hello.wav", audio)})
            assert first.status_code == 201
            job = first.json()
            assert job["status"] == "todo"
            assert job["language"] == "en"
            assert job["model"] == "medium.en"
            assert job["duration_seconds"] == 1.0
            duplicate = client.post("/jobs", files={"file": ("other.wav", audio)})
            assert duplicate.json()["id"] == job["id"]
            assert duplicate.json()["deduplicated"] is True
            assert len(list((Path(directory) / "queue").glob("*.upload"))) == 1
            different = client.post("/jobs", data={"model": "small.en"}, files={"file": ("hello.wav", audio)})
            assert different.json()["id"] != job["id"]
            multilingual = client.post("/jobs", data={"language": "nl", "model": "medium"}, files={"file": ("hello.wav", audio)})
            assert multilingual.json()["id"] != job["id"]
            assert client.get("/jobs").json()["count"] == 3
            assert client.get(f"/jobs/{job['id']}/result").status_code == 409
            assert client.delete(f"/jobs/{job['id']}/result").status_code == 409
            assert client.post("/jobs", data={"language": "nl"}, files={"file": ("x", audio)}).status_code == 400
            assert client.post("/jobs", files={"file": ("x", b"")}).status_code == 400
            assert client.post("/jobs", files={"file": ("x", b"nonsense")}).status_code == 400

            store = app.state.store
            claimed = store.claim_next()
            assert claimed is not None
            assert claimed["id"] == job["id"]
            assert store.get(job["id"])["status"] == "in_progress"
            (store.output_dir / f"{job['id']}.txt").write_text("hello")
            (store.output_dir / f"{job['id']}.srt").write_text("subtitle")
            store.finish(job["id"], 3.2, 13)
            assert client.get(f"/jobs/{job['id']}/result").text == "hello"
            assert client.get(f"/jobs/{job['id']}/result?format=srt").text == "subtitle"
            assert client.delete(f"/jobs/{job['id']}/result").status_code == 200
            status = client.get(f"/jobs/{job['id']}").json()
            assert status["result_available"] is False
            assert status["output_bytes"] == 0
            assert client.get(f"/jobs/{job['id']}/result").status_code == 410
            assert client.post("/jobs", files={"file": ("hello.wav", audio)}).json()["id"] == job["id"]


def test_restart_requeues_interrupted_job() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        first = create_app(root, start_worker=False)
        with TestClient(first) as client:
            job_id = client.post("/jobs", files={"file": ("a.wav", sample_wav())}).json()["id"]
            first.state.store.claim_next()
        second = create_app(root, start_worker=False)
        assert second.state.store.get(job_id)["status"] == "todo"
