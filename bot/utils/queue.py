"""
Global job queue tracker
────────────────────────
Tracks all active jobs across all users.
Each job has:
  job_id    — unique ID
  user_id   — who started it
  username  — display name
  type      — what kind of job (leech, burn, resolution)
  desc      — short description (URL or filename)
  status    — current status string
  started   — unix timestamp
  task      — asyncio.Task (can be cancelled)
"""

import time
import asyncio
from typing import Optional

# { job_id: dict }
JOBS: dict[str, dict] = {}


def register(job_id: str, user_id: int, username: str,
             job_type: str, desc: str) -> dict:
    """Register a new job and return the job dict."""
    job = {
        "job_id":   job_id,
        "user_id":  user_id,
        "username": username,
        "type":     job_type,
        "desc":     desc,
        "status":   "starting…",
        "started":  time.time(),
        "task":     None,
    }
    JOBS[job_id] = job
    return job


def update_status(job_id: str, status: str):
    if job_id in JOBS:
        JOBS[job_id]["status"] = status


def set_task(job_id: str, task: asyncio.Task):
    if job_id in JOBS:
        JOBS[job_id]["task"] = task


def finish(job_id: str):
    JOBS.pop(job_id, None)


def cancel(job_id: str) -> bool:
    """Cancel a job. Returns True if cancelled."""
    job = JOBS.get(job_id)
    if not job:
        return False
    task = job.get("task")
    if task and not task.done():
        task.cancel()
    JOBS.pop(job_id, None)
    return True


def get_all() -> list[dict]:
    return list(JOBS.values())


def elapsed_str(started: float) -> str:
    secs = int(time.time() - started)
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


TYPE_EMOJI = {
    "leech":      "📥",
    "burn":       "🔤",
    "resolution": "📐",
    "magnet":     "🧲",
    "ytdlp":      "🎬",
    "direct":     "🌐",
}
