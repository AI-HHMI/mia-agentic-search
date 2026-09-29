---
name: maintain-catalog
description: Daily upkeep of the microscopy dataset catalog. Checks links on the oldest records, re-verifies facts, flags stale review PRs and posts the daily Slack digest. Optional arg is dry-run.
---

# maintain-catalog

Arguments: `$ARGUMENTS`. `ROUTINE=maintainer`, `BRANCH=claude/maintainer`.
Read `CLAUDE.md` first.

## 0. Setup
```bash
pip install -q -r requirements.txt
git fetch origin && git checkout -B "$BRANCH" origin/main   # skip in dry-run
python tools/run_log.py start --routine maintainer          # add --dry-run in dry-run
```

## 1. Link checks
Run `python tools/check_links.py --oldest 40 --write`.
For each broken record, open its landing page with WebFetch:
- The page moved and you find the new location: update `landing_url` / `download_url`, append the
  new page to `evidence_urls` (log it with `run_log.py fetched`), set `url_ok: true`, and add a line to `notes`.
- It's still broken: leave `url_ok: false` and log `event error --id <id> --reason "link broken: <url>"`.

## 2. Re-verify weak records
Pick up to 5 records where `license_found` or `annotations_verified` is false, `confidence < 0.7`,
or key fields are empty (`voxel_size_nm`, `publications`, `formats: [other]`).
Take the oldest `verification.last_checked` first. For each one:
1. Re-fetch its sources and fill in facts that are now confirmed.
2. Fill `null` / `unknown` fields using the research steps in find-datasets step 4a: the full dataset page,
   the file listing (`tools/peek_archive.py` for zips) and the paper (`tools/paper.py`), plus any `short_name` over 50 characters.
3. Raise `confidence` only when the evidence supports it (caps in CLAUDE.md).
4. Update `last_checked`.

Never delete records. If a dataset has clearly disappeared, set `data.access: restricted`,
`url_ok: false` and a dated note.

## 3. Validate and publish (skip git in dry-run)
```bash
python tools/validate.py
git add datasets state/runs && git commit -m "maintenance: link checks + re-verification" && git push -u origin "$BRANCH" --force
```
Open or update a PR from `claude/maintainer` with the label `maintenance`, listing each change and its reason.

## 4. Review queue health
List the open PRs labeled `new-datasets` (with `gh pr list --label new-datasets --json number,title,createdAt,url` or the GitHub tools available).
Note any PR open longer than 7 days. Then check freshness: run `git fetch origin` and
`python -m tools.collect_runs`, which reads run logs from `main` and every `claude/state/*` branch.
Find each routine's latest run.
Flag any routine whose last run is older than 2× its cadence (repositories 1h, literature 3h, websearch 3h).

## 5. Daily digest (Slack connector)
Post a single message to `#mia-harvester`:
- **Last 24h:** runs per routine (ok / partial / failed), records proposed, duplicates skipped, errors.
- **Catalog:** total records on `main`, new records merged in the last 24h.
- **Review queue:** number of open `new-datasets` PRs (one dataset each) and the oldest ones' ages. Stale ones get ⚠.
- **Link health:** records checked and how many are broken.
- **Stale routines:** list them, if any.

Finish with `python tools/run_log.py finish` (then commit and push the run log if not a dry run), and a short summary.
