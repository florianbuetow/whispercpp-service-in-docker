"""Durable job queue and metadata."""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class JobStore:
    """Persist jobs in SQLite; the unique key is the deduplication index."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.queue_dir = data_dir / "queue"
        self.output_dir = data_dir / "output"
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = data_dir / "jobs.sqlite3"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, dedup_key TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL, language TEXT NOT NULL, model TEXT NOT NULL,
                input_bytes INTEGER NOT NULL, duration_seconds REAL,
                status TEXT NOT NULL, submitted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                started_at TEXT, finished_at TEXT, output_bytes INTEGER,
                result_deleted INTEGER NOT NULL DEFAULT 0, error TEXT
            )""")
            db.execute("UPDATE jobs SET status='todo', started_at=NULL WHERE status='in_progress'")
        # A crash can leave an upload that was moved before its database insert.
        with self.connect() as db:
            known = {row[0] for row in db.execute("SELECT id FROM jobs")}
        for path in self.queue_dir.glob("*.upload"):
            if path.stem not in known:
                path.unlink()
        for path in self.queue_dir.glob("*.upload"):
            job = self.get(path.stem)
            if job and job["status"] == "done":
                path.unlink()

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        """Open a short lived connection safe for the API and worker threads."""
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, values: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Insert a queued job or return the existing job for its dedup key."""
        with self.connect() as db:
            existing = db.execute("SELECT * FROM jobs WHERE dedup_key=?", (values["dedup_key"],)).fetchone()
            if existing:
                return dict(existing), True
            try:
                db.execute(
                    """INSERT INTO jobs
                    (id,content_hash,dedup_key,filename,language,model,input_bytes,duration_seconds,status)
                    VALUES (:id,:content_hash,:dedup_key,:filename,:language,:model,:input_bytes,:duration_seconds,'todo')""",
                    values,
                )
            except sqlite3.IntegrityError:
                existing = db.execute("SELECT * FROM jobs WHERE dedup_key=?", (values["dedup_key"],)).fetchone()
                if existing is None:
                    raise
                return dict(existing), True
        job = self.get(values["id"])
        if job is None:
            raise RuntimeError("inserted job disappeared")
        return job, False

    def get(self, job_id: str) -> dict[str, Any] | None:
        """Get one job."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        """List jobs in submission order."""
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY rowid").fetchall()
        return [dict(row) for row in rows]

    def claim_next(self) -> dict[str, Any] | None:
        """Atomically claim the oldest queued job."""
        with self.connect() as db:
            row = db.execute("SELECT id FROM jobs WHERE status='todo' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='in_progress', started_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (row["id"],))
        return self.get(row["id"])

    def finish(self, job_id: str, duration: float, output_bytes: int) -> None:
        """Mark transcription complete after both output files exist."""
        with self.connect() as db:
            db.execute(
                """UPDATE jobs SET status='done', duration_seconds=?, output_bytes=?,
                finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), error=NULL WHERE id=?""",
                (duration, output_bytes, job_id),
            )

    def fail(self, job_id: str, error: str) -> None:
        """Record a processing error without losing the uploaded file."""
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (error, job_id)
            )

    def retry(self, job_id: str) -> bool:
        """Requeue a failed job while its uploaded file is available."""
        if not (self.queue_dir / f"{job_id}.upload").is_file():
            return False
        with self.connect() as db:
            changed = db.execute(
                "UPDATE jobs SET status='todo', started_at=NULL, finished_at=NULL, error=NULL WHERE id=? AND status='failed'", (job_id,)
            ).rowcount
        return changed == 1

    def delete_result(self, job_id: str) -> bool:
        """Delete output while preserving the hash and job metadata."""
        job = self.get(job_id)
        if job is None or job["status"] != "done" or job["result_deleted"]:
            return False
        for extension in ("txt", "srt"):
            (self.output_dir / f"{job_id}.{extension}").unlink(missing_ok=True)
        with self.connect() as db:
            db.execute("UPDATE jobs SET result_deleted=1, output_bytes=0 WHERE id=?", (job_id,))
        return True
