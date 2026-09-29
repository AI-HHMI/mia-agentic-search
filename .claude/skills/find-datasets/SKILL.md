---
name: find-datasets
description: Harvest run. Search one source family for usable microscopy training datasets, write schema-valid YAML records, and open one PR per dataset. Args are source=repositories|literature|websearch, optionally limit=N (max new datasets, default 5) and dry-run.
---

# find-datasets

Arguments: `$ARGUMENTS`. Parse `source` (required), `limit` (default 5) and `dry-run` (flag).
`ROUTINE=harvest-<source>`.

Read `CLAUDE.md` first. Its hard rules and "usable" criteria apply to every step.

**How branches work:**
- **Each dataset gets its own branch and PR:** `claude/dataset/<id>`, containing exactly one new file.
- **This routine's search state lives on `claude/state/$ROUTINE`:** its frontier and run logs. That
  branch is pushed directly and never reviewed.
- **You never create branches by hand.** `tools/publish.py` handles all branching and pushing.
- **Your working tree stays on `origin/main`.** Leave records uncommitted there.

## 0. Setup
```bash
pip install -q -r requirements.txt
git fetch origin                     # needed so dedup sees main, rejections and pending dataset branches
git checkout -q --detach origin/main
python tools/publish.py state-pull --routine $ROUTINE      # skip in dry-run
python tools/run_log.py start --routine $ROUTINE           # add --dry-run in dry-run; note the printed path
```

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

If a host is blocked by the sandbox network policy (`403` on `CONNECT`, or a proxy error), log it once:
`run_log.py event error --reason "network blocked: <host>"`. Then skip that host's other queries in
this run and go on to the next source.

After **every** WebFetch or API call whose content you rely on, run `python tools/run_log.py fetched <url>`.
When a page points to a promising new query or portal, run
`python tools/frontier.py add --routine $ROUTINE --key "<new key>"`. Add at most 3 per run.

## 3. Screen each candidate
1. Dedup: `python tools/dedup.py --doi D --repository R --accession A --url U --title "T"`.
   - `duplicate`, `pending` (already has an open PR) or `rejected`: run `run_log.py event duplicate --id <slug> --reason "<status>: <match>"` and skip.
   - `possible-duplicate`: compare against the matched file. Skip it if it's the same dataset.
2. Check the "usable" criteria in CLAUDE.md. If it fails, run `run_log.py event rejected --id <slug> --reason "<which criterion>"`.
3. Estimate confidence (next section). If it's below 0.5, run `run_log.py event low-confidence --id <slug> --reason "..."` and skip.

## 4. Write the record
```bash
python tools/new_record.py --id <slug> --repository <Repo> --by $ROUTINE
```
### 4a. Research every field before writing `null` / `unknown`
Don't write a field as unknown until you've looked in **all** of these sources, in this order:
1. **The dataset page, read in full.** Also read its metadata/API record, its README or
   description files, and any linked documentation or challenge "data" page.
2. **The file listing.** Look at the repository's file browser or FTP directory. For a single `.zip`, run
   `python tools/peek_archive.py <zip url>`, which lists what's inside without downloading it.
   This is where file formats, image/mask pairs, train/val/test folders and counts come from.
   Never write `formats: [other]` for a zip whose contents you haven't listed.
3. **The paper.** Find it on the dataset page (citation, "related publications", DOI), in the
   repository's API record, or with WebSearch. If a paper exists, you **must** read it:
   ```bash
   python tools/paper.py --doi <doi>        # or --pmid / --title
   ```
   The tool gets the full text from Europe PMC or bioRxiv even when the publisher page returns 403.
   It prints key sentences and writes the full text to a file. **Read the Methods and the Data
   availability sections** of that file. They usually state voxel/pixel size, microscope and
   modality, staining, organism/sample, annotation procedure and the train/test split.
   - Log every URL the tool prints with `run_log.py fetched`, and cite at least one in `evidence_urls`.
     The validator rejects records that list a paper but cite no paper source.
   - Add the paper to `publications` (title + DOI).
   - If the full text isn't open access, read the abstract, the supplementary material and the
     preprint version.

Only after these three steps may a value be `null` / `unknown`. When one is, say in `notes`
which sources you checked, e.g. *"voxel size not stated on dataset page, file headers or paper Methods"*.

### 4b. Fill in the record
Replace **every** `TODO` with facts from the pages you fetched:
- `short_name`: at most 50 characters. It becomes the PR title `Add dataset: <short_name>`.
  Name what's in the dataset and the modality, e.g. `CryoVesNet synaptic vesicles (cryo-ET)` or
  `C. elegans 3D nuclei segmentation (confocal)`. No accession numbers and no filler words.
- `data.formats` and `imaging.dimensionality` appear as their own rows in the PR table, so they must
  come from the file listing or paper, not from guesses.
- `verification.url_ok: true` only if you fetched the landing page successfully this run.
- `license_found: true` only if you saw the license.
- `annotations_verified: true` only if you saw the annotation files in a file listing (peek_archive counts).
- `provenance.evidence_urls`: list the pages you used; each one must already be logged with `run_log.py fetched`.
- `provenance.confidence`: an internal filter only; it isn't shown in the PR. It's your estimate that
  every field is correct and the dataset is usable. Below 0.5, don't propose the dataset. The
  validator caps it: `url_ok: false` → max 0.5, `license_found: false` → max 0.7,
  `annotations_verified: false` → max 0.85.

Then run:
```bash
python tools/validate.py <file> --run-log <run log path> --min-confidence 0.5
```
Fix the errors and re-validate. If a record still fails after 2 fix attempts, delete the file and
log `event error --id <slug> --reason "<validator message>"`.

## 5. Publish each dataset as its own PR (skip in dry-run)
For each valid record, right after it validates:
```bash
python tools/publish.py dataset <file>            # pushes claude/dataset/<id>; prints the branch
python tools/pr_text.py <file> --title > /tmp/pr_title.txt
python tools/pr_text.py <file> --body --run-log <run log path> > /tmp/pr_body.md
```
Open a PR from that branch into `main`, using **exactly** that title and body (don't rewrite them).
Use `gh pr create --base main --head claude/dataset/<id> --title "$(cat /tmp/pr_title.txt)" --body-file /tmp/pr_body.md`
or the GitHub tooling available in this session. A workflow adds the `new-datasets` label automatically.
Then log `python tools/run_log.py event added --id <slug> --pr-url <PR url>`.

Stop once you've published `limit` datasets. In dry-run, log `event added --id <slug>` without `--pr-url`.

## 6. Save state (skip git in dry-run)
**Always do this, even if the run failed or found nothing.** An unfinished run log stays `running`
forever, and the watchdog treats that as a failure:
```bash
# for each query you finished:
python tools/frontier.py touch --routine $ROUTINE --key "<key>"
python tools/run_log.py finish
python tools/publish.py state-push --routine $ROUTINE      # skip in dry-run
```

## 7. Notify
If a Slack connector is available, post to `#mia-harvester` **only** when one of these is true:
- the run status is `failed`,
- `counts.error ≥ 3`,
- you added a record with `confidence ≥ 0.9` **and** `annotations.types` containing segmentation,
  tracking or synapse labels (a "notable find"). Include the title, dataset page link and PR link.

Otherwise stay quiet. The maintainer sends a daily digest.

## 8. Final message
End with a short summary: the queries run, the counts from `run_log.py show`, and one line per PR opened (title + URL).
