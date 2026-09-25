"""Regression tests for the pre-init CLI behavior.

Running a command that needs a config (list/scan/rank/serve) before
``gigwatch init`` used to die with a raw ``FileNotFoundError`` traceback.
It must now print a friendly hint and exit cleanly with code 1 (no traceback).

The tests run the CLI from the source tree (workspace root) so they are
deterministic and do not depend on which copy is installed in site-packages.
No network is required: all cases fail at config load, before any fetch.
"""

import os
import subprocess
import sys

import pytest

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def run_cli(args, cwd):
    env = dict(os.environ)
    env["PYTHONPATH"] = WORKSPACE  # shadow any installed copy
    return subprocess.run(
        [PY, "-m", "gigwatch"] + args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.mark.parametrize("cmd", ["list", "scan", "rank", "serve"])
def test_missing_config_friendly_exit(tmp_path, cmd):
    """No config.json present -> friendly 'run init' hint, exit 1, no traceback."""
    res = run_cli([cmd], str(tmp_path))
    assert res.returncode == 1
    assert "Traceback (most recent call last)" not in res.stderr
    assert "FileNotFoundError" not in res.stderr
    assert "no config file" in res.stderr
    assert "gigwatch init" in res.stderr
