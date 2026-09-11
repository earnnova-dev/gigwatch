"""Render matched jobs in different output formats.

Used by the ``scan`` and ``list`` commands via their ``--format`` flag:

* ``text`` (default) - the indented human-readable listing;
* ``markdown`` - a GitHub-flavoured table (``#``, Title, Company, Salary,
  Location, Score, URL);
* ``json`` - an array of job objects (every :class:`~gigwatch.sources.Job`
  field, plus ``score`` and ``matched_keywords``).
"""

from __future__ import annotations

import html as _html
import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import List

from gigwatch.filtering import ScoredJob

FORMATS = ("text", "markdown", "json", "html")

_MD_COLUMNS = ("#", "Title", "Company", "Salary", "Location", "Score", "URL")


def render(scored: List[ScoredJob], fmt: str = "text") -> str:
    """Return *scored* rendered as *fmt* (one of :data:`FORMATS`)."""
    if fmt == "text":
        return _render_text(scored)
    if fmt == "markdown":
        return _render_markdown(scored)
    if fmt == "json":
        return _render_json(scored)
    if fmt == "html":
        return _render_html(scored)
    raise ValueError("unknown format: %r" % fmt)


def _render_text(scored: List[ScoredJob]) -> str:
    lines: List[str] = []
    for i, s in enumerate(scored, 1):
        j = s.job
        lines.append("%2d. [score %s] %s" % (i, s.score, j.title))
        if j.company:
            lines.append("     company: %s" % j.company)
        if j.salary:
            lines.append("     salary:  %s" % j.salary)
        if j.location:
            lines.append("     where:   %s" % j.location)
        if s.matched_keywords:
            lines.append("     matched: %s" % ", ".join(s.matched_keywords))
        lines.append("     %s" % j.url)
    return "\n".join(lines)


def _md_cell(value) -> str:
    """Sanitise a value for use inside a Markdown table cell."""
    text = str(value).replace("|", "\\|").replace("\n", " ").strip()
    return text or "-"


def _render_markdown(scored: List[ScoredJob]) -> str:
    rows = [
        "| " + " | ".join(_MD_COLUMNS) + " |",
        "|" + "|".join(["---"] * len(_MD_COLUMNS)) + "|",
    ]
    for i, s in enumerate(scored, 1):
        j = s.job
        rows.append(
            "| " + " | ".join(
                [
                    str(i),
                    _md_cell(j.title),
                    _md_cell(j.company),
                    _md_cell(j.salary),
                    _md_cell(j.location),
                    _md_cell(s.score),
                    _md_cell(j.url),
                ]
            ) + " |"
        )
    return "\n".join(rows)


def _render_json(scored: List[ScoredJob]) -> str:
    out = []
    for s in scored:
        obj = asdict(s.job)
        obj["score"] = s.score
        obj["matched_keywords"] = list(s.matched_keywords)
        out.append(obj)
    return json.dumps(out, indent=2, ensure_ascii=False)


def _render_html(scored: List[ScoredJob]) -> str:
    """Render matches as a self-contained HTML page.

    A single file with inline CSS — no external assets — so it can be saved
    to disk, emailed as an attachment, or published to GitHub Pages as a
    live demo. This is the format that makes GigWatch *presentable* to a
    client or a job board.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows: List[str] = []
    for i, s in enumerate(scored, 1):
        j = s.job
        title = _html.escape(j.title) or "-"
        company = _html.escape(j.company) or "-"
        salary = _html.escape(j.salary) or "-"
        location = _html.escape(j.location) or "-"
        score = _html.escape(str(s.score))
        url = _html.escape(j.url, quote=True)
        src = _html.escape(j.source) or "-"
        kw = ", ".join(_html.escape(k) for k in s.matched_keywords) or "-"
        rows.append(
            "<tr>"
            "<td>%d</td>"
            "<td><a href=\"%s\">%s</a></td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "</tr>" % (i, url, title, company, salary, location, score, src, kw)
        )
    if not rows:
        body = (
            "<p class='empty'>No jobs matched your filters in this scan. "
            "Adjust <code>filters.keywords</code> or add more sources.</p>"
        )
    else:
        body = (
            "<table>"
            "<thead><tr>"
            "<th>#</th><th>Title</th><th>Company</th><th>Salary</th>"
            "<th>Location</th><th>Score</th><th>Source</th><th>Matched</th>"
            "</tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody>"
            "</table>"
        )
    return (
        "<!doctype html>\n"
        "<html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>GigWatch — %d matching gig(s)</title>\n"
        "<style>"
        "body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "margin:0;padding:24px;background:#0f1115;color:#e6e6e6}"
        "h1{font-size:20px;margin:0 0 4px}"
        ".meta{color:#8a93a3;font-size:13px;margin-bottom:16px}"
        "table{border-collapse:collapse;width:100%%;font-size:14px}"
        "th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #232833}"
        "th{color:#8a93a3;font-weight:600;text-transform:uppercase;font-size:11px;"
        "letter-spacing:.04em}"
        "tr:hover td{background:#161b24}"
        "a{color:#5aa9ff;text-decoration:none}"
        "a:hover{text-decoration:underline}"
        ".empty{color:#8a93a3}"
        "code{background:#1a2029;padding:1px 5px;border-radius:4px}"
        ".foot{margin-top:20px;color:#5b6472;font-size:12px}"
        "</style></head><body>"
        "<h1>GigWatch &mdash; matching gigs</h1>"
        "<div class='meta'>%d match(es) &middot; generated %s</div>\n"
        "%s"
        "<div class='foot'>Self-hosted gig watcher &middot; "
        "<a href='https://github.com/earnnova-dev/gigwatch'>gigwatch-nova</a></div>"
        "</body></html>"
    ) % (len(scored), len(scored), now, body)
