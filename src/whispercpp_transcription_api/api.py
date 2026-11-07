"""HTTP endpoints and the single sequential job worker."""

import hashlib
import logging
import os
import re
import subprocess
import threading
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from whispercpp_transcription_api.processor import probe_duration, transcribe
from whispercpp_transcription_api.store import JobStore

logger = logging.getLogger(__name__)
_MODEL_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
_LANGUAGE_PATTERN = re.compile(r"^[a-z]{2,3}$")


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    """Return stable public metadata without the internal dedup key."""
    return {
        "id": job["id"],
        "status": job["status"],
        "filename": job["filename"],
        "language": job["language"],
        "model": job["model"],
        "content_hash": job["content_hash"],
        "input_bytes": job["input_bytes"],
        "duration_seconds": job["duration_seconds"],
        "output_bytes": job["output_bytes"],
        "submitted_at": job["submitted_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "result_available": bool(job["status"] == "done" and not job["result_deleted"]),
        "error": job["error"],
    }


class ApiRuntime:
    """Own the store, worker and HTTP handlers for one service process."""

    def __init__(self, store: JobStore, model_dir: Path, whisper_bin: str) -> None:
        self.store = store
        self.model_dir = model_dir
        self.whisper_bin = whisper_bin
        self.threads = int(os.environ.get("WHISPER_THREADS", "4"))
        if self.threads < 1:
            raise ValueError("WHISPER_THREADS must be positive")
        self.wake = threading.Event()
        self.stop = threading.Event()

    def work(self) -> None:
        """Claim and process one job at a time until shutdown."""
        while not self.stop.is_set():
            job = self.store.claim_next()
            if job is None:
                self.wake.wait(timeout=1)
                self.wake.clear()
                continue
            job_id = job["id"]
            try:
                duration, size = transcribe(
                    self.store.queue_dir / f"{job_id}.upload",
                    self.store.output_dir / job_id,
                    job["language"],
                    job["model"],
                    self.model_dir,
                    self.whisper_bin,
                    self.threads,
                )
                self.store.finish(job_id, duration, size)
                (self.store.queue_dir / f"{job_id}.upload").unlink()
            except Exception as exc:
                logger.exception("Transcription failed for %s", job_id)
                self.store.fail(job_id, str(exc))

    @staticmethod
    def validate_settings(language: str, model: str) -> tuple[str, str]:
        """Normalize and validate public job settings."""
        language = language.strip().lower()
        model = model.strip().lower()
        model = {"en-medium": "medium.en"}.get(model, model)
        if not _LANGUAGE_PATTERN.fullmatch(language):
            raise HTTPException(400, "language must be a 2 or 3 letter ISO code")
        if not _MODEL_PATTERN.fullmatch(model) or model.startswith("."):
            raise HTTPException(400, "invalid model name")
        if (model.endswith(".en") or ".en-" in model) and language != "en":
            raise HTTPException(400, "English-only models require language=en")
        return language, model

    async def submit(
        self, file: Annotated[UploadFile, File()], language: Annotated[str, Form()] = "en", model: Annotated[str | None, Form()] = None
    ) -> dict[str, Any]:
        """Store upload and return a new or existing job."""
        language, model = self.validate_settings(language, model if model is not None else os.environ.get("DEFAULT_MODEL", "medium.en"))
        job_id = str(uuid.uuid4())
        path = self.store.queue_dir / f"{job_id}.upload"
        digest = hashlib.sha256()
        size = 0
        max_bytes = int(os.environ.get("MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
        try:
            with path.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(413, "upload exceeds MAX_UPLOAD_BYTES")
                    digest.update(chunk)
                    output.write(chunk)
            if size == 0:
                raise HTTPException(400, "empty upload")
            try:
                duration = probe_duration(path)
            except (subprocess.CalledProcessError, ValueError) as exc:
                raise HTTPException(400, "upload is not readable media") from exc
            content_hash = digest.hexdigest()
            key = hashlib.sha256(f"{content_hash}\0{language}\0{model}".encode()).hexdigest()
            job, duplicate = self.store.add(
                {
                    "id": job_id,
                    "content_hash": content_hash,
                    "dedup_key": key,
                    "filename": Path(file.filename or "upload").name,
                    "language": language,
                    "model": model,
                    "input_bytes": size,
                    "duration_seconds": duration,
                }
            )
            if duplicate:
                path.unlink()
                if self.store.retry(job["id"]):
                    updated = self.store.get(job["id"])
                    if updated is None:
                        raise RuntimeError("requeued job disappeared")
                    job = updated
                    self.wake.set()
            else:
                self.wake.set()
            return {**public_job(job), "deduplicated": duplicate}
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    def list_jobs(self) -> dict[str, Any]:
        """List all job metadata in submission order."""
        jobs = [public_job(job) for job in self.store.list()]
        return {"count": len(jobs), "jobs": jobs}

    def job_status(self, job_id: str) -> dict[str, Any]:
        """Fetch one job's metadata."""
        job = self.store.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return public_job(job)

    def result(self, job_id: str, format: str = "txt") -> FileResponse:
        """Download text or SRT after completion."""
        if format not in ("txt", "srt"):
            raise HTTPException(400, "format must be txt or srt")
        job = self.store.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if job["result_deleted"]:
            raise HTTPException(410, "result was deleted")
        if job["status"] != "done":
            raise HTTPException(409, "job is not done")
        path = self.store.output_dir / f"{job_id}.{format}"
        if not path.is_file():
            raise HTTPException(500, "result file is missing")
        return FileResponse(path, media_type="text/plain" if format == "txt" else "application/x-subrip", filename=f"{job_id}.{format}")

    def delete_result(self, job_id: str) -> dict[str, str]:
        """Remove both results and retain the deduplication record."""
        job = self.store.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if not self.store.delete_result(job_id):
            raise HTTPException(409, "result is unavailable")
        return {"id": job_id, "status": "deleted"}


def create_app(data_dir: Path, model_dir: Path | None = None, whisper_bin: str | None = None, start_worker: bool = True) -> FastAPI:
    """Create one API instance backed by a persistent data directory."""
    store = JobStore(data_dir)
    runtime = ApiRuntime(
        store,
        model_dir or Path(os.environ.get("MODEL_DIR", "/models")),
        whisper_bin or os.environ.get("WHISPER_BIN", "/usr/local/bin/whisper-cli"),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        worker = threading.Thread(target=runtime.work, name="transcription-worker", daemon=True)
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            runtime.stop.set()
            runtime.wake.set()
            if start_worker:
                worker.join(timeout=5)

    app = FastAPI(title="Whisper.cpp Transcription API", lifespan=lifespan)
    app.state.store = store
    app.add_api_route("/health", lambda: {"status": "ok"}, methods=["GET"])
    app.add_api_route("/jobs", runtime.submit, methods=["POST"], status_code=201)
    app.add_api_route("/jobs", runtime.list_jobs, methods=["GET"])
    app.add_api_route("/jobs/{job_id}", runtime.job_status, methods=["GET"])
    app.add_api_route("/jobs/{job_id}/result", runtime.result, methods=["GET"])
    app.add_api_route("/jobs/{job_id}/result", runtime.delete_result, methods=["DELETE"])
    return app
