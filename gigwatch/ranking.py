"""Job ranking: turn a filtered list of matches into a ranked shortlist.

Two engines, one interface (:func:`rank_jobs`):

* **AI** (default when ``OPENAI_API_KEY`` is set) — a single batched call to
  an OpenAI-compatible chat endpoint scores every job 0-100 against the user's
  profile and returns a one-line rationale per job. Zero third-party deps:
  it speaks plain HTTPS with :mod:`urllib`, so the package stays dependency-free.
* **Heuristic** (fallback when there is no key, the call fails, or the user
  opts out) — a deterministic, explainable score from keyword coverage,
  salary presence, seniority signals, and remote-ness. Never needs the network.

Both return :class:`RankedJob` so callers (the CLI, the landing-page demo,
client dashboards) don't care which engine produced the numbers.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from gigwatch.filtering import ScoredJob
from gigwatch.sources import Job

USER_AGENT = "GigWatch/0.2 (+https://github.com/earnnova-dev/gigwatch)"

# Words that signal a senior / well-paid role (used by the heuristic engine).
_SENIORITY_WORDS = (
    "senior", "lead", "principal", "staff", "architect", "head of",
    "manager", "founder", "vp", "director",
)
# Words that signal a role is genuinely remote (vs. hybrid / on-site).
_REMOTE_WORDS = ("remote", "anywhere", "worldwide", "global", "wfh", "work from home")


@dataclass
class RankedJob:
    """A job with a 0-100 fit score, a rationale, and which engine produced it."""

    job: Job
    score: float            # 0-100, higher = better fit for the profile
    rationale: str
    method: str             # "ai" | "heuristic"
    matched_keywords: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Heuristic engine
# ---------------------------------------------------------------------------

def _profile_skills(profile: Dict[str, Any]) -> List[str]:
    skills = profile.get("skills") or []
    out: List[str] = []
    for s in skills:
        s = str(s).lower().strip()
        if s:
            out.append(s)
    return out


def _profile_text(profile: Dict[str, Any]) -> str:
    parts = [str(profile.get("title") or ""), str(profile.get("location") or ""),
             str(profile.get("notes") or "")]
    parts += [str(s) for s in (profile.get("skills") or [])]
    return " ".join(parts).lower()


def heuristic_score(job: Job, profile: Dict[str, Any]) -> "RankedJob":
    """Deterministic 0-100 fit score. No network, fully explainable.

    Scoring (capped at 100):
      * +40  for every profile skill that appears in the title (up to 3 skills)
      * +15  if the job is remote-friendly
      * +10  if a salary is listed (transparency = less time wasted)
      * +10  if a seniority word appears (senior roles pay more)
      * +5   if the role title matches the profile's stated role
    """
    hay = " ".join([job.title, job.category, " ".join(job.tags)]).lower()
    skills = _profile_skills(profile)
    pt = _profile_text(profile)

    score = 0.0
    reasons: List[str] = []

    matched = [s for s in skills if s in hay]
    if matched:
        pts = min(len(matched), 3) * 40 / max(len(skills), 1)
        pts = min(pts, 40.0)
        score += pts
        reasons.append("matches %s" % ", ".join(matched[:3]))
    if not skills and pt and job.title.lower() in pt:
        score += 10
        reasons.append("matches your role")

    if any(w in hay for w in _REMOTE_WORDS):
        score += 15
        reasons.append("remote")
    if job.salary:
        score += 10
        reasons.append("salary listed")
    if any(w in hay for w in _SENIORITY_WORDS):
        score += 10
        reasons.append("senior-level")

    score = round(min(score, 100.0), 1)
    rationale = "; ".join(reasons) if reasons else "no strong signal"
    return RankedJob(job=job, score=score, rationale=rationale,
                     method="heuristic", matched_keywords=matched)


def rank_heuristic(jobs: List[ScoredJob], profile: Dict[str, Any]) -> List[RankedJob]:
    out = [heuristic_score(s.job, profile) for s in jobs]
    for r in out:
        r.matched_keywords = list(
            next((s.matched_keywords for s in jobs if s.job.id == r.job.id), [])
        )
    out.sort(key=lambda r: r.score, reverse=True)
    return out


# ---------------------------------------------------------------------------
# AI engine
# ---------------------------------------------------------------------------

def _ai_chat(prompt: str, api_key: str, base_url: str, model: str,
             timeout: int = 60) -> str:
    """One chat-completion call to an OpenAI-compatible endpoint."""
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system",
             "content": "You rank job listings for a candidate. "
                        "Return JSON only."},
            {"role": "user", "content": prompt},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % api_key,
                 "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return data["choices"][0]["message"]["content"]


def _ai_batch(jobs: List[ScoredJob], profile: Dict[str, Any],
              api_key: str, base_url: str, model: str) -> Dict[str, tuple]:
    """Score a batch of jobs in one call. Returns {job_id: (score, rationale)}."""
    lines = []
    for i, s in enumerate(jobs, 1):
        j = s.job
        lines.append(
            "%d. id=%s | title=%s | company=%s | location=%s | salary=%s | tags=%s"
            % (i, j.id, j.title, j.company, j.location, j.salary,
               ",".join(j.tags))
        )
    prof = (
        "Role: %s\nSkills: %s\nLocation pref: %s\nNotes: %s"
        % (profile.get("title") or "n/a",
           ", ".join(_profile_skills(profile)) or "n/a",
           profile.get("location") or "n/a",
           profile.get("notes") or "n/a")
    )
    prompt = (
        "You are a job-matching engine. For each job below, score its fit for "
        "the candidate 0-100 (100 = perfect fit) and give a one-line rationale "
        "under 15 words. Base the score on skill match, seniority, remote-ness, "
        "and salary transparency.\n\n"
        "CANDIDATE PROFILE:\n%s\n\nJOBS:\n%s\n\n"
        "Return JSON: {\"scores\": [{\"id\": <id>, \"score\": <0-100>, "
        "\"rationale\": \"...\"}, ...]} with one entry per job, same order."
        % (prof, "\n".join(lines))
    )
    raw = _ai_chat(prompt, api_key, base_url, model)
    parsed = json.loads(raw)
    out: Dict[str, tuple] = {}
    for entry in parsed.get("scores", []):
        jid = str(entry.get("id"))
        try:
            sc = max(0.0, min(100.0, float(entry.get("score", 0))))
        except (TypeError, ValueError):
            sc = 0.0
        out[jid] = (sc, str(entry.get("rationale", ""))[:200])
    return out


def _discover_model(base_url: str, api_key: str) -> Optional[str]:
    """Pick a usable chat model from an OpenAI-compatible ``/models`` list.

    Prefers fast/cheap models (``gemini-flash-lite`` first) so the default
    works out-of-the-box against most litellm/OpenAI-compatible proxies.
    Returns ``None`` if the endpoint is unreachable or lists no models.
    """
    try:
        req = urllib.request.Request(
            base_url.rstrip("/") + "/models",
            headers={"Authorization": "Bearer %s" % api_key,
                     "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
    except Exception:  # noqa: BLE001 - discovery is best-effort
        return None
    if not ids:
        return None
    for preferred in ("gemini-flash-lite", "gpt-4o-mini", "gpt-4o",
                      "gpt-3.5-turbo"):
        if preferred in ids:
            return preferred
    return ids[0]


def rank_ai(jobs: List[ScoredJob], profile: Dict[str, Any],
            api_key: Optional[str] = None,
            base_url: Optional[str] = None,
            model: Optional[str] = None,
            batch_size: int = 25) -> List[RankedJob]:
    """Score jobs with an LLM. Falls back to the heuristic engine per-batch
    if a call fails, so one bad request never loses the whole ranking."""
    api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
    base_url = base_url or os.environ.get("OPENAI_BASE_URL",
                                          "https://api.openai.com/v1")
    if not api_key:
        return rank_heuristic(jobs, profile)
    # Model resolution: explicit arg > GIGWATCH_AI_MODEL env > auto-discover
    # > gpt-4o-mini (works against the real OpenAI API).
    model = model or os.environ.get("GIGWATCH_AI_MODEL") \
        or _discover_model(base_url, api_key) or "gpt-4o-mini"
    out: List[RankedJob] = []
    for start in range(0, len(jobs), batch_size):
        batch = jobs[start:start + batch_size]
        try:
            scores = _ai_batch(batch, profile, api_key, base_url, model)
        except Exception:  # noqa: BLE001 - fall back per batch, keep going
            out.extend(rank_heuristic(batch, profile))
            continue
        for s in batch:
            if s.job.id in scores:
                sc, rat = scores[s.job.id]
                out.append(RankedJob(job=s.job, score=sc, rationale=rat,
                                     method="ai",
                                     matched_keywords=list(s.matched_keywords)))
            else:
                out.append(heuristic_score(s.job, profile))
    out.sort(key=lambda r: r.score, reverse=True)
    return out


def rank_jobs(jobs: List[ScoredJob], profile: Dict[str, Any],
              use_ai: bool = True,
              api_key: Optional[str] = None,
              base_url: Optional[str] = None,
              model: Optional[str] = None) -> List[RankedJob]:
    """Rank *jobs* for *profile*.

    *profile* is a dict with optional keys: ``title`` (desired role),
    ``skills`` (list of skill strings), ``location`` (preferred location),
    ``notes`` (free-text, e.g. "prefers senior roles, $150k+").

    Uses the AI engine when ``use_ai`` and a key are available; otherwise the
    deterministic heuristic engine.
    """
    if use_ai:
        return rank_ai(jobs, profile, api_key=api_key, base_url=base_url,
                       model=model)
    return rank_heuristic(jobs, profile)
