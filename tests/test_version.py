"""Regression test: the runtime version must never drift from pyproject.toml.

Historically the package shipped with a hardcoded ``__version__`` that lagged
the published artifact (e.g. the 0.6.2 wheel reported ``0.6.1``), so a new
PyPI installer saw a version that did not match what they installed. The
runtime version is now derived from installed package metadata (single source
of truth = pyproject.toml), with a source-tree fallback. This test asserts the
invariant that the version the CLI reports always equals the version declared
in pyproject.toml, in both installed and source-checkout modes.
"""

import os
import subprocess
import sys
import tomllib

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def _pyproject_version():
    with open(os.path.join(WORKSPACE, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)["project"]["version"]


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
