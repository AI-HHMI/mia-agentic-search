# mia-agentic-search

An agent-maintained catalog of **usable microscopy training datasets**.
Claude scheduled routines search the web and dataset repositories around the clock. They write
schema-validated YAML records and propose them as pull requests. You review the PRs, and merging
one adds its records to the catalog.

```
Routines (hourly / 3-hourly)       this repo = the database              monitoring
harvest-repositories ─┐            datasets/<repo>/<id>.yaml             GitHub Pages dashboard
harvest-literature  ──┼─ PR ──▶    schema/dataset.schema.json   ──CI──▶  watchdog → Slack
harvest-websearch  ───┘            state/frontier/, state/runs/          routine transcripts
maintainer (daily) ── link checks, re-verification, Slack digest
```

## Layout
| Path | What it holds |
|---|---|
| `schema/dataset.schema.json` | **The record contract.** Every file in `datasets/` must pass it |
| `datasets/<repository>/<id>.yaml` | One record per dataset (see the three examples) |
| `CLAUDE.md` | Agent rules and the definition of a "usable" dataset |
| `.claude/skills/find-datasets/` | Harvest procedure (`/find-datasets source=...`) |
| `.claude/skills/maintain-catalog/` | Daily upkeep and digest (`/maintain-catalog`) |
| `tools/` | Validator, dedup, record skeleton, run log, frontier, repository API clients, dashboard, watchdog |
| `state/frontier/<routine>.yaml` | Rotating search queue for each routine |
| `state/runs/*.json` | One structured log per agent run (feeds the dashboard and watchdog) |
| `state/rejected.yaml` | Keys of rejected datasets; agents never propose these again |

## Review workflow
- Each harvest routine keeps **one** open PR, `claude/harvest-<source>` labeled `new-datasets`,
  and adds to it on every run. The PR body lists every pending record.
- **Merge** to accept all records in the PR.
- To **reject a single record**, delete its file in the PR and add its `id` to `state/rejected.yaml`.
- **Close** the PR without merging to reject every record in it. The `record-rejections`
  workflow then adds their keys to `state/rejected.yaml` automatically.

## Setup
1. **Push this repo to GitHub.** Enable Pages (Settings → Pages → Source: GitHub Actions) and create
   the labels `new-datasets` and `maintenance`.
2. **Slack:** create an incoming webhook for your channel and store it as the repo secret
   `SLACK_WEBHOOK_URL`. It's used by the watchdog and CI alerts.
3. **Routines** (at claude.ai/code/routines, or `/schedule` in Claude Code): create four, all on
   this repo with the GitHub app installed. Give the cloud environment the setup script
   `pip install -r requirements.txt`, and attach the **Slack connector**.

   | Routine name | Schedule | Prompt |
   |---|---|---|
   | harvest-repositories | hourly | `/find-datasets source=repositories` |
   | harvest-literature | every 3 h | `/find-datasets source=literature` |
   | harvest-websearch | every 3 h | `/find-datasets source=websearch` |
   | maintainer | daily 08:00 | `/maintain-catalog` |

   Routine names must match the keys in `CADENCE_H` in `tools/watchdog.py` and `tools/site_template.html`.
4. Use **Run now** on each routine once, then check that a PR appears and the dashboard updates.

## Local use
```bash
pip install -r requirements.txt
python tools/validate.py                         # validate the catalog
python -m tools.sources empiar "FIB-SEM" --limit 5
python tools/build_site.py && open site/index.html
claude -p "/find-datasets source=repositories limit=2 dry-run"   # local agent run, no git/PR
```

## Monitoring
- **Dashboard** (GitHub Pages, rebuilt hourly and on every merge): KPIs, per-routine health,
  weekly additions, modality and annotation breakdowns, recent runs, and a searchable catalog.
  It also reads run logs from unmerged `claude/*` branches.
- **Slack:**
  - Harvest runs post on failures and notable finds.
  - The maintainer posts a daily digest.
  - The `watchdog` workflow alerts when a routine goes stale (no run in 2× its cadence) or fails 3 times in a row.
  - CI posts when validation fails.
- **Traces:** the full transcript of every routine run is at claude.ai/code/routines.
