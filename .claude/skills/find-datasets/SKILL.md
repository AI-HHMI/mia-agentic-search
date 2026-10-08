---
name: find-datasets
description: Harvest run. Search one source family until 10 new usable microscopy training datasets are published (max 2 h), write schema-valid YAML records, and open one PR per dataset. Args are source=repositories|literature|websearch, optionally target=N (new datasets to publish, default 10) and dry-run.
---

# find-datasets

Arguments: `$ARGUMENTS`. Parse `source` (required), `target` (default 10) and `dry-run` (flag).
`ROUTINE=harvest-<source>`, `TARGET=<target>` (default 10).

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
git fetch origin                     # dedup.py and publish.py re-fetch main and dataset branches themselves
git checkout -q --detach origin/main
python tools/publish.py state-pull --routine $ROUTINE      # skip in dry-run
python tools/run_log.py start --routine $ROUTINE --target $TARGET   # add --dry-run in dry-run; note the printed path
```

## 1. The search loop: keep going until `target` new datasets are published (max 2 h)
A run aims to publish **`target` (default 10) datasets that aren't on `main`, have no open PR and
weren't rejected**, each as **its own PR** (step 5). Duplicates don't count. A run that publishes at
least one but fewer than `target` before the cutoff is still a success. Work through the frontier
**one query at a time**:

```
while python tools/run_log.py continue; do      # exit 1 = stop (`target` datasets published, or 110 min used)
    KEY = python tools/frontier.py next --routine $ROUTINE      # e.g. ["zenodo: FIB-SEM ground truth"]
    if KEY is empty: add 3–5 new queries (see "Growing the frontier"), then retry;
                     if you still have none, stop with --stop-reason frontier-exhausted
    python tools/run_log.py query "<KEY>"
    steps 2–5 for this query: search, screen, research, write, publish each new dataset as its own PR
    python tools/frontier.py touch --routine $ROUTINE --key "<KEY>"     # right after each query
done
```

- **Check `run_log.py continue` before every new query**, and also between candidates during a long
  query. It stops the loop once `target` new datasets have been published, or once 110 minutes
  have passed since the run started.
- **Publish as you go.** Each dataset gets its own branch and PR (step 5) right after it validates,
  not batched at the end, so a run cut short still leaves every finished dataset proposed.
- **When the target is reached mid-query,** stop there; don't publish more than `target` in one run.
- **Hard limit: the whole run ends within 2 hours.** The last 10 minutes are for step 6. At the
  cutoff, publish any record that already validates, drop half-researched ones (log them as
  `event error --reason "time limit"`), and go straight to step 6.
- **Growing the frontier.** When a query only turns up duplicates, try new angles: other
  repositories, modalities, organisms, annotation types, challenge years, or "papers citing X".
  Add them with `frontier.py add`; with a target of 10, add up to 10 per run when the frontier runs low.
  Queries that keep producing only duplicates are fine to keep; the frontier rotates them to the back.

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
`python tools/frontier.py add --routine $ROUTINE --key "<new key>"`.

## 3. Screen each candidate
1. Dedup: `python tools/dedup.py --doi D --repository R --accession A --url U --title "T" --paper-doi P --download-url DL`
   (pass every paper DOI you know with its own `--paper-doi`; re-deposits of the same data in another
   repository usually share the paper or the download, not the dataset DOI).
   - `duplicate`, `pending` (already has an open PR) or `rejected`: run `run_log.py event duplicate --id <slug> --reason "<status>: <match>"` and skip.
   - `possible-duplicate`: open the matched record or PR and compare: same images (count, sizes, file
     names)? A re-deposit, a new version or a mirror of the same data is a duplicate: skip it and log
     `event duplicate`. Only a genuinely different dataset (e.g. another dataset of the same challenge)
     may go ahead, and its `notes` must say how it differs from the matched one.
2. Check the "usable" criteria in CLAUDE.md. If it fails, run `run_log.py event rejected --id <slug> --reason "<which criterion>"`.
3. Estimate confidence (next section). If it's below 0.5, run `run_log.py event low-confidence --id <slug> --reason "..."` and skip.

## 4. Write the record
```bash
python tools/new_record.py --id <slug> --repository <Repo> --by $ROUTINE    # drafts/<slug>.yaml
```
The draft stays in `drafts/`. `publish.py dataset` files it at `datasets/<dimensionality>/<first modality>/`,
so list the modality that best describes the images first.
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
- `short_name`: at most 50 characters. It is used verbatim as the PR title.
  Name what's in the dataset and the modality, e.g. `CryoVesNet synaptic vesicles (cryo-ET)` or
  `C. elegans 3D nuclei segmentation (confocal)`. No accession numbers and no filler words.
- `data.formats` and `imaging.dimensionality` appear as their own rows in the PR table, so they must
  come from the file listing or paper, not from guesses. Use `other` only when none of the listed formats
  applies, never alongside a known one; describe extra file types (GeoJSON, CSV, …) in `annotations.format`.
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
python tools/publish.py dataset <file> --run-log <run log path>   # prints the PR URL
```
This re-runs dedup against freshly fetched main, open dataset PRs and rejections, and refuses (non-zero
exit) a duplicate: then log `event duplicate` and move on. Otherwise it pushes `claude/dataset/<id>`, opens
the PR with the `tools/pr_text.py` title and body, and logs `event added`. If only `gh pr create` fails, open
the PR with that title and body verbatim using the GitHub tooling of this session, then log
`run_log.py event added --id <slug> --pr-url <PR url>`.

Never publish more than `target` datasets in one run, and never put two datasets in one PR. In dry-run, log
`event added --id <slug>` without `--pr-url`; that counts towards the target for `run_log.py continue`.

## 6. Save state (skip git in dry-run)
**Always do this, even if the run failed or found nothing.** An unfinished run log stays `running`
forever, and the watchdog treats that as a failure:
```bash
python tools/run_log.py finish          # records stop_reason: found | time-limit | frontier-exhausted
python tools/publish.py state-push --routine $ROUTINE      # skip in dry-run
```

## 7. Notify
If a Slack connector is available, post to `#mia-harvester` **only** for a notable find: a record you added
with `confidence ≥ 0.9` **and** `annotations.types` containing segmentation, tracking or synapse labels.
Include the title, dataset page link and PR link. Otherwise stay quiet: the watchdog reports failed runs and
the maintainer sends a daily digest.

## 8. Final message
End with a short summary: the queries run, the runtime and stop reason, the counts from `run_log.py show`, and one line per PR opened (title + URL).
