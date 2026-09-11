"""Alert delivery: console, email (SMTP), Slack (webhook or chat API).

All senders are best-effort: a failure in one channel never blocks the others
and never crashes the scan. Errors are returned so the CLI can report them.
"""

from __future__ import annotations

import json
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage
from typing import List, Optional

from gigwatch.config import AlertConfig
from gigwatch.filtering import ScoredJob


def format_jobs(jobs: List[ScoredJob], max_per_alert: int = 25) -> str:
    """Human-readable multi-line summary of new matches."""
    lines = ["GigWatch: %d new matching gig(s)" % len(jobs), ""]
    for i, s in enumerate(jobs[:max_per_alert], 1):
        j = s.job
        lines.append("%d. %s" % (i, j.title))
        if j.company:
            lines.append("   company: %s" % j.company)
        if j.salary:
            lines.append("   salary:  %s" % j.salary)
        if j.location:
            lines.append("   where:   %s" % j.location)
        if s.matched_keywords:
            lines.append("   matched: %s" % ", ".join(s.matched_keywords))
        lines.append("   score:   %s" % s.score)
        lines.append("   %s" % j.url)
        lines.append("")
    if len(jobs) > max_per_alert:
        lines.append("... and %d more (raise alerts.max_per_alert to see them)"
                     % (len(jobs) - max_per_alert))
    return "\n".join(lines).strip()


def send_console(jobs: List[ScoredJob], cfg: AlertConfig) -> Optional[str]:
    if not cfg.console:
        return None
    text = format_jobs(jobs, cfg.max_per_alert)
    print(text)
    return None


def send_email(jobs: List[ScoredJob], cfg: AlertConfig,
               subject: Optional[str] = None) -> Optional[str]:
    e = cfg.email
    if not e or not e.get("to"):
        return None
    try:
        msg = EmailMessage()
        msg["Subject"] = subject or "GigWatch: %d new matching gig(s)" % len(jobs)
        msg["From"] = e.get("from", "gigwatch@localhost")
        msg["To"] = e["to"] if isinstance(e["to"], str) else ", ".join(e["to"])
        msg.set_content(format_jobs(jobs, cfg.max_per_alert))

        host = e.get("smtp_host", "localhost")
        port = int(e.get("smtp_port", 587))
        use_tls = bool(e.get("use_tls", True))
        username = e.get("username")
        password = e.get("password", "")

        if use_tls:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls(context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=30)
        if username:
            server.login(username, password)
        server.send_message(msg)
        server.quit()
        return None
    except Exception as exc:  # noqa: BLE001 - best-effort alert
        return "email: %s" % exc


def send_slack(jobs: List[ScoredJob], cfg: AlertConfig) -> Optional[str]:
    s = cfg.slack
    if not s:
        return None
    text = format_jobs(jobs, cfg.max_per_alert)
    try:
        if s.get("webhook_url"):
            payload = {"text": text[:3900]}
            req = urllib.request.Request(
                s["webhook_url"],
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=30).read()
            return None
        if s.get("token") and s.get("channel"):
            payload = {"channel": s["channel"], "text": text[:3900]}
            req = urllib.request.Request(
                "https://slack.com/api/chat.postMessage",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": "Bearer %s" % s["token"],
                },
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
            if not resp.get("ok"):
                return "slack: %s" % resp.get("error")
            return None
        return "slack: no webhook_url or (token+channel) configured"
    except Exception as exc:  # noqa: BLE001
        return "slack: %s" % exc


def send_all(jobs: List[ScoredJob], cfg: AlertConfig,
             subject: Optional[str] = None) -> List[str]:
    """Send through every enabled channel. Returns a list of error strings.

    *subject* optionally overrides the email subject (used by the digest
    command to make the batched nature obvious).
    """
    errors: List[str] = []
    if not jobs:
        return errors
    for fn in (send_console, send_email, send_slack):
        err = fn(jobs, cfg, subject) if fn is send_email else fn(jobs, cfg)
        if err:
            errors.append(err)
    return errors
