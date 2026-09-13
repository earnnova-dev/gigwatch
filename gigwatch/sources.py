"""Job/gig feed sources.

Each source fetches a list of normalized :class:`Job` objects. Sources are
pure functions of the network + config, so they are easy to test and extend.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

USER_AGENT = "GigWatch/0.1 (+https://github.com/earnnova7/gigwatch)"


@dataclass
class Job:
    """A normalized job/gig listing."""

    id: str
    title: str
    company: str
    url: str
    category: str = ""
    location: str = ""
    salary: str = ""
    tags: List[str] = field(default_factory=list)
    published: str = ""
    source: str = ""
    description: str = ""

    def haystack(self) -> str:
        """Lowercased text used for keyword matching."""
        return " ".join(
            [self.title, self.company, self.category, self.location,
             self.salary, " ".join(self.tags), self.description]
        ).lower()


def _http_get(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _clean(text: str) -> str:
    """Strip tags and collapse whitespace from a (possibly HTML) string."""
    if not text:
        return ""
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    import html
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_remotive(limit: Optional[int] = None) -> List[Job]:
    """Fetch live remote jobs from Remotive's public API (no auth required)."""
    url = "https://remotive.com/api/remote-jobs"
    data = json.loads(_http_get(url).decode("utf-8"))
    jobs: List[Job] = []
    for raw in data.get("jobs", []):
        jobs.append(
            Job(
                id=str(raw.get("id")),
                title=_clean(raw.get("title", "")),
                company=_clean(raw.get("company_name", "")),
                url=raw.get("url", ""),
                category=_clean(raw.get("category", "")),
                location=_clean(raw.get("candidate_required_location", "")),
                salary=_clean(raw.get("salary", "")),
                tags=[_clean(t) for t in raw.get("tags", []) if t],
                published=raw.get("publication_date", ""),
                source="remotive",
                description=_clean(raw.get("description", "")),
            )
        )
    if limit:
        jobs = jobs[:limit]
    return jobs


def fetch_rss(url: str, limit: Optional[int] = None) -> List[Job]:
    """Fetch jobs from an RSS/Atom feed (works for many job boards)."""
    root = ET.fromstring(_http_get(url).decode("utf-8", "replace"))
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    jobs: List[Job] = []

    # RSS 2.0: <channel><item>
    items = root.findall(".//item")
    for it in items:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc = _clean(it.findtext("description") or "")
        pub = (it.findtext("pubDate") or "").strip()
        jobs.append(_rss_job(title, link, desc, pub, source="rss"))

    # Atom: <entry>
    if not items:
        entries = root.findall("atom:entry", ns)
        for it in entries:
            title = (it.findtext("atom:title", namespaces=ns) or "").strip()
            link_el = it.find("atom:link[@rel='alternate']", ns)
            if link_el is None:
                link_el = it.find("atom:link", ns)
            link = (link_el.get("href") if link_el is not None else "") or ""
            desc = _clean(it.findtext("atom:summary", namespaces=ns) or "")
            pub = (it.findtext("atom:updated", namespaces=ns) or "").strip()
            jobs.append(_rss_job(title, link, desc, pub, source="rss"))

    if limit:
        jobs = jobs[:limit]
    return [j for j in jobs if j.id]


def _rss_job(title: str, link: str, desc: str, pub: str, source: str) -> Job:
    import hashlib
    jid = hashlib.sha1((link or title).encode("utf-8")).hexdigest()[:16]
    return Job(
        id=jid,
        title=title,
        company="",
        url=link,
        description=desc,
        published=pub,
        source=source,
    )


def fetch_json(url: str, limit: Optional[int] = None) -> List[Job]:
    """Fetch jobs from a JSON endpoint returning a list of objects.

    Expects each object to have at least `title` and `url`. Other common keys
    (company/company_name, category, location, salary, tags, publication_date/
    published) are mapped if present.
    """
    data = json.loads(_http_get(url).decode("utf-8", "replace"))
    if isinstance(data, dict):
        # tolerate {"jobs": [...]} / {"data": [...]}
        for key in ("jobs", "data", "results", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise ValueError("json source must return a list of job objects")

    jobs: List[Job] = []
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or raw.get("name") or "").strip()
        url = str(raw.get("url") or raw.get("link") or "").strip()
        if not title and not url:
            continue
        jobs.append(
            Job(
                id=str(raw.get("id") or hashlib_sha1(url or title)),
                title=title,
                company=str(raw.get("company") or raw.get("company_name") or ""),
                url=url,
                category=str(raw.get("category") or ""),
                location=str(raw.get("location") or raw.get("country") or ""),
                salary=str(raw.get("salary") or ""),
                tags=[str(t) for t in (raw.get("tags") or [])],
                published=str(raw.get("publication_date") or raw.get("published") or ""),
                source="json",
                description=_clean(str(raw.get("description") or "")),
            )
        )
    if limit:
        jobs = jobs[:limit]
    return jobs


def _hashlib_sha1(s: str) -> str:
    import hashlib
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _rss_items(url: str) -> List[ET.Element]:
    """Fetch *url* and return its RSS 2.0 ``<item>`` elements."""
    root = ET.fromstring(_http_get(url).decode("utf-8", "replace"))
    return root.findall(".//item")


def fetch_wwr(limit: Optional[int] = None) -> List[Job]:
    """Fetch remote jobs from the We Work Remotely RSS feed.

    WWR titles are ``"Company: Job Title"`` and each item carries ``<region>``
    and ``<category>`` elements, so company/location come through cleanly.
    """
    url = "https://weworkremotely.com/remote-jobs.rss"
    jobs: List[Job] = []
    for it in _rss_items(url):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        company = ""
        if ": " in title:
            company, title = (p.strip() for p in title.split(": ", 1))
        jobs.append(
            Job(
                id=_hashlib_sha1(link or title),
                title=title,
                company=company,
                url=link,
                category=_clean(it.findtext("category") or ""),
                location=_clean(it.findtext("region") or ""),
                published=(it.findtext("pubDate") or "").strip(),
                source="wwr",
                description=_clean(it.findtext("description") or ""),
            )
        )
    if limit:
        jobs = jobs[:limit]
    return [j for j in jobs if j.id and j.title]


def _remoteok_salary(raw: Dict[str, Any]) -> str:
    lo, hi = raw.get("salary_min"), raw.get("salary_max")
    if lo and hi:
        return "$%s-$%s" % (f"{int(lo):,}", f"{int(hi):,}")
    if lo or hi:
        return "$%s" % f"{int(lo or hi):,}"
    return ""


def fetch_remoteok(limit: Optional[int] = None) -> List[Job]:
    """Fetch remote jobs from RemoteOK's public JSON API (no auth required).

    RemoteOK retired its RSS feed; the ``/api`` endpoint returns a JSON array
    whose first element is a licence/metadata blob (skipped here) followed by
    one object per job.
    """
    url = "https://remoteok.com/api"
    data = json.loads(_http_get(url).decode("utf-8", "replace"))
    jobs: List[Job] = []
    for raw in data:
        if not isinstance(raw, dict) or not raw.get("position"):
            continue  # skips the leading {"legal": ...} metadata entry
        jobs.append(
            Job(
                id=str(raw.get("id") or _hashlib_sha1(raw.get("url", ""))),
                title=_clean(raw.get("position", "")),
                company=_clean(raw.get("company", "")),
                url=raw.get("url", ""),
                location=_clean(raw.get("location", "")),
                salary=_remoteok_salary(raw),
                tags=[_clean(t) for t in raw.get("tags", []) if t],
                published=raw.get("date", ""),
                source="remoteok",
                description=_clean(raw.get("description", "")),
            )
        )
    if limit:
        jobs = jobs[:limit]
    return [j for j in jobs if j.id and j.title]


def _hn_algolia(params: Dict[str, Any]) -> Dict[str, Any]:
    """Run a query against the public HN Algolia search API (no auth)."""
    qs = urllib.parse.urlencode(params)
    url = "https://hn.algolia.com/api/v1/search?%s" % qs
    return json.loads(_http_get(url).decode("utf-8", "replace"))


def _hn_line_to_job(line: str):
    """Parse one HN Who-is-Hiring line into ``(title, company)`` or ``None``.

    Two common shapes:

    * ``Company | Title | Location | Type | Link``  (pipe-separated, dominant)
    * ``Company — Title`` / ``Company: Title`` / ``Company - Title``

    The company must start with a capital letter and must not be a URL
    scheme.  Only the first two fields are kept (title, company).  Lines
    that don't match are ignored.
    """
    import re
    s = line.strip().lstrip("-*•\t ").strip()
    if not s:
        return None

    def _ok(company, title):
        company = company.strip()
        title = title.strip()
        if not title or len(title) < 3:
            return None
        if not company or company.lower() in ("i", "we", "a", "the"):
            return None
        if company.lower() in ("http", "https", "mailto", "ftp"):
            return None
        # A company name never contains an em/en-dash or a colon — if it
        # does, we mis-split (e.g. "Acme — Senior Engineer" as a company).
        if "—" in company or "–" in company or ":" in company:
            return None
        if not re.search(r"[A-Za-z]", title):
            return None
        return (title, company)

    # 1) "Company — Title" / "Company: Title" / "Company - Title"
    #    (title may be followed by "| Location | Type | Link" fields).
    m = re.match(r"^([A-Z][A-Za-z0-9&.'\-]{0,60}?)\s*(?:—|–|:|-)\s+(.{3,160})$", s)
    if m:
        got = _ok(m.group(1), m.group(2).split("|")[0].strip())
        if got:
            return got
    # 2) pure pipe-separated: Company | Title | ...
    if "|" in s:
        parts = [p.strip() for p in s.split("|")]
        if len(parts) >= 2:
            got = _ok(parts[0], parts[1])
            if got:
                return got
    return None


def _hn_strip_html(text: str) -> str:
    """Remove HTML tags and unescape entities from an HN comment body."""
    import html
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return text


def _hn_parse_jobs(text: str) -> List[tuple]:
    """Extract ``(title, company)`` pairs from a Who-is-Hiring comment.

    The thread format is one job per line, typically::

        <Company> | <Title> | <Location> | <Type> | <Link>

    or ``<Company> — <Title>``.  We keep the first two fields of each
    line.  Lines that are just a header, an "I'm hiring" note, or a bare
    URL are skipped.
    """
    text = _hn_strip_html(text)
    jobs: List[tuple] = []
    for raw in text.splitlines():
        parsed = _hn_line_to_job(raw)
        if parsed:
            jobs.append(parsed)
    return jobs


def _hn_get_json(url: str) -> Dict[str, Any]:
    """Fetch a JSON document from the HN Algolia API."""
    return json.loads(_http_get(url).decode("utf-8", "replace"))


def fetch_hn(limit: Optional[int] = None) -> List[Job]:
    """Fetch jobs from Hacker News "Who is Hiring?" threads.

    Finds the most recent "Ask HN: Who is hiring?" megathread (via the
    public Algolia search API — these run roughly monthly, first Monday),
    then loads the full item tree via the ``items/{id}`` endpoint and
    parses each top-level comment for individual job listings.  Each
    listing becomes a :class:`Job` whose ``url`` deep-links to that
    comment on the thread.
    """
    import time
    now = int(time.time())
    # The megathread is monthly; a 35-day window reliably covers the last one.
    window = now - 35 * 86400
    res = _hn_get_json(
        "https://hn.algolia.com/api/v1/search?%s" % urllib.parse.urlencode({
            "query": "who is hiring",
            "tags": "story",
            "numericFilters": "created_at_i>=%d" % window,
            "hitsPerPage": 20,
        }))
    # Keep only the genuine megathread(s): "Ask HN: Who is hiring?" —
    # exclude "analysis of …", "who wants to be hired", "show hn" tools, etc.
    stories = []
    for h in res.get("hits", []):
        t = (h.get("title") or "").lower()
        if "who is hiring" not in t or "analysis" in t:
            continue
        if "who wants to be hired" in t:
            continue
        if "show hn" in t:
            continue
        stories.append(h)
    if not stories:
        return []

    jobs: List[Job] = []
    for story in stories:
        sid = story.get("objectID")
        item = _hn_get_json("https://hn.algolia.com/api/v1/items/%s" % sid)
        for ch in item.get("children", [])[:100]:
            text = ch.get("text") or ""
            if not text:
                continue
            cid = ch.get("id")
            base_url = "https://news.ycombinator.com/item?id=%s" % cid
            desc = _hn_strip_html(text)[:400]
            for title, company in _hn_parse_jobs(text):
                jobs.append(Job(
                    id=_hashlib_sha1("%s|%s" % (cid, title)),
                    title=title,
                    company=company,
                    url=base_url,
                    source="hn",
                    description=desc,
                ))
    if limit:
        jobs = jobs[:limit]
    return [j for j in jobs if j.id and j.title]


def _jobicy_salary(job: Dict[str, Any]) -> str:
    """Extract salary from Jobicy's ``salary`` field (e.g. "$80k-$120k")."""
    s = job.get("salary") or ""
    if not s:
        return ""
    if s.strip().upper() in ("N/A", "NA"):
        return ""
    return str(s).strip()


def fetch_jobicy(limit: Optional[int] = None) -> List[Job]:
    """Fetch remote jobs from Jobicy's public JSON API (no auth required).

    Jobicy aggregates remote jobs from many boards and exposes a free
    ``/api/v2/remote-jobs`` endpoint.  Each job carries title, company,
    industry, type, geo, level, and a link back to the original posting.
    """
    url = "https://jobicy.com/api/v2/remote-jobs?count=%d" % (limit or 50)
    data = json.loads(_http_get(url).decode("utf-8", "replace"))
    jobs: List[Job] = []
    for raw in data.get("jobs", []):
        if not isinstance(raw, dict):
            continue
        title = _clean(raw.get("jobTitle") or "")
        if not title:
            continue
        industry = raw.get("jobIndustry") or []
        jtype = raw.get("jobType") or []
        tags = []
        if isinstance(industry, list):
            tags.extend(str(t) for t in industry)
        if isinstance(jtype, list):
            tags.extend(str(t) for t in jtype)
        geo = raw.get("jobGeo") or ""
        level = raw.get("jobLevel") or ""
        loc_parts = [p for p in (geo, level) if p]
        jobs.append(
            Job(
                id=str(raw.get("id") or _hashlib_sha1(raw.get("url", ""))),
                title=title,
                company=_clean(raw.get("companyName") or ""),
                url=raw.get("url", ""),
                location=" / ".join(loc_parts),
                salary=_jobicy_salary(raw),
                tags=tags,
                published=raw.get("lastUpdate") or "",
                source="jobicy",
                description=_clean((raw.get("jobExcerpt") or "")[:300]),
            )
        )
    if limit:
        jobs = jobs[:limit]
    return [j for j in jobs if j.id and j.title]


def fetch(source) -> List[Job]:
    """Dispatch to the right fetcher for a :class:`SourceConfig`."""
    if source.type == "remotive":
        return fetch_remotive(source.limit)
    if source.type == "wwr":
        return fetch_wwr(source.limit)
    if source.type == "remoteok":
        return fetch_remoteok(source.limit)
    if source.type == "hn":
        return fetch_hn(source.limit)
    if source.type == "jobicy":
        return fetch_jobicy(source.limit)
    if source.type == "rss":
        return fetch_rss(source.url, source.limit)
    if source.type == "json":
        return fetch_json(source.url, source.limit)
    raise ValueError("unknown source type: %r" % source.type)
