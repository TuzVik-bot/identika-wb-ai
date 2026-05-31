from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from identika.config import settings
from identika.models import GenerationResult, JobRecord


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Storage:
    def __init__(self, db_path: Path | None = None, assets_dir: Path | None = None) -> None:
        self.db_path = db_path or settings.identika_db_path
        self.assets_dir = assets_dir or settings.identika_assets_dir
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    product_title TEXT NOT NULL,
                    store_slug TEXT NOT NULL,
                    nm_id INTEGER,
                    sku_id INTEGER,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    approved_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    job_id TEXT,
                    path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def create_job(self, request_payload: dict[str, Any]) -> JobRecord:
        job_id = uuid.uuid4().hex
        product = request_payload["product"]
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, status, product_title, store_slug, nm_id, sku_id, request_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    "queued",
                    product.get("title") or "Товар WB",
                    product.get("store_slug") or "default",
                    product.get("nm_id"),
                    product.get("sku_id"),
                    json.dumps(request_payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.get_job(job_id)

    def set_running(self, job_id: str) -> None:
        self._set_status(job_id, "running")

    def save_result(self, job_id: str, result: GenerationResult) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, result_json=?, error=NULL, updated_at=? WHERE id=?",
                ("succeeded", result.model_dump_json(), now, job_id),
            )

    def update_result(self, job_id: str, result: GenerationResult) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET result_json=?, updated_at=? WHERE id=?",
                (result.model_dump_json(), now, job_id),
            )

    def save_error(self, job_id: str, error: str) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                ("failed", error[:1000], now, job_id),
            )

    def approve(self, job_id: str) -> JobRecord:
        job = self.get_job(job_id)
        if job.status not in ("succeeded", "approved"):
            raise ValueError("approve is allowed only after successful generation")
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, approved_at=?, updated_at=? WHERE id=?",
                ("approved", now, now, job_id),
            )
        return self.get_job(job_id)

    def _set_status(self, job_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, updated_at=? WHERE id=?",
                (status, utcnow_iso(), job_id),
            )

    def list_jobs(self) -> list[JobRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [self._row_to_job(row) for row in rows]

    def get_job(self, job_id: str) -> JobRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row_to_job(row)

    _ASSET_SUFFIXES = {
        ".svg",
        ".pdf",
        ".zip",
        ".json",
        ".html",
        ".txt",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
    }

    def add_asset(self, job_id: str, filename: str, data: bytes, media_type: str) -> str:
        asset_id = uuid.uuid4().hex
        safe_suffix = Path(filename).suffix.lower()
        if safe_suffix not in self._ASSET_SUFFIXES:
            safe_suffix = ".bin"
        job_dir = self.assets_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        path = (job_dir / f"{asset_id}{safe_suffix}").resolve()
        if self.assets_dir.resolve() not in path.parents:
            raise ValueError("asset path escaped assets directory")
        path.write_bytes(data)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO assets (id, job_id, path, media_type, created_at) VALUES (?, ?, ?, ?, ?)",
                (asset_id, job_id, str(path), media_type, utcnow_iso()),
            )
        return asset_id

    def add_staging_asset(self, session_id: str, filename: str, data: bytes, media_type: str) -> str:
        return self.add_asset(f"_uploads/{session_id}", filename, data, media_type)

    def get_settings(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def set_settings(self, values: dict[str, str]) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            for key, value in values.items():
                conn.execute(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (key, value, now),
                )

    def get_asset(self, asset_id: str) -> tuple[Path, str]:
        with self._connect() as conn:
            row = conn.execute("SELECT path, media_type FROM assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise KeyError(asset_id)
        path = Path(row["path"]).resolve()
        if self.assets_dir.resolve() not in path.parents:
            raise ValueError("asset path escaped assets directory")
        return path, row["media_type"]

    def _row_to_job(self, row: sqlite3.Row) -> JobRecord:
        result = None
        if row["result_json"]:
            result = GenerationResult.model_validate_json(row["result_json"])
        return JobRecord(
            id=row["id"],
            status=row["status"],
            product_title=row["product_title"],
            store_slug=row["store_slug"],
            nm_id=row["nm_id"],
            sku_id=row["sku_id"],
            error=row["error"],
            result=result,
            approved_at=datetime.fromisoformat(row["approved_at"]) if row["approved_at"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
