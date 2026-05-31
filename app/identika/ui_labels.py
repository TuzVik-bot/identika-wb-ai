from __future__ import annotations

JOB_STATUS_LABELS: dict[str, str] = {
    "queued": "В очереди",
    "running": "Генерация…",
    "succeeded": "Готово",
    "failed": "Ошибка",
    "approved": "Утверждён",
}


def job_status_label(status: str) -> str:
    return JOB_STATUS_LABELS.get(status, status)
