"""Self-contained hosted server: ``gigwatch serve``.

This is the piece that turns GigWatch from a CLI tool into a *product you can
point a browser at*. One command starts a small HTTP server (stdlib only —
no Flask, no framework) that:

* runs a background refresh loop (fetch -> filter -> cache), and
* serves the current matches on a handful of endpoints:

  ``/``           a live HTML dashboard (auto-refreshes in the browser)
  ``/api/jobs``   the current matches as JSON (for integrations / scrapers)
  ``/feed``       the current matches as an RSS 2.0 feed (subscribe in any
                  feed reader)
  ``/health``     a liveness probe (uptime, last refresh, match count)

It is deliberately *stateless with respect to alerts*: the server does not
send email/Slack and does not touch the seen-state file. It is a read-only
window onto the latest filtered matches, which is exactly what a hosted
"$29/mo" offering is — a live, shareable page plus an RSS/JSON feed. The
alerting path (``scan`` / ``watch`` / ``digest``) stays separate so a hosted
instance never double-delivers.

Optional ``--token`` gates the API + feed endpoints with a simple
``Authorization: Bearer <token>`` check (the dashboard stays public so it can
be shared as a link). This is the minimal auth story for a hosted instance
behind a reverse proxy.
"""

from __future__ import annotations

import html
import json
import threading
import time
import urllib.parse
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

from gigwatch import __version__
from gigwatch.cli import _fetch_all
from gigwatch.config import Config
from gigwatch.filtering import ScoredJob, filter_jobs
from gigwatch.ranking import rank_jobs
from gigwatch.report import render

DEFAULT_REFRESH = 900  # 15 minutes
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_MAX_FEED = 25  # cap the RSS/JSON feed so a huge scan can't bloat a response
_RANK_CAP = 25  # cap the jobs we send to the (paid) ranking engine per refresh


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def run_once(cfg: Config, cache: Dict,
             profile: Optional[Dict] = None) -> None:
    """Fetch every enabled source, filter, and update *cache* in place.

    This is the unit that is tested directly (no socket needed) and that the
    background refresh thread calls on every tick. A source failure is
    recorded in ``cache["errors"]`` and never aborts the whole refresh.

    When *profile* is given (a dict with ``title``/``skills``/``location``/
    ``notes``), the matches are additionally ranked with
    :func:`gigwatch.ranking.rank_jobs` (AI when ``OPENAI_API_KEY`` is set,
    otherwise the deterministic heuristic) and the results are stored in
    ``cache["ranked"]`` — a list of ``RankedJob`` sorted best-first. Ranking
    is best-effort: any failure is swallowed and ``cache["ranked"]`` simply
    stays empty, so a flaky LLM endpoint can never break the dashboard.
    """
    jobs, errors = _fetch_all(cfg, verbose=False)
    scored = filter_jobs(jobs, cfg.filters)
    cache["last_refresh"] = time.time()
    cache["last_refresh_str"] = _utcnow()
    cache["fetched"] = len(jobs)
    cache["errors"] = errors
    cache["jobs"] = scored
    cache["by_id"] = {s.job.id: s for s in scored}

    if profile:
        try:
            ranked = rank_jobs(scored[:_RANK_CAP], profile, use_ai=True)
        except Exception:  # noqa: BLE001 - ranking must never break the loop
            ranked = []
        cache["ranked"] = ranked
        cache["rank_method"] = ranked[0].method if ranked else "none"
        cache["rank_profile"] = dict(profile)
    else:
        cache["ranked"] = []
        cache["rank_method"] = "none"
        cache["rank_profile"] = {}


def jobs_json(scored: List[ScoredJob]) -> str:
    """Serialize matches to JSON (all Job fields + score + matched_keywords)."""
    out = []
    for s in scored:
        obj = asdict(s.job)
        obj["score"] = s.score
        obj["matched_keywords"] = list(s.matched_keywords)
        out.append(obj)
    return json.dumps(out, indent=2, ensure_ascii=False)


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def rss_xml(scored: List[ScoredJob]) -> str:
    """Render the top *scored* matches as an RSS 2.0 feed."""
    now = _iso(time.time())
    items: List[str] = []
    for s in scored[:_MAX_FEED]:
        j = s.job
        desc = (j.description or "")[:300]
        if s.matched_keywords:
            desc = (desc + " " if desc else "") + "Matched: " + ", ".join(s.matched_keywords)
        items.append(
            "    <item>\n"
            "      <title>%s</title>\n"
            "      <link>%s</link>\n"
            "      <guid isPermaLink=\"true\">%s</guid>\n"
            "      <pubDate>%s</pubDate>\n"
            "      <description>%s</description>\n"
            "    </item>\n" % (
                _esc(j.title), _esc(j.url), _esc(j.url or j.id),
                _esc(j.published or now), _esc(desc),
            )
        )
    body = "".join(items)
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<rss version=\"2.0\">\n"
        "  <channel>\n"
        "    <title>GigWatch — matching gigs</title>\n"
        "    <link>https://github.com/earnnova-dev/gigwatch</link>\n"
        "    <description>Live GigWatch matches, refreshed every %ds</description>\n"
        "    <lastBuildDate>%s</lastBuildDate>\n"
        "%s"
        "  </channel>\n"
        "</rss>\n" % (DEFAULT_REFRESH, now, body)
    )


def dashboard_html(cache: Dict) -> str:
    """Render the live dashboard page (auto-refreshing, self-contained)."""
    scored: List[ScoredJob] = cache.get("jobs", [])
    ranked = cache.get("ranked") or []
    rank_method = cache.get("rank_method") or "none"
    rank_profile = cache.get("rank_profile") or {}
    errors: List[str] = cache.get("errors", [])
    last = cache.get("last_refresh_str") or "never"
    fetched = cache.get("fetched", 0)

    # --- Ranked "top matches" panel (the AI/human-readable pitch) ---
    ranked_html = ""
    if ranked:
        prof_bits = []
        if rank_profile.get("title"):
            prof_bits.append(_esc(rank_profile["title"]))
        if rank_profile.get("skills"):
            prof_bits.append(_esc(", ".join(rank_profile["skills"])))
        prof_bits += [_esc(x) for x in (
            rank_profile.get("location"), rank_profile.get("notes")) if x]
        prof_line = " &middot; ".join(prof_bits) if prof_bits else "custom profile"
        cards: List[str] = []
        for i, r in enumerate(ranked[:10], 1):
            j = r.job
            cards.append(
                "<div class='rank'>"
                "<span class='rank-n'>%d</span>"
                "<div class='rank-body'>"
                "<div class='rank-title'><a href='%s'>%s</a>"
                " <span class='fit'>fit %s/100</span></div>"
                "<div class='rank-meta'>%s%s</div>"
                "<div class='rank-why'>%s</div>"
                "</div></div>" % (
                    i, _esc(j.url), _esc(j.title) or "-", _esc(str(r.score)),
                    _esc(j.company) or "-",
                    (" &middot; " + _esc(j.salary)) if j.salary else "",
                    _esc(r.rationale) or "-",
                )
            )
        ranked_html = (
            "<div class='ranked'>"
            "<h2>Top matches <span class='rank-badge'>%s-ranked</span></h2>"
            "<div class='rank-prof'>ranked for: %s</div>"
            "%s"
            "</div>" % (
                _esc(rank_method), prof_line, "".join(cards))
        )
    else:
        ranked_html = (
            "<div class='ranked rank-off'>"
            "<h2>Top matches</h2>"
            "<div class='rank-prof'>start the server with <code>--profile "
            "ROLE --skills a,b,c</code> to rank these with AI</div>"
            "</div>"
        )

    rows: List[str] = []
    for i, s in enumerate(scored, 1):
        j = s.job
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
            "</tr>" % (
                i, _esc(j.url), _esc(j.title) or "-", _esc(j.company) or "-",
                _esc(j.salary) or "-", _esc(j.location) or "-",
                _esc(str(s.score)), _esc(j.source) or "-",
                _esc(", ".join(s.matched_keywords)) or "-",
            )
        )
    if rows:
        table = (
            "<table><thead><tr><th>#</th><th>Title</th><th>Company</th>"
            "<th>Salary</th><th>Location</th><th>Score</th><th>Source</th>"
            "<th>Matched</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        )
    else:
        table = ("<p class='empty'>No jobs match your filters right now — "
                 "the feed will update automatically.</p>")

    err_html = ""
    if errors:
        lis = "".join("<li>%s</li>" % _esc(e) for e in errors)
        err_html = ("<div class='errors'><strong>source warnings:</strong>"
                    "<ul>%s</ul></div>" % lis)

    return (
        "<!doctype html>\n"
        "<html lang='en'><head><meta charset='utf-8'>\n"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>\n"
        "<meta http-equiv='refresh' content='%d'>\n"
        "<title>GigWatch — %d matching gig(s)</title>\n"
        "<style>\n"
        "body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "margin:0;padding:24px;background:#0f1115;color:#e6e6e6}\n"
        "h1{font-size:20px;margin:0 0 4px}\n"
        ".meta{color:#8a93a3;font-size:13px;margin-bottom:16px}\n"
        ".meta a{color:#5aa9ff;text-decoration:none}\n"
        "table{border-collapse:collapse;width:100%%;font-size:14px}\n"
        "th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #232833}\n"
        "th{color:#8a93a3;font-weight:600;text-transform:uppercase;font-size:11px;"
        "letter-spacing:.04em}\n"
        "tr:hover td{background:#161b24}\n"
        "a{color:#5aa9ff;text-decoration:none}\n"
        "a:hover{text-decoration:underline}\n"
        ".empty{color:#8a93a3}\n"
        ".errors{margin:0 0 16px;padding:10px 12px;background:#2a1c1c;"
        "border:1px solid #4a2a2a;border-radius:6px;font-size:13px;color:#e0a0a0}\n"
        ".errors ul{margin:6px 0 0;padding-left:18px}\n"
        ".ranked{margin:0 0 20px}\n"
        ".ranked h2{font-size:15px;margin:0 0 4px}\n"
        ".rank-badge{display:inline-block;margin-left:8px;padding:1px 8px;\n"
        "background:#1c2b1c;border:1px solid #2f5a2f;border-radius:10px;\n"
        "font-size:11px;color:#7fd07f;text-transform:uppercase;letter-spacing:.04em}\n"
        ".rank-prof{color:#8a93a3;font-size:12px;margin:0 0 10px}\n"
        ".rank-prof code{color:#c9d1de;background:#161b24;padding:1px 5px;\n"
        "border-radius:4px;font-size:11px}\n"
        ".rank{display:flex;gap:12px;padding:10px 12px;margin-bottom:8px;\n"
        "background:#141922;border:1px solid #232833;border-radius:8px}\n"
        ".rank-n{flex:0 0 26px;height:26px;line-height:26px;text-align:center;\n"
        "background:#1c2b1c;color:#7fd07f;border-radius:50%%;font-weight:700;\n"
        "font-size:13px}\n"
        ".rank-body{min-width:0}\n"
        ".rank-title{font-size:14px;font-weight:600}\n"
        ".rank-title a{color:#e6e6e6}\n"
        ".rank-title a:hover{color:#5aa9ff}\n"
        ".fit{color:#7fd07f;font-size:12px;font-weight:700;margin-left:6px}\n"
        ".rank-meta{color:#8a93a3;font-size:12px;margin-top:2px}\n"
        ".rank-why{color:#c9d1de;font-size:12px;margin-top:4px;\n"
        "font-style:italic}\n"
        ".rank-off .rank-prof{margin:0}\n"
        ".foot{margin-top:20px;color:#5b6472;font-size:12px}\n"
        ".foot a{color:#5b6472}\n"
        "</style></head><body>\n"
        "<h1>GigWatch &mdash; live matching gigs</h1>\n"
        "<div class='meta'>%d match(es) &middot; %d fetched &middot; updated %s "
        "&middot; auto-refreshes every %ds</div>\n"
        "%s\n"
        "%s\n"
        "%s\n"
        "<div class='foot'>Self-hosted gig watcher &middot; "
        "<a href='https://github.com/earnnova-dev/gigwatch'>gigwatch-nova</a> "
        "&middot; <a href='/api/jobs'>JSON</a> &middot; "
        "<a href='/feed'>RSS</a> &middot; <a href='/health'>health</a>"
        "</div>\n"
        "</body></html>\n" % (
            DEFAULT_REFRESH, len(scored), len(scored), fetched, last,
            DEFAULT_REFRESH, ranked_html, err_html, table,
        )
    )


class _Handler(BaseHTTPRequestHandler):
    """HTTP handler bound to a shared cache (and optional token)."""

    server_version = "GigWatch/%s" % __version__
    protocol_version = "HTTP/1.1"
    cache: Dict = {}
    token: Optional[str] = None
    start_time: float = 0.0

    def log_message(self, fmt, *args):  # noqa: D401 - silence per-request logs
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.token:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == "Bearer " + self.token

    def do_GET(self):  # noqa: N802 - http.server naming
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            cache = self.cache
            payload = {
                "status": "ok",
                "version": __version__,
                "uptime_s": round(time.time() - self.start_time, 1),
                "last_refresh": cache.get("last_refresh"),
                "last_refresh_str": cache.get("last_refresh_str"),
                "matches": len(cache.get("jobs", [])),
                "fetched": cache.get("fetched", 0),
                "source_errors": cache.get("errors", []),
            }
            self._send(200, json.dumps(payload, indent=2).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/jobs":
            if not self._authorized():
                self._send(401, b'{"error":"unauthorized"}',
                           "application/json; charset=utf-8")
                return
            self._send(200, jobs_json(self.cache.get("jobs", [])).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/feed":
            if not self._authorized():
                self._send(401, b"unauthorized", "text/plain; charset=utf-8")
                return
            self._send(200, rss_xml(self.cache.get("jobs", [])).encode("utf-8"),
                       "application/rss+xml; charset=utf-8")
        elif path == "/":
            self._send(200, dashboard_html(self.cache).encode("utf-8"),
                       "text/html; charset=utf-8")
        else:
            self._send(404, b'{"error":"not found"}',
                       "application/json; charset=utf-8")


class Server:
    """A GigWatch hosted instance: HTTP server + background refresh thread."""

    def __init__(self, cfg: Config, host: str = DEFAULT_HOST,
                 port: int = DEFAULT_PORT, refresh: int = DEFAULT_REFRESH,
                 token: Optional[str] = None,
                 profile: Optional[Dict] = None):
        self.cfg = cfg
        self.host = host
        self.port = port
        self.refresh = refresh
        self.token = token
        self.profile = profile
        self.cache: Dict = {
            "last_refresh": 0,
            "last_refresh_str": "never",
            "fetched": 0,
            "errors": [],
            "jobs": [],
            "by_id": {},
            "ranked": [],
            "rank_method": "none",
            "rank_profile": dict(profile) if profile else {},
        }
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def _refresh_loop(self) -> None:
        while True:
            try:
                run_once(self.cfg, self.cache, profile=self.profile)
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                self.cache["errors"] = ["refresh: %s" % exc]
            time.sleep(self.refresh)

    def _start_http(self) -> "ThreadingHTTPServer":
        """Create and bind the HTTP server (non-blocking). Returns the server."""
        handler = type("BoundHandler", (_Handler,), {
            "cache": self.cache,
            "token": self.token,
            "start_time": time.time(),
        })
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        if self._httpd.server_address[1] != self.port:
            self.port = self._httpd.server_address[1]
        return self._httpd

    def _serve(self) -> threading.Thread:
        """Run serve_forever in a daemon thread (non-blocking; for tests)."""
        t = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        t.start()
        return t

    def run(self) -> int:
        """Start the server and block until interrupted (Ctrl-C)."""
        self._start_http()
        self._thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._thread.start()
        print("GigWatch server v%s listening on http://%s:%d (refresh every %ds)"
              % (__version__, self.host, self.port, self.refresh))
        print("  dashboard: http://%s:%d/" % (self.host, self.port))
        print("  json:      http://%s:%d/api/jobs" % (self.host, self.port))
        print("  rss:       http://%s:%d/feed" % (self.host, self.port))
        print("  health:    http://%s:%d/health" % (self.host, self.port))
        if self.token:
            print("  (api/feed require: Authorization: Bearer <token>)")
        print("Ctrl-C to stop.")
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopping.")
        finally:
            self._httpd.server_close()
        return 0
