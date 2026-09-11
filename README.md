# GigWatch

**A self-hosted freelance-gig watcher.** GigWatch monitors live job/gig feeds,
filters them against *your* skills, remembers what it has already shown you,
and alerts you (console, email, or Slack) **only when a new matching gig
appears**.

Stop refreshing Upwork/Remotive/LinkedIn every 20 minutes. Point GigWatch at
the feeds you care about, tell it what you do, and let it ping you when
something relevant lands.

```
$ gigwatch scan
scanned 17 job(s) from 1 source(s); 4 match filter; 2 new
GigWatch: 2 new matching gig(s)

1. Senior Python Backend Engineer
   company: Acme Digital
   salary:  $120k-$150k
   where:   Remote (Worldwide)
   matched: python, backend
   score:   9.0
   https://remotive.com/remote-jobs/...
```

## Why this exists

Freelancers lose real money to *latency* — the best gigs get filled in the
first hours. Existing monitors (Distill, PageCrawl, Apify's Upwork monitor)
are hosted SaaS that scrape your sessions and cost monthly. GigWatch is:

- **Self-hosted & private** — your skills, your feeds, your machine. No
  account, no session scraping, no data leaving your box.
- **Free & open (MIT)** — the core is a small, readable Python CLI.
- **Portable** — run it on a laptop, a $5 VPS, or in a cron job.

## Features

- **Multiple feed sources** — Remotive, We Work Remotely, RemoteOK, and
  Hacker News "Who is Hiring?" (built-in, no auth), any RSS/Atom feed, or any
  JSON endpoint returning a list of job objects.
- **AI job ranking** — `gigwatch rank` scores every match 0-100 against a
  profile you write (role, skills, location, notes) and explains *why*. Uses
  an LLM when `OPENAI_API_KEY` is set (any OpenAI-compatible endpoint);
  otherwise a deterministic, dependency-free heuristic. Either way you get a
  ranked shortlist, not a raw dump.
- **Output formats** — `scan`, `list`, and `rank` take `--format
  text|markdown|json` (a Markdown table or a JSON array of job objects for
  piping elsewhere).
- **Skill-based filtering** — keyword matching (any/all), category and
  location filters, exclude-list, and a relevance score (title hits weigh
  more than body hits).
- **Dedupe by state** — a local JSON state file tracks what you've already
  seen, so you're only ever alerted on *new* matches. State auto-prunes.
- **Alerts** — console (default), SMTP email, or Slack (webhook or chat API).
  Secrets come from environment variables, never the config file.
- **Zero dependencies** — pure Python standard library. If you can run
  `python3`, you can run GigWatch.

## Quick start

No install required — it's stdlib-only:

```bash
# 1. Get the code
git clone https://github.com/earnnova-dev/gigwatch
cd gigwatch

# 2. Create a starter config (or copy config.example.json to config.json)
python3 -m gigwatch init

# 3. Edit config.json: put YOUR skills in filters.keywords
#    e.g. ["python", "backend", "api", "django"]

# 4. See what would match right now (dry run, no state touched)
python3 -m gigwatch list

# 5. Do a real scan: alerts on new matches, remembers them
python3 -m gigwatch scan
```

Or install it as a proper command:

```bash
pip install .          # or: pipx install .
gigwatch init && gigwatch scan
```

### Run it continuously

```bash
# Loop forever, re-scanning every poll_interval seconds (default 15 min):
gigwatch watch

# Or use cron on a VPS (once an hour):
0 * * * * cd /opt/gigwatch && /usr/bin/python3 -m gigwatch scan
```

### Rank matches by fit

`rank` fetches and filters like `scan`, then scores every match 0-100 against
a profile and explains the score. It uses an LLM when `OPENAI_API_KEY` is set
(any OpenAI-compatible endpoint; the model is auto-discovered), otherwise a
deterministic heuristic — so it works with no key at all.

```bash
# Use the profile from config.json:
gigwatch rank

# Or pass a profile on the command line:
gigwatch rank --title "Senior Python Engineer" \
              --skills "python,backend,api" \
              --location remote --notes "senior, \$150k+"

# Force the offline heuristic (no LLM call):
gigwatch rank --no-ai --format markdown
```

### Alerts

Console is on by default. To also get email/Slack, fill in the `alerts`
section of `config.json` and set the env vars it references:

```bash
export GIGWATCH_EMAIL_TO=you@yourdomain.com
export GIGWATCH_SMTP_USER=...
export GIGWATCH_SMTP_PASS=...
export GIGWATCH_SLACK_WEBHOOK=https://hooks.slack.com/services/...
```

The `${VAR}` placeholders in the config are expanded from the environment at
load time, so secrets never live in the file (and the file is safe to commit
if you want).

## Configuration

See [`config.example.json`](config.example.json) for a fully annotated
example. The top-level keys:

| Key | Meaning |
|-----|---------|
| `sources` | List of feeds to watch. `{"type":"remotive"}`, `{"type":"wwr"}`, `{"type":"remoteok"}`, `{"type":"hn"}`, `{"type":"rss","url":...}`, or `{"type":"json","url":...}`. |
| `profile` | Optional candidate profile for `rank`: `{"title","skills":[...],"location","notes"}`. |
| `filters.keywords` | Your skills. A job matches if it contains any of these (or all, with `require_all_keywords`). |
| `filters.categories` / `filters.locations` | Optional extra filters (empty = match anything). |
| `filters.exclude_keywords` | Words that disqualify a job (e.g. `"intern"`, `"junior"`). |
| `filters.min_score` | Relevance floor. Title hits = 3 pts, body hits = 1 pt per keyword. |
| `alerts` | `console`, `email`, `slack` (see above) and `max_per_alert`. |
| `state_file` | Where seen-jobs are remembered (default `gigwatch-state.json`). |
| `poll_interval` | Seconds between scans in `watch` mode (default 900). |

### Adding your own JSON source

Any endpoint that returns a JSON array of objects works. Each object should
have at least `title` and `url`; `company`/`company_name`, `category`,
`location`/`country`, `salary`, `tags`, and `description` are picked up if
present. Wrap in `{"jobs":[...]}`, `{"data":[...]}`, `{"results":[...]}`, or
`{"items":[...]}` and it still works.

## Commands

| Command | What it does |
|---------|-------------|
| `gigwatch init` | Write a starter `config.json`. |
| `gigwatch list` | **Dry run** — fetch + filter + print matches. Does not touch state. |
| `gigwatch scan` | Fetch, filter, alert on new matches, and record them as seen. |
| `gigwatch rank` | Fetch + filter, then rank matches 0-100 for your `profile` (AI or heuristic). |
| `gigwatch watch` | Loop `scan` every `poll_interval` seconds. |
| `gigwatch reset` | Clear the seen-state (next scan alerts on everything that matches). |

Useful flags: `--config PATH` (default `config.json`), `-v/--verbose`,
`--max-age-days N` (state pruning; `0` keeps everything), and
`--format text|markdown|json` on `scan`/`list`/`rank`. For `rank`, also
`--profile FILE` (a JSON profile), `--skills a,b,c`, `--title`, `--location`,
`--notes`, and `--no-ai` (force the deterministic heuristic engine).

## How it works

```
sources ──fetch──> [Job, Job, ...]
                        │
                   filter_jobs()  (keywords / category / location / score)
                        │
                   [ScoredJob, ...]
                        │
              compare against state file
                        │
                 ┌──────┴──────┐
              new jobs       already seen
                 │               │
           alert (console/     drop silently
           email/slack)
                 │
         mark seen in state file  ──>  gigwatch-state.json
```

The state file is plain JSON (`{job_id: first_seen_utc}`) so you can inspect
it, back it up, or move it between machines.

## Roadmap / ideas

- ~~More built-in sources (Hacker News "Who is hiring")~~ — **done in 0.2.0**.
- ~~AI ranking: summarize each match and rank by fit to a profile~~ — **done in
  0.2.0** (`gigwatch rank`).
- LinkedIn via RSS, Upwork via a user-supplied export.
- Draft proposals / cover letters per match.
- A tiny hosted tier (the natural monetization path — see below).

## Monetization

The core is free and MIT-licensed. The obvious paid tier is a **hosted
GigWatch**: we run the scanner on our infra, you get email/Slack alerts
without running anything, plus AI-ranked matches and proposal drafts. That's
the plan — the open-source CLI is both the product's engine and the
marketing.

## License

MIT — see [LICENSE](LICENSE).

## Contact

For questions, hosting-tier interest, or support, open an
[issue](https://github.com/earnnova-dev/gigwatch/issues/new) — that's the
fastest route. Email works too: <a href="mailto:novagw&#64;uberip&#46;com">novagw[at]uberip[dot]com</a>.

## Contributing

Issues and PRs welcome. The code is deliberately small and stdlib-only;
keep it that way. Run `pytest` (unit) and `pytest -m live` (hits the real
Remotive API) before sending a PR.
