---
name: find-datasets
description: Harvest run. Search one source family for usable microscopy training datasets, write schema-valid YAML records, and update this routine's review PR. Args are source=repositories|literature|websearch, optionally limit=N (max new records, default 8) and dry-run.
---

# find-datasets

Arguments: `$ARGUMENTS`. Parse `source` (required), `limit` (default 8) and `dry-run` (flag).
`ROUTINE=harvest-<source>`, `BRANCH=claude/harvest-<source>`.

Read `CLAUDE.md` first. Its hard rules and "usable" criteria apply to every step.

## 0. Setup
```bash
pip install -q -r requirements.txt
```
Skip git steps 0b and 6 if `dry-run`.

**0b. Branch.** Each routine keeps a single long-lived branch and PR. Unmerged records and
search state carry over between runs, so later runs see earlier proposals and don't re-search:
```bash
git fetch origin
if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null; then
  git checkout -B "$BRANCH" "origin/$BRANCH"
  git merge --no-edit origin/main || { git merge --abort; echo "MERGE_CONFLICT"; }
else
  git checkout -B "$BRANCH" origin/main
fi
```
On `MERGE_CONFLICT`:
1. Reset to `origin/main`, keeping only `state/frontier/$ROUTINE.yaml` from the old branch: `git checkout origin/$BRANCH -- state/frontier/$ROUTINE.yaml`.
2. Log `event error --reason "merge conflict; branch reset"`.
3. Force-push at the end of the run.

**0c. Start the run log:**
`python tools/run_log.py start --routine $ROUTINE` (add `--dry-run` when dry-run).

## 1. Pick work
`python tools/frontier.py next --routine $ROUTINE --n 3`. These are your queries for this run.
Log each one with `run_log.py query "<key>"`.

## 2. Search (per source family)
- **repositories:** the key is `<client>: <query>`. Run `python -m tools.sources <client> "<query>" --limit 15`.
  Candidates are only leads, never facts. For each promising candidate, WebFetch its landing page
  (and, where one exists, the file listing or API record) to confirm the details.
- **literature:** use WebSearch plus bioRxiv, PubMed or journal pages to find papers that *release* a
  dataset ("data availability", "we release", "benchmark"). Follow each lead to where the data is
  actually hosted. That hosting page is the record's `landing_url` and `repository`, not the paper.
  Put the paper in `publications`.
- **websearch:** use WebSearch and WebFetch on portals, challenge sites, lab pages and curated
  lists. From a list page, pull out the individual datasets. One dataset is one record.

After **every** WebFetch or API call whose content you rely on, run `python tools/run_log.py fetched <url>`.
When a page points to a promising new query or portal, run
`python tools/frontier.py add --routine $ROUTINE --key "<new key>"`. Add at most 3 per run.

## 3. Screen each candidate
1. Dedup: `python tools/dedup.py --doi D --repository R --accession A --url U --title "T"`.
   - `duplicate` or `rejected`: run `run_log.py event duplicate --id <slug> --reason "<match>"` and skip.
   - `possible-duplicate`: compare against the matched file. Skip it if it's the same dataset.
2. Check the "usable" criteria in CLAUDE.md. If it fails, run `run_log.py event rejected --id <slug> --reason "<which criterion>"`.
3. Assess confidence. If it's below 0.5, run `run_log.py event low-confidence --id <slug> --reason "..."`.

## 4. Write the record
```bash
python tools/new_record.py --id <slug> --repository <Repo> --by $ROUTINE
```
Replace **every** `TODO`, using only facts from fetched pages. Unknown values become `null`,
`unknown` or `other` (see CLAUDE.md). Set these fields:
- `verification.url_ok: true` only if you fetched the landing page successfully this run.
- `license_found: true` only if you saw the license.
- `annotations_verified: true` only if you saw the annotation files in a file listing.
- `provenance.evidence_urls`: list the pages you used; each one must already be logged with `run_log.py fetched`.

Then run:
```bash
python tools/validate.py <file> --run-log <run log path> --min-confidence 0.5
```
Fix the errors and re-validate. If a record still fails after 2 fix attempts, delete the file and
log `event error --id <slug> --reason "<validator message>"`. Otherwise log `event added --id <slug>`.

Stop adding records once you reach `limit`.

## 5. Update the frontier
Run `python tools/frontier.py touch --routine $ROUTINE --key "<key>"` for each query you finished.

## 6. Publish (skip if dry-run)
```bash
python tools/validate.py          # whole catalog must pass
git add datasets state/frontier state/runs
git commit -m "harvest($ROUTINE): +<n> datasets"   # commit even when n=0: run log + frontier are the heartbeat
git push -u origin "$BRANCH"       # add --force only after a MERGE_CONFLICT reset
```
Next, make sure a PR from `$BRANCH` into `main` exists. Use `gh pr create` or the GitHub tooling
available in this session:
- Title: `[<routine>] new microscopy datasets`
- Label: `new-datasets`
- Body: a markdown table of **all** records on the branch that aren't on `main`
  (`git diff --name-only origin/main -- datasets/`), with columns
  id | title | modality | annotations | license | confidence.
  Also include this reviewer note: *merge = accept all; to reject one record, delete its file in the PR
  and add its id to `state/rejected.yaml`; closing the PR rejects every record in it*.

If a PR already exists, update its body with the refreshed table.
Record the PR URL: `python tools/run_log.py finish --pr-url <url>`.
Then `git add state/runs && git commit -m "run log" && git push`.

In dry-run, just run `python tools/run_log.py finish`.

## 7. Notify
If a Slack connector is available, post to `#mia-harvester` **only** when one of these is true:
- the run status is `failed`,
- `counts.error ≥ 3`,
- you added a record with `confidence ≥ 0.9` **and** `annotations.types` containing segmentation, tracking or synapse labels (a "notable find"). Include the title, landing URL and PR link.

Otherwise stay quiet. The maintainer sends a daily digest.

## 8. Final message
End with a short summary: the queries run, the counts from `run_log.py show`, and the PR URL.
