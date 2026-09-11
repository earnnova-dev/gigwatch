"""Batched digest delivery: one alert per period instead of one per scan.

``watch`` pings you the moment a new match appears, which is great for a
personal job hunt but noisy for a busy recruiter or a job board that scans
every 15 minutes. ``digest`` fixes that: it still *catches* every new match
(the moment it appears, it is marked seen and parked in a buffer), but it only
*delivers* one consolidated alert per period (default: daily).

This is the mode that powers the hosted offering: run ``gigwatch watch`` on a
short interval to keep the buffer fresh, and ``gigwatch digest`` on a long
interval (e.g. once a day) to flush it. The recipient gets a single email with
everything new since the last digest — not 96 separate pings.

The buffer lives in its own JSON file (default ``gigwatch-digest.json``) so it
never collides with the seen-state file used by ``scan``/``watch``.
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, List

from gigwatch.alerts import send_all
from gigwatch.config import AlertConfig
from gigwatch.filtering import ScoredJob

DEFAULT_PERIOD = 86400  # one digest per day


def _now() -> int:
    return int(time.time())


def load_buffer(path: str) -> Dict:
    """Return the digest buffer: ``{"last_flush": ts, "jobs": {id: first_seen}}``.

    ``jobs`` maps job id -> first-seen epoch, in arrival order (JSON object
    order is preserved by :func:`json.load`).
    """
    if not os.path.exists(path):
        return {"last_flush": 0, "jobs": {}}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            jobs = data.get("jobs")
            if not isinstance(jobs, dict):
                jobs = {}
            return {
                "last_flush": int(data.get("last_flush", 0)),
                "jobs": {str(k): int(v) for k, v in jobs.items()},
            }
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        pass
    return {"last_flush": 0, "jobs": {}}


def save_buffer(path: str, buffer: Dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(buffer, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def add_pending(buffer: Dict, jobs: List[ScoredJob]) -> int:
    """Park newly-seen jobs into the buffer. Returns how many were added.

    A job already in the buffer is not re-timestamped (first-seen wins), and a
    job that is *not* new (already seen by ``scan``) is never added.
    """
    added = 0
    now = _now()
    for s in jobs:
        jid = s.job.id
        if jid and jid not in buffer["jobs"]:
            buffer["jobs"][jid] = now
            added += 1
    return added


def is_due(buffer: Dict, period: int, force: bool = False) -> bool:
    """True if the buffer should be flushed now.

    Due when there is at least one pending job AND either the period has
    elapsed since the last flush, or *force* is set. A buffer that has never
    been flushed (``last_flush == 0``) is always due — the first digest goes
    out immediately rather than waiting a full period.
    """
    if not buffer["jobs"]:
        return False
    if force:
        return True
    if buffer["last_flush"] == 0:
        return True
    return (_now() - buffer["last_flush"]) >= period


def build_digest(buffer: Dict, jobs_by_id: Dict[str, ScoredJob]) -> List[ScoredJob]:
    """Return the pending jobs (in arrival order) that have a known record.

    Jobs parked in the buffer but no longer present in the current scan are
    skipped (they may have been removed from the source); they are dropped on
    the next flush so the buffer stays bounded.
    """
    out: List[ScoredJob] = []
    for jid in buffer["jobs"]:
        if jid in jobs_by_id:
            out.append(jobs_by_id[jid])
    return out


def flush(buffer: Dict) -> None:
    """Clear the buffer and record the flush time."""
    buffer["jobs"] = {}
    buffer["last_flush"] = _now()


def send_digest(jobs: List[ScoredJob], cfg: AlertConfig,
                subject: str = "GigWatch digest: %d new matching gig(s)") -> List[str]:
    """Send the batched alert through the configured channels.

    Reuses the same channel senders as ``scan`` (console / email / Slack). The
    email subject is customised to make the batched nature obvious.
    """
    if not jobs:
        return []
    return send_all(jobs, cfg, subject=subject % len(jobs))
