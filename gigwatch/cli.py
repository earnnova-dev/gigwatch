"""GigWatch command-line interface.

Commands:
  init      Write a starter config file.
  scan      One-shot: fetch, filter, dedupe, alert, update state.
  watch     Loop `scan` on an interval (for a cron-less daemon).
  serve     Self-contained hosted instance: live dashboard + JSON + RSS feed.
  digest    Batched alerts: catch every new match, deliver one alert/period.
  list      Dry run: fetch + filter + print matches, WITHOUT touching state.
  reset     Clear the seen-state so everything can alert again.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List

from gigwatch import __version__
from gigwatch.alerts import send_all
from gigwatch.config import Config, load_config
from gigwatch.digest import (
    DEFAULT_PERIOD,
    add_pending,
    build_digest,
    flush,
    is_due,
    load_buffer,
    save_buffer,
    send_digest,
)
from gigwatch.filtering import filter_jobs
from gigwatch.ranking import rank_jobs
from gigwatch.report import render
from gigwatch.sources import Job, fetch
from gigwatch.state import load_state, mark_seen, prune, save_state

DEFAULT_CONFIG = "config.json"


def _log(msg: str, verbose: bool = False, err: bool = False) -> None:
    if err:
        print("[gigwatch] " + msg, file=sys.stderr)
    elif verbose:
        print("[gigwatch] " + msg, file=sys.stderr)


def _fetch_all(cfg: Config, verbose: bool) -> (List[Job], List[str]):
    """Fetch every enabled source; collect per-source errors, keep going."""
    jobs: List[Job] = []
    errors: List[str] = []
    for src in cfg.sources:
        if not src.enabled:
            continue
        try:
            got = fetch(src)
            jobs.extend(got)
            _log("fetched %d job(s) from %s" % (len(got), src.type), verbose)
        except Exception as exc:  # noqa: BLE001 - keep going on one bad source
            errors.append("%s: %s" % (src.type, exc))
            _log("source %s failed: %s" % (src.type, exc), verbose, err=True)
    return jobs, errors


def cmd_init(args) -> int:
    path = args.config
    if os.path.exists(path) and not args.force:
        print("config already exists at %s (use --force to overwrite)" % path,
              file=sys.stderr)
        return 1
    starter = {
        "sources": [
            {"type": "remotive"},
            {"type": "wwr"},
            {"type": "remoteok"},
            {"type": "hn"},
        ],
        "profile": {
            "title": "Senior Software Engineer",
            "skills": ["python", "backend", "api"],
            "location": "remote",
            "notes": "senior, $150k+",
        },
        "filters": {
            "keywords": ["python", "backend", "api"],
            "require_all_keywords": False,
            "categories": [],
            "locations": [],
            "min_score": 1.0,
            "exclude_keywords": ["intern"],
        },
        "alerts": {
            "console": True,
            "email": {
                "to": "${GIGWATCH_EMAIL_TO}",
                "from": "gigwatch@localhost",
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "use_tls": True,
                "username": "${GIGWATCH_SMTP_USER}",
                "password": "${GIGWATCH_SMTP_PASS}",
            },
            "slack": {"webhook_url": "${GIGWATCH_SLACK_WEBHOOK}"},
            "max_per_alert": 25,
        },
        "state_file": "gigwatch-state.json",
        "poll_interval": 900,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(starter, fh, indent=2)
        fh.write("\n")
    print("wrote starter config to %s" % path)
    print("edit filters.keywords + profile to your skills, then run:")
    print("  gigwatch scan   (alert on new matches)")
    print("  gigwatch rank   (rank matches by fit to your profile)")
    return 0


def cmd_scan(args) -> int:
    cfg = load_config(args.config)
    fmt = getattr(args, "format", "text")
    jobs, fetch_errors = _fetch_all(cfg, args.verbose)

    scored = filter_jobs(jobs, cfg.filters)
    state = load_state(cfg.state_file)
    new_matches = [s for s in scored if s.job.id not in state]

    if fmt == "text":
        print("scanned %d job(s) from %d source(s); %d match filter; %d new"
              % (len(jobs), len([s for s in cfg.sources if s.enabled]),
                 len(scored), len(new_matches)))
    else:
        # Machine-readable output goes to stdout; silence the console alert so
        # it doesn't interleave plain text into the markdown/JSON.
        cfg.alerts.console = False

    alert_errors: List[str] = []
    if new_matches:
        alert_errors = send_all(new_matches, cfg.alerts)

    if fmt != "text":
        # HTML is a *report* format: show every current match (not just new
        # ones) so the page is a complete, shareable snapshot. JSON/markdown
        # keep the "new only" alert semantics.
        print(render(scored if fmt == "html" else new_matches, fmt))

    # Mark every job that matched as seen (so it won't re-alert). Jobs that
    # don't match are left unmarked, so they can alert later if your filters
    # change and they start matching.
    mark_seen(state, scored)
    removed = prune(state, args.max_age_days)
    if removed:
        _log("pruned %d stale state entries" % removed, args.verbose)
    save_state(cfg.state_file, state)

    for e in fetch_errors:
        print("  [warn] " + e, file=sys.stderr)
    for e in alert_errors:
        print("  [warn] alert failed: " + e, file=sys.stderr)
    return 0


def cmd_list(args) -> int:
    cfg = load_config(args.config)
    fmt = getattr(args, "format", "text")
    jobs, _ = _fetch_all(cfg, args.verbose)
    scored = filter_jobs(jobs, cfg.filters)
    if fmt == "text":
        print("%d job(s) fetched, %d match your filters (dry run, state untouched)"
              % (len(jobs), len(scored)))
        if scored:
            print(render(scored, "text"))
    else:
        print(render(scored, fmt))
    return 0


def cmd_watch(args) -> int:
    interval = args.interval or cfg_interval(args.config)
    print("watching every %ds (Ctrl-C to stop)" % interval)
    while True:
        try:
            cmd_scan(_args_with_interval(args))
        except Exception as exc:  # noqa: BLE001 - never let a scan kill the loop
            print("[gigwatch] scan error: %s" % exc, file=sys.stderr)
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nstopped.")
            return 0


def _build_profile(args, base: dict) -> dict:
    """Assemble a ranking profile from CLI flags (shared by rank + serve).

    ``--profile`` points at a JSON file; the individual flags (``--skills``,
    ``--title``, ``--location``, ``--notes``) layer on top of it. Returns a
    plain dict (possibly empty) suitable for :func:`gigwatch.ranking.rank_jobs`.
    """
    profile: dict = dict(base or {})
    if getattr(args, "profile", None):
        with open(args.profile, "r", encoding="utf-8") as fh:
            profile = json.load(fh)
    if getattr(args, "skills", None):
        profile["skills"] = [s.strip() for s in args.skills.split(",") if s.strip()]
    if getattr(args, "title", None):
        profile["title"] = args.title
    if getattr(args, "location", None):
        profile["location"] = args.location
    if getattr(args, "notes", None):
        profile["notes"] = args.notes
    return profile


def cmd_serve(args) -> int:
    """Run a self-contained hosted instance (dashboard + JSON + RSS + health)."""
    cfg = load_config(args.config)
    from gigwatch.server import Server
    profile = _build_profile(args, cfg.profile)
    server = Server(
        cfg,
        host=args.host,
        port=args.port,
        refresh=args.refresh,
        token=args.token,
        profile=profile or None,
    )
    return server.run()


def cfg_interval(config_path: str) -> int:
    try:
        return load_config(config_path).poll_interval
    except Exception:  # noqa: BLE001
        return 900


def _args_with_interval(args):
    return args


def cmd_digest(args) -> int:
    """Batched alerts: catch every new match, deliver one alert per period.

    Each run: fetch + filter, mark new matches seen (so ``scan``/``watch``
    won't double-alert), park them in the digest buffer, and — if the period
    has elapsed since the last flush — send ONE consolidated alert with
    everything pending.

    Intended pairing for the hosted offering:
      cron: gigwatch watch --interval 900   (keeps the buffer fresh)
      cron: gigwatch digest --interval 86400 (one email per day)
    """
    cfg = load_config(args.config)
    period = args.period or DEFAULT_PERIOD
    buffer_path = args.buffer or "gigwatch-digest.json"
    buffer = load_buffer(buffer_path)

    jobs, fetch_errors = _fetch_all(cfg, args.verbose)
    scored = filter_jobs(jobs, cfg.filters)
    state = load_state(cfg.state_file)
    new_matches = [s for s in scored if s.job.id not in state]

    # Catch: park every new match in the buffer (first-seen timestamp wins).
    added = add_pending(buffer, new_matches)

    # Dedupe: mark seen so scan/watch don't re-alert on the same jobs.
    mark_seen(state, scored)
    removed = prune(state, args.max_age_days)
    if removed:
        _log("pruned %d stale state entries" % removed, args.verbose)
    save_state(cfg.state_file, state)

    # Deliver: flush when the period has elapsed (or --force).
    due = is_due(buffer, period, force=args.force)
    if due:
        by_id = {s.job.id: s for s in scored}
        pending = build_digest(buffer, by_id)
        if pending:
            subject = "GigWatch digest: %d new matching gig(s) since last digest"
            alert_errors = send_digest(pending, cfg.alerts, subject=subject)
            for e in alert_errors:
                print("  [warn] digest alert failed: " + e, file=sys.stderr)
            print("digest: sent %d job(s) (buffer flushed)" % len(pending))
        else:
            print("digest: buffer has %d pending id(s) but none matched the "
                  "current scan; dropped" % len(buffer["jobs"]))
        flush(buffer)
    else:
        print("digest: %d pending, period not elapsed (%ds) — no alert yet"
              % (len(buffer["jobs"]), period))

    save_buffer(buffer_path, buffer)
    for e in fetch_errors:
        print("  [warn] " + e, file=sys.stderr)
    return 0


def cmd_rank(args) -> int:
    """Fetch + filter, then rank matches for a profile (AI or heuristic)."""
    cfg = load_config(args.config)
    fmt = getattr(args, "format", "text")
    jobs, _ = _fetch_all(cfg, args.verbose)
    scored = filter_jobs(jobs, cfg.filters)
    if not scored:
        print("no jobs matched your filters (nothing to rank)", file=sys.stderr)
        return 0

    profile: dict = dict(cfg.profile or {})
    if args.profile:
        with open(args.profile, "r", encoding="utf-8") as fh:
            profile = json.load(fh)
    elif args.skills:
        profile = {"skills": [s.strip() for s in args.skills.split(",") if s.strip()]}
    if args.title:
        profile["title"] = args.title
    if args.location:
        profile["location"] = args.location
    if args.notes:
        profile["notes"] = args.notes

    use_ai = not args.no_ai
    ranked = rank_jobs(scored, profile, use_ai=use_ai)
    method = ranked[0].method if ranked else "none"
    if fmt == "text":
        print("ranked %d match(es) via %s engine" % (len(ranked), method))
        print(_render_ranked_text(ranked))
    elif fmt == "markdown":
        print(_render_ranked_md(ranked))
    else:
        print(_render_ranked_json(ranked))
    return 0


def _render_ranked_text(ranked) -> str:
    lines: List[str] = []
    for i, r in enumerate(ranked, 1):
        j = r.job
        lines.append("%2d. [fit %s/100, %s] %s" % (i, r.score, r.method, j.title))
        if j.company:
            lines.append("     company: %s" % j.company)
        if j.salary:
            lines.append("     salary:  %s" % j.salary)
        if j.location:
            lines.append("     where:   %s" % j.location)
        if r.rationale:
            lines.append("     why:     %s" % r.rationale)
        lines.append("     %s" % j.url)
    return "\n".join(lines)


def _render_ranked_md(ranked) -> str:
    rows = [
        "| # | Title | Company | Salary | Location | Fit | Why | URL |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(ranked, 1):
        j = r.job
        rows.append("| %d | %s | %s | %s | %s | %s | %s | %s |" % (
            i, j.title.replace("|", "\\|"), j.company or "-",
            j.salary or "-", j.location or "-", r.score,
            r.rationale.replace("|", "\\|") or "-", j.url))
    return "\n".join(rows)


def _render_ranked_json(ranked) -> str:
    from dataclasses import asdict
    out = []
    for i, r in enumerate(ranked, 1):
        obj = asdict(r.job)
        obj["rank"] = i
        obj["fit_score"] = r.score
        obj["rationale"] = r.rationale
        obj["method"] = r.method
        obj["matched_keywords"] = list(r.matched_keywords)
        out.append(obj)
    return json.dumps(out, indent=2, ensure_ascii=False)


def cmd_reset(args) -> int:
    cfg = load_config(args.config)
    if os.path.exists(cfg.state_file):
        os.remove(cfg.state_file)
        print("cleared state file %s (next scan will alert on all matches)"
              % cfg.state_file)
    else:
        print("no state file at %s (nothing to reset)" % cfg.state_file)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gigwatch",
        description="Self-hosted freelance-gig watcher: monitor job feeds, "
                    "filter by your skills, alert on new matches.",
    )
    p.add_argument("--version", action="version", version="gigwatch %s" % __version__)
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--config", default=DEFAULT_CONFIG,
                        help="path to config file (default: %s)" % DEFAULT_CONFIG)
        sp.add_argument("--verbose", "-v", action="store_true")

    sp = sub.add_parser("init", help="write a starter config file")
    add_common(sp)
    sp.add_argument("--force", action="store_true", help="overwrite if it exists")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("scan", help="one-shot scan + alert")
    add_common(sp)
    sp.add_argument("--max-age-days", type=int, default=90,
                    help="drop state entries older than this (0 = keep all)")
    sp.add_argument("--format", choices=("text", "markdown", "json", "html"),
                    default="text", help="output format (default: text)")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("list", help="dry run: show matches, no state change")
    add_common(sp)
    sp.add_argument("--format", choices=("text", "markdown", "json", "html"),
                    default="text", help="output format (default: text)")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("watch", help="loop scan on an interval")
    add_common(sp)
    sp.add_argument("--interval", type=int, default=None,
                    help="seconds between scans (default: poll_interval from config)")
    sp.add_argument("--max-age-days", type=int, default=90,
                    help="drop state entries older than this (0 = keep all)")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser(
        "serve",
        help="run a self-contained hosted instance (live dashboard + JSON + RSS)",
    )
    add_common(sp)
    sp.add_argument("--host", default="127.0.0.1",
                    help="bind address (default: 127.0.0.1; use 0.0.0.0 to expose)")
    sp.add_argument("--port", type=int, default=8765,
                    help="port to listen on (default: 8765)")
    sp.add_argument("--refresh", type=int, default=900,
                    help="seconds between background refreshes (default: 900)")
    sp.add_argument("--token", default=None,
                    help="optional bearer token gating /api/jobs and /feed "
                         "(dashboard stays public)")
    sp.add_argument("--profile", default=None,
                    help="path to a JSON profile file (title/skills/location/notes) "
                         "used to AI-rank the top matches on the dashboard")
    sp.add_argument("--skills", default=None,
                    help="comma-separated skills (e.g. 'python,aws') for AI ranking")
    sp.add_argument("--title", default=None,
                    help="desired role title for AI ranking")
    sp.add_argument("--location", default=None,
                    help="preferred location for AI ranking")
    sp.add_argument("--notes", default=None,
                    help="free-text notes (e.g. 'senior, $150k+') for AI ranking")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser(
        "digest",
        help="batched alerts: catch every new match, deliver one alert/period",
    )
    add_common(sp)
    sp.add_argument("--period", type=int, default=None,
                    help="seconds between digest alerts "
                         "(default: %d = daily)" % DEFAULT_PERIOD)
    sp.add_argument("--buffer", default=None,
                    help="path to the digest buffer file "
                         "(default: gigwatch-digest.json)")
    sp.add_argument("--force", action="store_true",
                    help="flush the buffer now, even if the period hasn't elapsed")
    sp.add_argument("--max-age-days", type=int, default=90,
                    help="drop state entries older than this (0 = keep all)")
    sp.set_defaults(func=cmd_digest)

    sp = sub.add_parser("rank",
                        help="rank matched jobs for a profile (AI or heuristic)")
    add_common(sp)
    sp.add_argument("--profile", default=None,
                    help="path to a JSON profile file "
                         "(keys: title, skills[], location, notes)")
    sp.add_argument("--skills", default=None,
                    help="comma-separated skills (used if --profile omitted)")
    sp.add_argument("--title", default=None, help="desired role title")
    sp.add_argument("--location", default=None, help="preferred location")
    sp.add_argument("--notes", default=None,
                    help="free-text profile notes (e.g. 'senior, $150k+')")
    sp.add_argument("--no-ai", action="store_true",
                    help="force the deterministic heuristic engine")
    sp.add_argument("--format", choices=("text", "markdown", "json", "html"),
                    default="text", help="output format (default: text)")
    sp.set_defaults(func=cmd_rank)

    sp = sub.add_parser("reset", help="clear seen-state")
    add_common(sp)
    sp.set_defaults(func=cmd_reset)

    return p


def main(argv: List[str] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        # Only translate a missing *config* file into the friendly init
        # hint; a missing --profile / state file keeps its normal behaviour.
        cfg_path = getattr(args, "config", None) or DEFAULT_CONFIG
        if str(getattr(exc, "filename", None)) != str(cfg_path):
            raise
        print(
            "[gigwatch] no config file at %s. Run `gigwatch init` first "
            "(or point --config at an existing file)." % cfg_path,
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
