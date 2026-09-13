#!/usr/bin/env python3
"""
Unfiltered job scan for the public API.
Fetches ALL jobs from all sources, no keyword/skill filtering.
Writes to live-feed.json which the webhook server serves via /api/jobs.
Run this hourly (cron) to keep the API fresh.
"""
import json
import sys
import os
import dataclasses
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gigwatch"))

from gigwatch.sources import (
    fetch_remotive, fetch_wwr, fetch_remoteok, fetch_jobicy, fetch_hn
)

SOURCES = [
    ("remotive", fetch_remotive),
    ("wwr", fetch_wwr),
    ("remoteok", fetch_remoteok),
    ("jobicy", fetch_jobicy),
    ("hn", fetch_hn),
]

def main():
    output_path = os.path.join(os.path.dirname(__file__), "gigwatch", "live-feed.json")
    all_jobs = []
    errors = []
    
    for name, func in SOURCES:
        try:
            jobs = func()
            for j in jobs:
                d = dataclasses.asdict(j) if dataclasses.is_dataclass(j) else dict(j)
                d["source"] = name  # override with our source label
            all_jobs.extend([dataclasses.asdict(j) if dataclasses.is_dataclass(j) else dict(j) for j in jobs])
            for d in all_jobs[-len(jobs):]:
                d["source"] = name
            print(f"[scan] {name}: {len(jobs)} jobs", file=sys.stderr)
        except Exception as e:
            errors.append({"source": name, "error": str(e)})
            print(f"[scan] {name}: ERROR {e}", file=sys.stderr)
    
    # Deduplicate by URL
    seen = set()
    unique_jobs = []
    for j in all_jobs:
        url = j.get("url", "")
        if url and url not in seen:
            seen.add(url)
            unique_jobs.append(j)
    
    feed = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "sources_scanned": len(SOURCES),
        "jobs_fetched": len(all_jobs),
        "matches": len(unique_jobs),
        "errors": errors,
        "jobs": unique_jobs
    }
    
    with open(output_path, "w") as f:
        json.dump(feed, f, indent=2)
    
    print(f"[scan] Done: {len(unique_jobs)} unique jobs -> {output_path}", file=sys.stderr)

if __name__ == "__main__":
    main()

