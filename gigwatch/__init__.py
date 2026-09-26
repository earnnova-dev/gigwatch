"""GigWatch: a self-hosted freelance-gig watcher.

Pulls live job/gig feeds, filters them against your skills, dedupes against a
local state file, and alerts you (console / email / Slack) only when a new
matching gig appears.
"""

# Keep in sync with pyproject.toml [project] version. tests/test_version.py
# asserts the two never drift (a prior release shipped a wheel whose reported
# version lagged its artifact, so this is a hard regression guard).
__version__ = "0.6.3"

