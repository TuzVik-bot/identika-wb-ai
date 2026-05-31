#!/usr/bin/env python3
"""Re-render Identika jobs (fix broken SVG embeds / fetch missing WB photos).

Usage:
  python scripts/rerender_jobs.py              # all succeeded/approved jobs
  python scripts/rerender_jobs.py JOB_ID ...   # specific jobs
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from identika.services.jobs import JobService  # noqa: E402
from identika.storage import Storage  # noqa: E402


async def _rerender(service: JobService, job_id: str) -> None:
    job = await service.rerender_job(job_id)
    print(f"OK {job_id} status={job.status} slides={len(job.result.slides) if job.result else 0}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-render Identika job slide SVGs")
    parser.add_argument("job_ids", nargs="*", help="Job IDs (default: all with results)")
    args = parser.parse_args()

    storage = Storage()
    service = JobService(storage)
    job_ids = args.job_ids
    if not job_ids:
        job_ids = [job.id for job in storage.list_jobs() if job.result]

    if not job_ids:
        print("No jobs to re-render.")
        return 0

    for job_id in job_ids:
        try:
            asyncio.run(_rerender(service, job_id))
        except Exception as exc:
            print(f"FAIL {job_id}: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
