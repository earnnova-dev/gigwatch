"""Persistent state: which jobs we've already alerted on.

Stored as JSON so it's human-inspectable and easy to back up. The state file
is what makes GigWatch an *alert* tool rather than a *list* tool: it only
notifies about jobs it hasn't seen before.
"""

from __future__ import annotations

import calendar
import json
import os
import time
from typing import Dict, List, Set


def load_state(path: str) -> Dict[str, str]:
    """Return {job_id: first_seen_iso}."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_state(path: str, state: Dict[str, str]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def new_ids(state: Dict[str, str], jobs) -> List:
    """Return the subset of jobs whose id is not yet in state."""
    seen = set(state.keys())
    return [j for j in jobs if j.job.id not in seen]


def mark_seen(state: Dict[str, str], jobs) -> None:
    """Record each job as seen (first-seen timestamp preserved)."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for j in jobs:
        state.setdefault(j.job.id, now)


def prune(state: Dict[str, str], max_age_days: int = 90) -> int:
    """Drop entries older than max_age_days. Returns number removed."""
    if max_age_days <= 0:
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for k in list(state.keys()):
        try:
            # first_seen values are UTC ("Z"); parse as UTC, not local time.
            # (time.mktime misinterprets them as local, making entries age
            # faster and risk re-alerting a job that is still within the limit.)
            ts = calendar.timegm(time.strptime(state[k], "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, OverflowError):
            continue
        if ts < cutoff:
            del state[k]
            removed += 1
    return removed
