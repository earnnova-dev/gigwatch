"""Live integration tests: hit real public endpoints.

Deselected by default (see pyproject addopts). Run explicitly with:
    pytest -m live
"""

import urllib.error

import pytest

from gigwatch.sources import fetch_remoteok, fetch_remotive, fetch_wwr
from gigwatch.filtering import Filters, filter_jobs


def _fetch_or_skip(fn, **kwargs):
    """Call a live fetcher, skipping (not failing) on transient upstream errors.

    RemoteOK/WWR occasionally rate-limit or block CI egress IPs; a 403/429/5xx
    or an unreachable host is an environment problem, not a bug in the adapter.
    """
    try:
        return fn(**kwargs)
    except urllib.error.HTTPError as exc:  # noqa: PERF203
        if exc.code in (403, 429) or exc.code >= 500:
            pytest.skip("upstream returned HTTP %s" % exc.code)
        raise
    except urllib.error.URLError as exc:
        pytest.skip("network unavailable: %s" % exc)


@pytest.mark.live
def test_remotive_returns_jobs():
    jobs = fetch_remotive(limit=50)
    assert len(jobs) > 0
    j = jobs[0]
    assert j.title and j.url and j.source == "remotive"
    assert j.id


@pytest.mark.live
def test_remotive_filter_end_to_end():
    """Filter with a broad keyword set that Remotive reliably has.

    The point is that the filter pipeline works end-to-end, not that one
    specific technology is posted today. Role words (developer/engineer/...)
    appear on any non-empty remote board, so we pair them with a few tech
    keywords instead of asserting a hard dependency on one language — a day
    with no Python/React roles posted (live-data drift) must not fail CI.
    """
    jobs = fetch_remotive(limit=200)
    assert len(jobs) > 0, "Remotive returned no jobs at all"
    # Role words + a few tech keywords: robust to which specific roles are
    # posted on any given day.
    f = Filters(keywords=[
        "react", "golang", "python", "java", "node",
        "engineer", "developer", "designer", "manager", "lead",
    ], min_score=1.0)
    out = filter_jobs(jobs, f)
    # A non-empty remote tech board always has at least one role-word title.
    assert len(out) >= 1, (
        f"No matches for common role/tech keywords among {len(jobs)} jobs. "
        f"Titles: {[j.title for j in jobs[:10]]}"
    )
    # Pipeline invariant (deterministic): every result clears the threshold
    # and its title genuinely contains at least one matched keyword.
    for s in out:
        assert s.score >= f.min_score
        assert s.matched_keywords
        for kw in s.matched_keywords:
            assert kw in s.job.title.lower()


@pytest.mark.live
def test_wwr_returns_jobs():
    jobs = _fetch_or_skip(fetch_wwr, limit=50)
    assert len(jobs) > 0
    j = jobs[0]
    assert j.title and j.url and j.source == "wwr"
    assert j.id


@pytest.mark.live
def test_remoteok_returns_jobs():
    jobs = _fetch_or_skip(fetch_remoteok, limit=50)
    assert len(jobs) > 0
    j = jobs[0]
    assert j.title and j.url and j.source == "remoteok"
    assert j.id


@pytest.mark.live
def test_wwr_filter_end_to_end():
    jobs = _fetch_or_skip(fetch_wwr, limit=200)
    assert len(jobs) > 0, "WWR returned no jobs at all"
    f = Filters(keywords=["react", "golang", "python", "java", "node",
                          "engineer", "developer", "manager"], min_score=1.0)
    out = filter_jobs(jobs, f)
    assert len(out) >= 1, (
        f"No matches for common keywords among {len(jobs)} jobs. "
        f"Titles: {[j.title for j in jobs[:10]]}"
    )
