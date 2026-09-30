---
name: enrich-prs
description: Enricher run. Goes through open dataset PRs that aren't enriched yet and inspects the actual files (folder structure, confirmed size, shapes, dtypes, compression, value ranges, label encoding, raw/label alignment, license), writes the results into the PR's record and updates the PR. Labels follow automatically. Optional args are limit=N (max PRs, default 20), pr=<number> (just that PR), shard=K/N (only PRs whose number mod N is K, for parallel runs) and dry-run.
---

# enrich-prs

Arguments: `$ARGUMENTS`. Parse `limit` (default 20), `pr` (optional), `shard` (optional, `K/N`) and `dry-run` (flag).
`ROUTINE=enricher`.

Read `CLAUDE.md` first. Its hard rules apply, in particular: **never invent values**. Everything you
write must come from a page you fetched or a file you inspected in this run. This routine goes
deeper than the harvesters: it reads file headers and small samples. It still never downloads a
whole dataset.

**What you change:** only the one record on each PR's `claude/dataset/<id>` branch, the PR body and
one PR comment. Never another file, branch or PR, and never `main`. Labels are computed from the
record by `.github/workflows/label.yml`, so **don't add labels yourself**.

## 0. Setup
```bash
pip install -q -r requirements-inspect.txt
git fetch origin
git checkout -q --detach origin/main
python tools/publish.py state-pull --routine enricher      # skip in dry-run
python tools/run_log.py start --routine enricher           # add --dry-run in dry-run; note the printed path
```

## 1. The queue
```bash
gh pr list --state open --label new-datasets --limit 300 --json number,headRefName,labels,createdAt
python tools/run_log.py recent-errors --routine enricher   # ids that failed in ≥ 2 of the last 5 runs: skip them
```
The queue is the open PRs whose branch starts with `claude/dataset/` and that **don't** have the
`enriched` label, oldest first. With `pr=<number>`, the queue is just that PR (even if it's enriched).
With `shard=K/N`, keep only PRs whose number modulo N equals K. Several runs with different K then
split the backlog without ever touching the same PR.

Work through it one PR at a time:
```
while python tools/run_log.py continue --time-only; do      # exit 1 = the 110-minute cutoff
    take the next PR; if there is none (or `limit` PRs are done): stop with --stop-reason queue-empty
    steps 2–7 for it
done
```
Spend about **10 minutes per PR**, and check `python tools/run_log.py show` (`elapsed_min`) as you go.
If a PR needs much longer, write down what you have (method `metadata-only` or `header` is fine),
and say in `technical.notes` what is still open.

## 2. Pull the record
```bash
python tools/publish.py pr-pull <id>        # writes the record into the tree, saves a baseline copy
gh pr view <number> --json body,comments
```
- `human_edits` lists commits a person made on the branch. Run `git show <sha>` for each one, and
  **don't change any field they changed**. A human edit always wins.
- Read the PR comments too. A reviewer may have asked for something specific.
- Keep the harvest run log path from the PR body (`state/runs/harvest-…json`). You need it in step 6.
- Read the whole record, its `landing_url` and its `download_url` before you start.

## 3. Inspect the files
Answer these questions for the dataset. Each one maps to a field in `technical` (schema v1.2).

| Question | Field | How |
|---|---|---|
| What files are there, in what folders? | `layout.tree`, `layout.n_files` | `tools/listing.py` |
| How big is it, exactly? | `data.size_bytes`, `size_source` | `tools/listing.py` total |
| Smallest piece to test with? | `sample` | from the listing: one raw + label pair |
| Raw images, restoration targets or labels? | `arrays[].role` | file/folder names, docs |
| Format, axes, tensor shape | `format`, `axes`, `shape`, `shape_varies` | `tools/probe.py` |
| Data type (int / float, bits) | `dtype` | `tools/probe.py` |
| Compression inside the files, lossy? | `compression`, `lossy`, `chunks` | `tools/probe.py` |
| Intensity range, contrast-normalized? | `value_range`, `normalization` | `tools/sample.py` + docs |
| What are the labels (binary, semantic IDs, instance IDs…)? | `encoding`, `classes`, `n_ids_observed` | `tools/sample.py` + docs |
| How do labels align with the raw data? | `alignment`, `alignment_notes` | shapes, voxel sizes, transforms |

**a. Listing.** Pass *all* of the dataset's top-level URLs: the record, every zip the page links to,
the folder, the bucket prefix.
```bash
python tools/listing.py <url> [<url> ...] --id <id> --expand-zips --examples 3
```
- Copy the tree into `layout.tree`. You may shorten it, but keep it ≤ 40 lines. Set `layout.n_files`
  and `layout.listing_complete` from the JSON line.
- **Confirmed size:** set `size_source: file-listing` only if the JSON says `"size_source": "file-listing"`
  **and** your URLs cover every file the dataset offers. Then set `data.size_bytes` to exactly
  `total_bytes`; the validator checks this. The `auto-download` label depends on it (confirmed and
  < 50 GB), so when in doubt don't claim it.
- Otherwise, use `page-stated`, `paper` or `estimated`, and leave the existing `size_bytes` unless you have a better stated value.
- For IDR, use its JSON API instead of downloading. `https://idr.openmicroscopy.org/webclient/imgData/<image id>/`
  gives size X/Y/Z/C/T, pixel type and physical sizes. For the CZ cryoET portal, use the S3 listing
  (listing.py handles `/datasets/<n>`) and probe the `.zarr` stores.

**b. Pick representatives.** From the listing, choose 1–3 files per role: raw, label and, for
restoration data, target. Work out how raw and label files pair up (same stem with `_mask`/`_gt`,
parallel `images/` and `masks/` folders, same well ID). Note the rule in `alignment_notes`.

**c. Headers.** These are cheap: a few KB per file.
```bash
python tools/probe.py <file url> --id <id>
python tools/probe.py <zip url> --glob '*/masks/*.tif' --n 2 --id <id>      # members of a remote zip
```
- `voxel_size_nm` from a header is the stored calibration. Use it to fill `imaging.voxel_size_nm`
  only if it's plausible for the modality and agrees with the docs; uncalibrated files often say
  1 px = 1 µm. If it conflicts with the record, keep the record's value and note the conflict.
- `axes: null` means the file doesn't say. Only set axes when the header or the docs state them.

**d. Sample.** Download the smallest raw + label pair and measure it. The budget is 500 MB per
record per run, and the tool enforces it.
```bash
python tools/sample.py --id <id> --raw "<url or zip url::member>" --label "<url or zip url::member>"
```
Turn its output into fields like this. Its `hints` are heuristics, not facts:
- `value_range`: the observed `[min, max]` of the sample. Say in `technical.notes` that it's from one sample.
- `normalization`:
  - `rescaled` if float in [0, 1]; `standardized` if mean ≈ 0 and std ≈ 1.
  - `contrast-stretched` only if the docs say so, or integer data fills the whole dtype range and is saturated at both ends.
  - `none` only if the docs/paper say the data are raw, or integer data sits in a detector's native range without clipping (say "inferred" in notes).
  - Otherwise `unknown`.
- `encoding`: `binary` for {0,1} / {0,255}; `instance-ids` or `semantic-ids` when the docs say so
  or the sample makes it unambiguous (e.g. hundreds of IDs); otherwise `unknown`. Put class names in
  `classes` only if the docs name them.
- `n_ids_observed`: `n_unique` of the label sample.
- `alignment`:
  - `same-grid` if shapes match and nothing says otherwise.
  - `scaled` if the shape ratio matches a voxel-size ratio.
  - `cropped` / `offset` if docs or OME-Zarr translations say so.
  - `transform-provided` if the files carry a transform.
  - `not-applicable` for raw arrays; `unknown` otherwise.
- Tables of points, boxes or tracks (CSV / JSON) are label arrays too, with `format: csv|json`,
  `shape` = [rows, columns] if you read it, and `encoding: points-table|boxes-table|tracks-table`.

**e. Docs inside the data.** Read READMEs, LICENSE files and class maps inside archives:
`python tools/peek_archive.py <zip url> --cat <path>`. Log any other page you read with `run_log.py fetched`.

**f. Formats the tools can't read** (CZI, ND2, LIF, DICOM, custom binary, an API that serves chunks):
you may write a short script in your scratch directory. Use only `aicsimageio`, `nd2`, `readlif`,
`pydicom`, `ome-zarr`, `cryoet-data-portal` or `requests`. Stay within the sample budget. Log every
read with `python tools/run_log.py inspected --kind header|sample --url <url> --id <id> --bytes <n>`.
Put the script's key output in the PR comment (step 6).

**Untrusted data rules:** never run code that ships with a dataset (scripts, notebooks, `setup.py`),
never `pip install` a package a dataset's README names (other than the ones above), and never
unpickle anything (`pickle`, `joblib`, `torch.load`, `np.load(allow_pickle=True)`). Downloads go to
temporary directories and are deleted. No whole-dataset downloads, ever.

## 4. License
The PR gets a `license:<spdx>` label straight from `license.spdx`. **Don't interpret the license.**
Only find and record it.
- If `license.spdx` is `unknown`, look harder: the dataset page (footer, "terms", "rights"), the
  repository API record, README/LICENSE files inside the archives (`peek_archive.py --cat`), a linked
  GitHub repo's LICENSE file, and the paper's Data availability section (`tools/paper.py`).
- If you find it: set `spdx` (an SPDX id, or `custom` for custom terms, quoted briefly in `notes`)
  and `url`, and set `license_found: true`. Add the page to `evidence_urls`; it must be logged.
- If there's still none: keep `unknown` and `license_found: false`. The PR then shows `license:unknown`.
  Add a draft request to the PR comment (step 6) that a human could send to the authors:
  a short, polite note asking under which license the data may be reused. **Never contact authors yourself.**

## 5. Other fields
Inspection often answers fields the harvester left empty or got wrong:
- `imaging.voxel_size_nm`, `imaging.channels` and `imaging.dimensionality`
- `data.formats` (never leave `other` for a format you probed) and `data.n_items`
- `annotations.format`, `annotations.types`
- `verification.annotations_verified: true` once you have seen label files in a listing or sample.

Fill or correct them only from what you inspected, and add one short `notes` line per change
(`enricher: voxel size from OME-Zarr scale (was null)`). `notes` is limited to 1000 characters;
inspection details go in `technical.notes`. You may adjust `provenance.confidence` up or down to
match the evidence (the caps in CLAUDE.md still apply). Set `verification.last_checked` to now.

## 6. Write, validate, publish
Set `schema_version: '1.2'` and write the `technical` block:
- `method`: the deepest check done: `metadata-only`, `header` or `sample`
- `inspected_at`: now
- `inspected_urls`: the URLs you passed to listing / probe / sample
- `bytes_downloaded`: the sum of the sample tools' `bytes_downloaded`
- `size_source`, `layout`, `sample` (null if there is no sensible small unit), `arrays`, `notes`

If a dataset can't be inspected at all (registration-only, Aspera/Globus only, dead links), still
write `technical` with `method: metadata-only` and explain why in `technical.notes`, so the PR
leaves the queue.

```bash
python tools/validate.py <file> --run-log <enricher run log> --baseline state/.enrich/<id>.orig.yaml --min-confidence 0.5
```
Fix errors and re-validate. After 2 failed attempts, log `event error --id <id> --reason "<message>"`,
delete the file and move on.

Then publish (skip everything but the event in dry-run, and delete the file there instead):
```bash
python tools/pr_text.py <file> --body --run-log <harvest run log from the PR body> --enrich-log <enricher run log> > /tmp/pr_body.md
python tools/publish.py pr-update <file>          # commits onto claude/dataset/<id>; refuses if the branch moved
gh pr edit <number> --body-file /tmp/pr_body.md
gh pr comment <number> --body-file /tmp/pr_comment.md
python tools/run_log.py event enriched --id <id> --pr-url <PR url>
```
If `pr-update` says the branch moved, someone edited the PR meanwhile. Run `pr-pull` again, redo
your changes on top, respecting their edit, and publish again.

`/tmp/pr_comment.md` is the **inspection report**. Keep it short and factual:
- a heading `Enricher inspection`
- which files were listed, probed and sampled, with byte counts
- the key raw tool outputs: the listing's JSON line, and each probe/sample shape, dtype, value range and hints
- what's still unknown and why
- the draft license request, if any

## 7. Finish (always, even if the run failed)
```bash
python tools/run_log.py finish --stop-reason queue-empty    # or time-limit
python tools/publish.py state-push --routine enricher       # skip in dry-run
```
If a Slack connector is available, post to `#mia-harvester` only when the run status is `failed`
or `counts.error ≥ 3`.

End with a short summary: runtime, PRs enriched (number, title, method, and whether it got
`auto-download`), PRs skipped or failed with reasons, and how many unenriched PRs are left in the queue.
