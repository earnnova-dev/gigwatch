"""Regression test: the runtime version must never drift from pyproject.toml.

Historically the package shipped with a hardcoded ``__version__`` that lagged
the published artifact (e.g. the 0.6.2 wheel reported ``0.6.1``), so a new
PyPI installer saw a version that did not match what they installed. The
runtime version is now derived from installed package metadata (single source
of truth = pyproject.toml), with a source-tree fallback. This test asserts the
invariant that the version the CLI reports always equals the version declared
in pyproject.toml, in both installed and source-checkout modes.

``tomllib`` is stdlib only on Python 3.11+, so on older interpreters (the CI
matrix also runs 3.9) we fall back to a minimal parser for the single
``version = "X.Y.Z"`` line in pyproject.toml. No runtime dependency is added.
"""

import os
import re
import subprocess
import sys

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

# stdlib on 3.11+; absent on 3.9-3.10 -> use the minimal fallback parser below.
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None


def _pyproject_version():
    """Return the ``project.version`` declared in pyproject.toml.

    Uses ``tomllib`` when available (Python 3.11+); otherwise parses the
    top-level ``version = "X.Y.Z"`` line directly so the drift guard still
    works on the older CI interpreter.
    """
    if tomllib is not None:
        with open(os.path.join(WORKSPACE, "pyproject.toml"), "rb") as f:
            return tomllib.load(f)["project"]["version"]

    # Minimal fallback: find the first top-level ``version = "..."`` line.
    with open(os.path.join(WORKSPACE, "pyproject.toml"), "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r'^version\s*=\s*["\']([^"\']+)["\']', line)
            if m:
                return m.group(1)
    raise AssertionError("could not parse version from pyproject.toml")


def test_cli_version_matches_pyproject():
    """`gigwatch --version` must report exactly the pyproject.toml version."""
    env = dict(os.environ)
    env["PYTHONPATH"] = WORKSPACE  # source-tree mode (fallback path)
    res = subprocess.run(
        [PY, "-m", "gigwatch", "--version"],
        cwd=WORKSPACE,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    reported = res.stdout.strip().split()[-1]  # "gigwatch X.Y.Z" -> X.Y.Z
    assert reported == _pyproject_version(), (
        f"CLI reports {reported!r} but pyproject declares "
        f"{_pyproject_version()!r} (version drift)"
    )


def test_runtime_version_matches_pyproject():
    """imported __version__ must equal the pyproject.toml version."""
    env = dict(os.environ)
    env["PYTHONPATH"] = WORKSPACE
    code = (
        "import gigwatch; print(gigwatch.__version__)"
    )
    res = subprocess.run(
        [PY, "-c", code],
        cwd=WORKSPACE,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == _pyproject_version()
