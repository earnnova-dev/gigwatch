"""Unit tests for GigWatch. No network required (see tests/test_live.py)."""

import json
import os
import time

import pytest

from gigwatch.config import Config, Filters, SourceConfig, load_config
from gigwatch.filtering import filter_jobs, score_job
from gigwatch import sources as sources_mod
from gigwatch.sources import Job, fetch, fetch_remoteok, fetch_wwr
from gigwatch.ranking import rank_jobs
from gigwatch.state import load_state, mark_seen, new_ids, prune, save_state
from gigwatch.alerts import format_jobs
from gigwatch.filtering import ScoredJob
from gigwatch.report import render


def make_job(jid="j1", title="Senior Python Backend Engineer",
             company="Acme", url="https://example.com/j/1",
             category="Information Technology", location="Remote (Worldwide)",
             salary="$120k-$150k", tags=["python", "django"],
             description="Build APIs with Python and Django."):
    return Job(id=jid, title=title, company=company, url=url, category=category,
               location=location, salary=salary, tags=tags,
               published="2026-09-01T00:00:00", source="test",
               description=description)


# ---------- filtering ----------

def test_keyword_any_match():
    f = Filters(keywords=["python", "rust"])
    jobs = [make_job(title="Python Backend Dev"), make_job(title="Rust Systems Dev"),
            make_job(title="Graphic Designer")]
    out = filter_jobs(jobs, f)
    assert [s.job.id for s in out] == ["j1", "j1", "j1"][:2] or len(out) == 2
    titles = {s.job.title for s in out}
    assert "Graphic Designer" not in titles

def test_keyword_all_match():
    f = Filters(keywords=["python", "django"], require_all_keywords=True)
    jobs = [make_job(title="Python Backend Dev"),
            make_job(title="Django Developer")]
    out = filter_jobs(jobs, f)
    assert len(out) == 0  # neither job has BOTH words

def test_exclude_keyword_blocks():
    f = Filters(keywords=["python"], exclude_keywords=["intern"])
    jobs = [make_job(title="Python Intern"), make_job(title="Python Engineer")]
    out = filter_jobs(jobs, f)
    assert [s.job.title for s in out] == ["Python Engineer"]

def test_category_filter():
    f = Filters(keywords=["python"], categories=["design"])
    jobs = [make_job(title="Python Dev", category="Information Technology")]
    assert filter_jobs(jobs, f) == []

def test_location_filter():
    f = Filters(keywords=["python"], locations=["worldwide"])
    jobs = [make_job(title="Python Dev", location="Remote (Worldwide)"),
            make_job(title="Python Dev", location="New York, USA")]
    out = filter_jobs(jobs, f)
    assert len(out) == 1 and "Worldwide" in out[0].job.location

def test_min_score_threshold():
    f = Filters(keywords=["python", "django"], min_score=4.0)
    # only one keyword in title -> score 3.0 < 4.0
    job = make_job(title="Python Dev", description="uses django in prod")
    out = filter_jobs([job], f)
    assert out == []

def test_score_title_hits_count_more():
    f = Filters(keywords=["python", "api"])
    title_hit = make_job(title="Python API Engineer", description="misc")
    body_hit = make_job(jid="j2", title="Backend Engineer",
                        description="python api work")
    assert score_job(title_hit, f) > score_job(body_hit, f)

def test_no_keywords_matches_everything():
    f = Filters(keywords=[])
    out = filter_jobs([make_job()], f)
    assert len(out) == 1 and out[0].score == 1.0


# ---------- state ----------

def test_state_roundtrip(tmp_path):
    p = str(tmp_path / "state.json")
    state = load_state(p)
    assert state == {}
    mark_seen(state, [ScoredJob(job=make_job("a"), score=1, matched_keywords=[])])
    save_state(p, state)
    again = load_state(p)
    assert again == state and "a" in again

def test_new_ids_only_unseen(tmp_path):
    p = str(tmp_path / "state.json")
    state = load_state(p)
    a, b = make_job("a"), make_job("b")
    mark_seen(state, [ScoredJob(job=a, score=1, matched_keywords=[])])
    save_state(p, state)
    state = load_state(p)
    fresh = [ScoredJob(job=a, score=1, matched_keywords=[]),
             ScoredJob(job=b, score=1, matched_keywords=[])]
    new = new_ids(state, fresh)
    assert [j.job.id for j in new] == ["b"]

def test_prune_drops_old_entries():
    state = {}
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(0))  # 1970
    state["old"] = old
    state["new"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    removed = prune(state, max_age_days=90)
    assert removed == 1 and "new" in state and "old" not in state

def test_prune_zero_keeps_all():
    state = {"x": "1970-01-01T00:00:00Z"}
    assert prune(state, max_age_days=0) == 0 and "x" in state


# ---------- config ----------

def test_load_config_minimal(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"sources": [{"type": "remotive"}]}))
    cfg = load_config(str(p))
    assert len(cfg.sources) == 1 and cfg.sources[0].type == "remotive"
    assert cfg.state_file == "gigwatch-state.json"

def test_load_config_rejects_bad_source(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"sources": [{"type": "nope"}]}))
    with pytest.raises(ValueError):
        load_config(str(p))

def test_load_config_requires_source_for_rss(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"sources": [{"type": "rss"}]}))
    with pytest.raises(ValueError):
        load_config(str(p))

def test_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("GIGWATCH_EMAIL_TO", "me@example.com")
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "sources": [{"type": "remotive"}],
        "alerts": {"console": True, "email": {"to": "${GIGWATCH_EMAIL_TO}"}},
    }))
    cfg = load_config(str(p))
    assert cfg.alerts.email["to"] == "me@example.com"


# ---------- new sources: We Work Remotely + RemoteOK (mocked HTTP) ----------

WWR_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>We Work Remotely</title>
<item>
  <title>Acme Inc: Senior Python Engineer</title>
  <region>Anywhere in the World</region>
  <category>Back-End Programming</category>
  <pubDate>Mon, 08 Sep 2026 12:00:00 +0000</pubDate>
  <link>https://weworkremotely.com/remote-jobs/acme-senior-python-engineer</link>
  <description>&lt;p&gt;We need a Python expert.&lt;/p&gt;</description>
</item>
<item>
  <title>Globex: React Developer</title>
  <region>USA Only</region>
  <category>Front-End Programming</category>
  <link>https://weworkremotely.com/remote-jobs/globex-react-developer</link>
  <description>Build UIs.</description>
</item>
</channel></rss>"""

REMOTEOK_JSON = json.dumps([
    {"legal": "API Terms of Service: link back to Remote OK."},
    {
        "id": "12345",
        "position": "Senior Backend Engineer",
        "company": "Acme",
        "url": "https://remoteOK.com/remote-jobs/12345",
        "location": "Worldwide",
        "salary_min": 120000,
        "salary_max": 150000,
        "tags": ["python", "golang"],
        "date": "2026-09-08T12:00:00+00:00",
        "description": "<p>Python and Go.</p>",
    },
    {
        "id": "12346",
        "position": "Product Designer",
        "company": "Globex",
        "url": "https://remoteOK.com/remote-jobs/12346",
        "description": "Design things.",
    },
]).encode("utf-8")


def _mock_http(monkeypatch, payload):
    monkeypatch.setattr(sources_mod, "_http_get",
                        lambda url, timeout=25: payload)


def test_fetch_wwr_parses_company_region_and_source(monkeypatch):
    _mock_http(monkeypatch, WWR_RSS)
    jobs = fetch_wwr()
    assert [j.title for j in jobs] == ["Senior Python Engineer", "React Developer"]
    assert jobs[0].company == "Acme Inc"
    assert jobs[0].location == "Anywhere in the World"
    assert jobs[0].category == "Back-End Programming"
    assert jobs[0].source == "wwr"
    assert jobs[0].url.endswith("acme-senior-python-engineer")
    assert jobs[0].id and "Python" in jobs[0].description


def test_fetch_wwr_limit(monkeypatch):
    _mock_http(monkeypatch, WWR_RSS)
    assert len(fetch_wwr(limit=1)) == 1


def test_fetch_remoteok_parses_fields_and_skips_metadata(monkeypatch):
    _mock_http(monkeypatch, REMOTEOK_JSON)
    jobs = fetch_remoteok()
    assert [j.title for j in jobs] == ["Senior Backend Engineer", "Product Designer"]
    assert jobs[0].company == "Acme"
    assert jobs[0].id == "12345"
    assert jobs[0].salary == "$120,000-$150,000"
    assert jobs[0].location == "Worldwide"
    assert jobs[0].tags == ["python", "golang"]
    assert jobs[0].source == "remoteok"
    assert jobs[1].salary == ""


def test_fetch_dispatch_new_sources(monkeypatch):
    _mock_http(monkeypatch, WWR_RSS)
    got = fetch(SourceConfig(type="wwr"))
    assert got and got[0].source == "wwr"
    _mock_http(monkeypatch, REMOTEOK_JSON)
    got = fetch(SourceConfig(type="remoteok", limit=1))
    assert len(got) == 1 and got[0].source == "remoteok"


def test_load_config_accepts_new_sources(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"sources": [{"type": "wwr"}, {"type": "remoteok"}]}))
    cfg = load_config(str(p))
    assert [s.type for s in cfg.sources] == ["wwr", "remoteok"]


# ---------- report formatting (text / markdown / json) ----------

def _scored(**kw):
    score = kw.pop("score", 9.0)
    matched = kw.pop("matched_keywords", ["python"])
    return ScoredJob(job=make_job(**kw), score=score, matched_keywords=matched)


def test_render_text_matches_listing():
    out = render([_scored()], "text")
    assert "[score 9.0] Senior Python Backend Engineer" in out
    assert "company: Acme" in out
    assert "https://example.com/j/1" in out


def test_render_markdown_table():
    out = render([_scored()], "markdown")
    lines = out.splitlines()
    assert lines[0] == "| # | Title | Company | Salary | Location | Score | URL |"
    assert set(lines[1]) == set("|-")
    assert "| 1 | Senior Python Backend Engineer | Acme | $120k-$150k | " \
           "Remote (Worldwide) | 9.0 | https://example.com/j/1 |" in out


def test_render_markdown_escapes_pipes():
    out = render([_scored(title="Data | Pipeline Eng")], "markdown")
    assert "Data \\| Pipeline Eng" in out


def test_render_markdown_empty_still_has_header():
    out = render([], "markdown")
    assert out.splitlines()[0].startswith("| # | Title")


def test_render_json_all_fields():
    data = json.loads(render([_scored()], "json"))
    assert isinstance(data, list) and len(data) == 1
    obj = data[0]
    for key in ("id", "title", "company", "url", "category", "location",
                "salary", "tags", "published", "source", "description"):
        assert key in obj
    assert obj["score"] == 9.0
    assert obj["matched_keywords"] == ["python"]


def test_render_json_empty():
    assert json.loads(render([], "json")) == []


def test_render_unknown_format():
    with pytest.raises(ValueError):
        render([], "yaml")


def test_parser_accepts_format():
    from gigwatch.cli import build_parser
    assert build_parser().parse_args(["list", "--format", "json"]).format == "json"
    assert build_parser().parse_args(["scan", "--format", "markdown"]).format == "markdown"


# ---------- alerts formatting ----------

def test_format_jobs_output():
    s = ScoredJob(job=make_job(), score=6.0, matched_keywords=["python"])
    text = format_jobs([s], max_per_alert=5)
    assert "1 new matching gig" in text
    assert "Senior Python Backend Engineer" in text
    assert "Acme" in text
    assert "https://example.com/j/1" in text
    assert "python" in text


# ---------- HN "Who is Hiring" adapter (mocked Algolia) ----------

HN_SEARCH = json.dumps({
    "hits": [
        {"objectID": "42000001", "title": "Ask HN: Who is hiring? (September 2026)"},
        {"objectID": "42000002", "title": "Who is working at ...? (September 2026)"},
    ],
}).encode("utf-8")

# The items/{id} endpoint returns the story with nested top-level comments.
HN_ITEM = json.dumps({
    "id": "42000001",
    "title": "Ask HN: Who is hiring? (September 2026)",
    "children": [
        {
            "id": "42000003",
            "text": (
                "I'm hiring!\n"
                "Acme — Senior Python Engineer | Remote (Worldwide) | Full-time | "
                "https://acme.com/jobs/1\n"
                "Globex: Backend Developer (Go) | USA | Contract | "
                "https://globex.com/jobs/2\n"
                "Initech — Product Designer | Hybrid NYC | Full-time\n"
                "Just a note about hiring culture, not a listing.\n"
                "https://random-link.com\n"
            ),
        },
        {
            "id": "42000004",
            "text": "Umbra — Staff Rust Engineer | Remote | $180k | "
                   "https://umbra.com/jobs/9\n",
        },
    ],
}).encode("utf-8")


def _mock_hn(monkeypatch):
    def fake_http(url, timeout=25):
        if "/items/" in url:
            return HN_ITEM
        return HN_SEARCH
    monkeypatch.setattr(sources_mod, "_http_get", fake_http)


def test_hn_parse_jobs_extracts_listings():
    from gigwatch.sources import _hn_parse_jobs
    text = (
        "I'm hiring!\n"
        "Acme — Senior Python Engineer | Remote | Full-time | https://acme.com/1\n"
        "Globex: Backend Developer (Go) | USA | Contract | https://globex.com/2\n"
        "Just a note about hiring culture, not a listing.\n"
        "https://random-link.com\n"
    )
    jobs = _hn_parse_jobs(text)
    assert ("Senior Python Engineer", "Acme") in jobs
    assert ("Backend Developer (Go)", "Globex") in jobs
    # non-job lines are dropped
    for title, company in jobs:
        assert "note" not in title.lower()
        assert company != "https"


def test_hn_parse_jobs_handles_dashes_and_colons():
    from gigwatch.sources import _hn_parse_jobs
    jobs = _hn_parse_jobs("Acme — Backend Engineer | Remote\nGlobex: Product Designer\n")
    assert ("Backend Engineer", "Acme") in jobs
    assert ("Product Designer", "Globex") in jobs


def test_fetch_hn_parses_thread_and_dedupes(monkeypatch):
    _mock_hn(monkeypatch)
    jobs = sources_mod.fetch_hn()
    titles = [j.title for j in jobs]
    assert "Senior Python Engineer" in titles
    assert "Staff Rust Engineer" in titles
    assert all(j.source == "hn" for j in jobs)
    assert all(j.url.startswith("https://news.ycombinator.com/item?id=")
               for j in jobs)
    # ids are unique (deduped)
    assert len({j.id for j in jobs}) == len(jobs)


def test_fetch_hn_limit(monkeypatch):
    _mock_hn(monkeypatch)
    assert len(sources_mod.fetch_hn(limit=1)) == 1


def test_fetch_dispatch_hn(monkeypatch):
    _mock_hn(monkeypatch)
    got = fetch(SourceConfig(type="hn"))
    assert got and all(j.source == "hn" for j in got)


def test_load_config_accepts_hn(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"sources": [{"type": "hn"}]}))
    cfg = load_config(str(p))
    assert cfg.sources[0].type == "hn"


# ---------- ranking (heuristic + AI) ----------

def _profile():
    return {"title": "Backend Engineer", "skills": ["python", "backend"],
            "location": "Remote", "notes": "senior, $150k+"}


def test_heuristic_ranks_skill_match_first():
    from gigwatch.ranking import rank_heuristic
    jobs = [
        _scored(jid="a", title="Senior Python Backend Engineer",
                salary="$150k-$180k", location="Remote (Worldwide)"),
        _scored(jid="b", title="Product Designer", salary="",
                location="New York"),
        _scored(jid="c", title="Junior Python Developer", salary="$60k",
                location="Remote"),
    ]
    out = rank_heuristic(jobs, _profile())
    assert [r.job.id for r in out][0] == "a"
    assert all(0 <= r.score <= 100 for r in out)
    assert all(r.method == "heuristic" for r in out)
    assert out[0].score > out[-1].score
    assert any("python" in r.rationale.lower() for r in out[:1])


def test_heuristic_is_deterministic():
    from gigwatch.ranking import rank_heuristic
    jobs = [_scored(jid="a", title="Senior Python Backend Engineer"),
            _scored(jid="b", title="Product Designer")]
    assert [r.score for r in rank_heuristic(jobs, _profile())] == \
           [r.score for r in rank_heuristic(jobs, _profile())]


def test_rank_jobs_no_key_falls_back_to_heuristic(monkeypatch):
    from gigwatch.ranking import rank_jobs
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    jobs = [_scored(jid="a", title="Senior Python Backend Engineer")]
    out = rank_jobs(jobs, _profile(), use_ai=True)
    assert out and out[0].method == "heuristic"


def test_rank_jobs_ai_path(monkeypatch):
    from gigwatch import ranking as rk
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://fake.invalid/v1")
    monkeypatch.setenv("GIGWATCH_AI_MODEL", "test-model")
    monkeypatch.setattr(rk, "_ai_chat", lambda prompt, k, b, m, timeout=60:
                        json.dumps({"scores": [
                            {"id": "j1", "score": 92, "rationale": "strong match"},
                        ]}))
    jobs = [_scored(jid="j1", title="Senior Python Backend Engineer")]
    out = rank_jobs(jobs, _profile(), use_ai=True)
    assert out[0].method == "ai"
    assert out[0].score == 92
    assert out[0].rationale == "strong match"


def test_rank_jobs_ai_failure_falls_back_per_batch(monkeypatch):
    from gigwatch import ranking as rk
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(rk, "_ai_chat",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    jobs = [_scored(jid="j1", title="Senior Python Backend Engineer")]
    out = rank_jobs(jobs, _profile(), use_ai=True)
    assert out and out[0].method == "heuristic"


def test_rank_jobs_no_ai_flag():
    from gigwatch.ranking import rank_jobs
    jobs = [_scored(jid="j1", title="Senior Python Backend Engineer")]
    out = rank_jobs(jobs, _profile(), use_ai=False)
    assert out and out[0].method == "heuristic"


def test_parser_accepts_rank():
    from gigwatch.cli import build_parser
    args = build_parser().parse_args(
        ["rank", "--skills", "python,backend", "--title", "Backend Engineer",
         "--no-ai", "--format", "json"])
    assert args.skills == "python,backend"
    assert args.no_ai is True
    assert args.format == "json"


# ---------- HTML report format (v0.3.0) ----------

def test_render_html_is_a_full_page():
    out = render([_scored()], "html")
    assert out.lstrip().lower().startswith("<!doctype html>")
    assert "<html" in out and "</html>" in out
    assert "<style>" in out
    assert "<title>GigWatch" in out
    assert "Senior Python Backend Engineer" in out
    assert "Acme" in out
    assert "https://example.com/j/1" in out
    assert "$120k-$150k" in out
    assert "<td>9.0</td>" in out


def test_render_html_escapes_special_characters():
    out = render([_scored(title="Data <&> Pipeline")], "html")
    assert "Data &lt;&amp;&gt; Pipeline" in out
    assert "Data <&> Pipeline" not in out


def test_render_html_links_point_to_job_urls():
    out = render([_scored()], "html")
    assert '<a href="https://example.com/j/1"' in out


def test_render_html_empty_list_still_renders():
    out = render([], "html")
    assert out.lstrip().lower().startswith("<!doctype html>")
    assert "0" in out  # count badge


def test_render_html_format_registered_in_parser():
    from gigwatch.cli import build_parser
    args = build_parser().parse_args(["list", "--format", "html"])
    assert args.format == "html"


# ---------- Batched digest (v0.3.0) ----------

def test_buffer_roundtrip(tmp_path):
    from gigwatch.digest import load_buffer, save_buffer
    p = str(tmp_path / "digest.json")
    buf = load_buffer(p)
    assert buf == {"last_flush": 0, "jobs": {}}
    buf["last_flush"] = 123
    buf["jobs"] = {"j1": 100, "j2": 200}
    save_buffer(p, buf)
    again = load_buffer(p)
    assert again == {"last_flush": 123, "jobs": {"j1": 100, "j2": 200}}
    # arrival order preserved
    assert list(again["jobs"]) == ["j1", "j2"]


def test_buffer_corrupt_file_resets(tmp_path):
    from gigwatch.digest import load_buffer
    p = tmp_path / "digest.json"
    p.write_text("not json at all")
    assert load_buffer(str(p)) == {"last_flush": 0, "jobs": {}}


def test_add_pending_dedupes_and_keeps_first_seen(monkeypatch):
    from gigwatch import digest
    monkeypatch.setattr(digest, "_now", lambda: 1000)
    buf = {"last_flush": 0, "jobs": {}}
    added = digest.add_pending(buf, [_scored(jid="j1"), _scored(jid="j2")])
    assert added == 2
    assert buf["jobs"] == {"j1": 1000, "j2": 1000}
    monkeypatch.setattr(digest, "_now", lambda: 2000)
    # re-adding an existing job does not re-timestamp or double-count
    assert digest.add_pending(buf, [_scored(jid="j1"), _scored(jid="j3")]) == 1
    assert buf["jobs"]["j1"] == 1000
    assert buf["jobs"]["j3"] == 2000


def test_is_due_respects_period_and_force(monkeypatch):
    from gigwatch import digest
    monkeypatch.setattr(digest, "_now", lambda: 1000)
    buf = {"last_flush": 0, "jobs": {}}
    assert digest.is_due(buf, 86400) is False  # nothing pending
    buf["jobs"] = {"j1": 1000}
    assert digest.is_due(buf, 86400) is True  # first flush is always due
    buf["last_flush"] = 900
    assert digest.is_due(buf, 86400) is False  # period not elapsed
    assert digest.is_due(buf, 86400, force=True) is True
    monkeypatch.setattr(digest, "_now", lambda: 900 + 86400)
    assert digest.is_due(buf, 86400) is True  # period elapsed


def test_build_digest_skips_unknown_ids():
    from gigwatch import digest
    buf = {"last_flush": 0, "jobs": {"j1": 1, "gone": 2}}
    by_id = {"j1": _scored(jid="j1")}
    out = digest.build_digest(buf, by_id)
    assert [s.job.id for s in out] == ["j1"]


def test_flush_clears_and_records_time(monkeypatch):
    from gigwatch import digest
    monkeypatch.setattr(digest, "_now", lambda: 500)
    buf = {"last_flush": 0, "jobs": {"j1": 1}}
    digest.flush(buf)
    assert buf == {"last_flush": 500, "jobs": {}}


def test_send_digest_empty_returns_no_errors():
    from gigwatch import digest
    from gigwatch.config import AlertConfig
    assert digest.send_digest([], AlertConfig()) == []


def test_parser_accepts_digest():
    from gigwatch.cli import build_parser
    args = build_parser().parse_args(
        ["digest", "--period", "3600", "--buffer", "/tmp/d.json", "--force"])
    assert args.period == 3600
    assert args.buffer == "/tmp/d.json"
    assert args.force is True
    # defaults
    args2 = build_parser().parse_args(["digest"])
    assert args2.period is None
    assert args2.buffer is None
    assert args2.force is False


# ---------- server (self-contained hosted instance) ----------

def _srv_scored(jid="j1", title="Senior Python Backend Engineer",
                company="Acme", url="https://example.com/j/1",
                score=5.0, matched=("python",)):
    return ScoredJob(job=make_job(jid=jid, title=title, company=company, url=url),
                     score=score, matched_keywords=list(matched))


def test_jobs_json_shape():
    from gigwatch.server import jobs_json
    data = json.loads(jobs_json([_srv_scored()]))
    assert len(data) == 1
    assert data[0]["id"] == "j1"
    assert data[0]["score"] == 5.0
    assert data[0]["matched_keywords"] == ["python"]
    assert data[0]["url"] == "https://example.com/j/1"


def test_rss_xml_wellformed():
    import xml.etree.ElementTree as ET
    from gigwatch.server import rss_xml
    xml = rss_xml([_srv_scored(), _srv_scored(jid="j2", title="Rust Dev", url="https://e.com/2")])
    root = ET.fromstring(xml)  # raises if malformed
    assert root.tag == "rss"
    items = root.findall("./channel/item")
    assert len(items) == 2
    assert items[0].findtext("title") == "Senior Python Backend Engineer"
    assert items[0].findtext("guid") == "https://example.com/j/1"
    # description carries the matched keywords
    assert "Matched: python" in items[0].findtext("description")


def test_rss_xml_caps_at_max_feed():
    from gigwatch.server import rss_xml, _MAX_FEED
    xml = rss_xml([_srv_scored(jid="j%d" % i) for i in range(_MAX_FEED + 10)])
    import xml.etree.ElementTree as ET
    items = ET.fromstring(xml).findall("./channel/item")
    assert len(items) == _MAX_FEED


def test_dashboard_html_renders_rows_and_meta():
    from gigwatch.server import dashboard_html
    cache = {"jobs": [_srv_scored()], "errors": ["remotive: boom"],
             "fetched": 42, "last_refresh_str": "2026-09-12 00:00:00 UTC"}
    html = dashboard_html(cache)
    assert "<!doctype html>" in html
    assert "Senior Python Backend Engineer" in html
    assert "source warnings:" in html and "remotive: boom" in html
    assert "42 fetched" in html and "1 match" in html
    # links to the API endpoints are present
    assert "/api/jobs" in html and "/feed" in html and "/health" in html


def test_dashboard_html_empty_state():
    from gigwatch.server import dashboard_html
    html = dashboard_html({"jobs": [], "errors": [], "fetched": 0,
                           "last_refresh_str": "never"})
    assert "No jobs match your filters" in html
    assert "0 match" in html


def test_dashboard_html_escapes_html_in_fields():
    from gigwatch.server import dashboard_html
    evil = _srv_scored(title="<script>alert(1)</script>",
                   company='Acme"<b>')
    html = dashboard_html({"jobs": [evil], "errors": [], "fetched": 1,
                           "last_refresh_str": "x"})
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_dashboard_html_ranks_panel_with_float_score():
    # Regression: RankedJob.score is a float; the dashboard must str() it
    # before html.escape (a raw float crashed the whole page).
    from gigwatch.server import dashboard_html
    from gigwatch.ranking import RankedJob
    job = make_job(jid="j1", title="Senior Backend Developer (Python)",
                   company="Proxify AB", url="https://example.com/j/1")
    ranked = [RankedJob(job=job, score=85.0,
                        rationale="Perfect role match and remote.",
                        method="ai")]
    cache = {"jobs": [_srv_scored()], "errors": [], "fetched": 1,
             "last_refresh_str": "2026-09-12 00:00:00 UTC",
             "ranked": ranked, "rank_method": "ai",
             "rank_profile": {"title": "Senior Python Backend Engineer",
                              "skills": ["python", "fastapi"]}}
    html = dashboard_html(cache)
    assert "ai-ranked" in html
    assert "fit 85.0/100" in html
    assert "Perfect role match and remote." in html
    assert "Senior Python Backend Engineer" in html
    assert "python, fastapi" in html


def test_dashboard_html_rank_off_when_no_profile():
    from gigwatch.server import dashboard_html
    html = dashboard_html({"jobs": [_srv_scored()], "errors": [], "fetched": 1,
                           "last_refresh_str": "x", "ranked": [],
                           "rank_method": "none", "rank_profile": {}})
    assert "rank-off" in html
    assert "--profile" in html
    assert "fit " not in html


def test_run_once_updates_cache(monkeypatch):
    from gigwatch import server
    from gigwatch.config import Config, Filters, SourceConfig, AlertConfig
    cfg = Config(sources=[SourceConfig(type="remotive")],
                 filters=Filters(keywords=["python"]),
                 alerts=AlertConfig(), state_file="/tmp/never.json",
                 poll_interval=900)
    # fake the fetch layer so no network is touched
    monkeypatch.setattr(server, "_fetch_all",
                        lambda c, verbose=False: ([make_job()], []))
    cache = {"jobs": [], "errors": [], "fetched": 0, "last_refresh": 0,
             "last_refresh_str": "never", "by_id": {}}
    server.run_once(cfg, cache)
    assert cache["fetched"] == 1
    assert len(cache["jobs"]) == 1
    assert cache["jobs"][0].job.id == "j1"
    assert cache["last_refresh"] > 0
    assert "never" not in cache["last_refresh_str"]


def test_run_once_records_source_error(monkeypatch):
    from gigwatch import server
    from gigwatch.config import Config, Filters, SourceConfig, AlertConfig
    cfg = Config(sources=[SourceConfig(type="remotive")],
                 filters=Filters(keywords=["python"]),
                 alerts=AlertConfig(), state_file="/tmp/never.json",
                 poll_interval=900)
    monkeypatch.setattr(server, "_fetch_all",
                        lambda c, verbose=False: ([], ["remotive: 404"]))
    cache = {"jobs": [], "errors": [], "fetched": 0, "last_refresh": 0,
             "last_refresh_str": "never", "by_id": {}}
    server.run_once(cfg, cache)
    assert cache["errors"] == ["remotive: 404"]
    assert cache["jobs"] == []


def test_server_endpoints_live(monkeypatch):
    """Boot a real ThreadingHTTPServer on 127.0.0.1 and hit every endpoint."""
    import socket
    import threading
    import urllib.request
    from gigwatch import server
    from gigwatch.config import Config, Filters, SourceConfig, AlertConfig

    cfg = Config(sources=[SourceConfig(type="remotive")],
                 filters=Filters(keywords=["python"]),
                 alerts=AlertConfig(), state_file="/tmp/never.json",
                 poll_interval=900)
    monkeypatch.setattr(server, "_fetch_all",
                        lambda c, verbose=False: ([make_job()], []))

    # find a free port
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    srv = server.Server(cfg, host="127.0.0.1", port=port, refresh=9999)
    # seed the cache with one match without waiting for the refresh thread
    server.run_once(cfg, srv.cache)
    srv._start_http()
    srv._serve()
    srv._thread = threading.Thread(target=srv._refresh_loop, daemon=True)
    srv._thread.start()
    try:
        base = "http://127.0.0.1:%d" % port

        def get(path, headers=None):
            import urllib.error
            req = urllib.request.Request(base + path, headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, r.headers.get("Content-Type", ""), r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.headers.get("Content-Type", ""), e.read().decode()

        st, ct, body = get("/health")
        assert st == 200 and "application/json" in ct
        health = json.loads(body)
        assert health["status"] == "ok"
        assert health["matches"] == 1
        assert health["version"]

        st, ct, body = get("/api/jobs")
        assert st == 200 and "application/json" in ct
        assert len(json.loads(body)) == 1

        st, ct, body = get("/feed")
        assert st == 200 and "application/rss+xml" in ct
        assert "<rss version=\"2.0\">" in body

        st, ct, body = get("/")
        assert st == 200 and "text/html" in ct
        assert "GigWatch" in body

        st, ct, body = get("/nope")
        assert st == 404
    finally:
        srv._httpd.server_close()
        srv._thread.join(timeout=2)


def test_server_token_auth(monkeypatch):
    """With a token set, /api/jobs and /feed require the bearer header."""
    import socket
    import threading
    import urllib.request
    import urllib.error
    from gigwatch import server
    from gigwatch.config import Config, Filters, SourceConfig, AlertConfig

    cfg = Config(sources=[SourceConfig(type="remotive")],
                 filters=Filters(keywords=["python"]),
                 alerts=AlertConfig(), state_file="/tmp/never.json",
                 poll_interval=900)
    monkeypatch.setattr(server, "_fetch_all",
                        lambda c, verbose=False: ([make_job()], []))

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    srv = server.Server(cfg, host="127.0.0.1", port=port, refresh=9999,
                        token="sekret")
    server.run_once(cfg, srv.cache)
    srv._start_http()
    srv._serve()
    srv._thread = threading.Thread(target=srv._refresh_loop, daemon=True)
    srv._thread.start()
    try:
        base = "http://127.0.0.1:%d" % port

        def get(path, headers=None):
            req = urllib.request.Request(base + path, headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()

        # no token -> 401
        st, _ = get("/api/jobs")
        assert st == 401
        st, _ = get("/feed")
        assert st == 401
        # wrong token -> 401
        st, _ = get("/api/jobs", {"Authorization": "Bearer wrong"})
        assert st == 401
        # right token -> 200
        st, body = get("/api/jobs", {"Authorization": "Bearer sekret"})
        assert st == 200 and len(json.loads(body)) == 1
        st, body = get("/feed", {"Authorization": "Bearer sekret"})
        assert st == 200 and "<rss" in body
        # dashboard stays public even with a token set
        st, body = get("/")
        assert st == 200 and "GigWatch" in body
        # health stays public
        st, body = get("/health")
        assert st == 200 and json.loads(body)["status"] == "ok"
    finally:
        srv._httpd.server_close()
        srv._thread.join(timeout=2)


def test_parser_accepts_serve():
    from gigwatch.cli import build_parser
    args = build_parser().parse_args(
        ["serve", "--host", "0.0.0.0", "--port", "9000",
         "--refresh", "300", "--token", "tok"])
    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.refresh == 300
    assert args.token == "tok"
    # defaults
    args2 = build_parser().parse_args(["serve"])
    assert args2.host == "127.0.0.1"
    assert args2.port == 8765
    assert args2.refresh == 900
    assert args2.token is None
