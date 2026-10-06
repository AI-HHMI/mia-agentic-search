# mia-agentic-search

An agent-maintained catalog of **usable microscopy training datasets**. The git repo *is* the
database: one YAML record per dataset in `datasets/<dimensionality>/<first modality>/<id>.yaml`
(e.g. `datasets/3D/FIB-SEM/cremi.yaml`), validated
against `schema/dataset.schema.json`. Agents (Claude scheduled routines) propose records via PRs;
humans review them (merge = accept, close = reject).

## Hard rules for agents
1. **Never invent values.** Every field must be supported by a page you actually fetched in this
   run. If you can't find a value, write `null` (or `unknown` / `other` where the schema has
   that enum value) and explain in `notes`. Guessing is worse than leaving it blank.
2. **Only the schema's fields and enum values.** Don't add keys. If nothing fits, use `other`
   and say what it is in `notes`.
3. **Every URL in `provenance.evidence_urls` must be logged** with `tools/run_log.py fetched`.
   CI rejects records citing pages you never logged.
4. **Dedup before writing:** run `python tools/dedup.py --doi ... --repository ... --accession ... --url ... --title ... --paper-doi ... --download-url ...`.
   `duplicate`/`pending`/`rejected` → skip and log it. `possible-duplicate` → open both pages and decide.
5. **Validate before pushing:** `python tools/validate.py <your files> --run-log <run log> --min-confidence 0.5` must pass.
6. Never edit or delete records on `main` from a harvest run. Corrections belong to the maintainer routine.
7. Never push to `main`. **One dataset per PR:** publish each record with `tools/publish.py dataset`
   (branch `claude/dataset/<id>`), and use `tools/pr_text.py` for the PR title and body, verbatim.
   Search state goes to `claude/state/<routine>` via `tools/publish.py state-push`.
8. Slack notifications go to **`#mia-harvester`** via the Slack connector. Don't post to any other channel.
9. **Run until 10 new datasets are published, max 2 hours.** Harvest runs keep searching, one frontier query
   at a time, until they publish 10 datasets (`target`, default 10) that aren't on `main`, have no open PR and
   weren't rejected, **each as its own PR**.
   Check `tools/run_log.py continue` before each query. Searching stops at 110 min; every run ends by 120 min.
10. Be polite to servers: ≤ 1 request/second per host, no bulk downloads. Metadata only. The one exception
   is the enricher (below), which may read file headers and download small samples within its budget.

## What counts as "usable"
A dataset qualifies only if **all** of these hold:
- **Microscopy images** (EM, light, X-ray, histology, cryo-ET, …), not just derived tables or figures.
- **Obtainable:** `access` is `open` or `registration`. Skip `on-request`/`restricted`, except for
  well-known benchmarks, which get `confidence ≤ 0.6` and a note.
- **Training signal:** it has annotations (masks, boxes, points, tracks, labels, skeletons, synapses),
  **or** it's clearly usable without labels: restoration pairs (denoising / super-resolution) or a
  dataset explicitly released for self-supervised / foundation-model training.
- **Terms:** it states a license or terms of use. If none can be found, set `license.spdx: unknown`
  and `verification.license_found: false`, and cap `confidence` at 0.7.
- **Non-trivial size:** at least ~10 images or 1 volume.

Log anything that fails these checks as `run_log.py event rejected --reason "..."`. Don't write a record for it.

## Research before writing "unknown"
A field may be `null` / `unknown` only after checking the **dataset page (in full), its file listing,
and the paper**. If a paper exists, read its full text with `tools/paper.py`: Methods and Data
availability. List what zips contain with `tools/peek_archive.py`. The find-datasets skill, step 4a,
gives the procedure. The validator rejects records that list a paper without citing a paper source.

## Confidence (internal filter, not shown in PRs)
`provenance.confidence` is the estimated probability that every field is correct and the dataset is
usable for training. Records below 0.5 aren't proposed. The validator caps it by verification:
`url_ok: false` → max 0.5, `license_found: false` → max 0.7, `annotations_verified: false` → max 0.85.

## Field conventions
- `id`: lowercase slug. Put the accession first when there is one, e.g. `empiar-10311-hela-fib-sem`, `s-biad2822-vem-nuclei`.
- **File location** follows from the record: `datasets/<imaging.dimensionality>/<imaging.modality[0]>/<id>.yaml`.
  `tools/new_record.py` drafts into `drafts/` and `tools/publish.py` files the record; after changing the
  dimensionality or first modality of an existing record, move it with `python tools/place.py <file>`.
  Put the modality that best describes the images first. Files still in the old `datasets/<repository>/`
  layout pass validation with a warning.
- `repository`: where the data is *hosted*. A dataset on its own website → `LabWebsite` or `other`.
- `voxel_size_nm`: in **nanometres**, so 0.116 µm → 116. For 2D data, `z: null`.
- `organism`: NCBI scientific names (`Mus musculus`, not "mouse").
- `data.formats`: every format the files come in. `other` only when **none** of the listed formats applies;
  never next to a known one (a zip of TIFFs plus GeoJSON annotations is `[tiff]`, the GeoJSON goes in
  `annotations.format`).
- `license.spdx`: SPDX IDs (`CC-BY-4.0`, `CC0-1.0`, `CC-BY-NC-4.0`, `MIT`), `custom` or `unknown`.
- `short_name`: ≤ 50 characters naming the content and modality; it's used verbatim as the PR title.
- Timestamps are UTC ISO-8601 with `Z`.

## Enricher (`/enrich-prs`)
A fifth routine inspects the files of open dataset PRs and fills the record's `technical` block
(schema v1.2): folder tree, confirmed size, and per array (raw / target / label) the format, axes,
shape, dtype, compression, value range, normalization, label encoding and raw↔label alignment.
- **Only on PR branches:** it edits the one record on `claude/dataset/<id>` via `tools/publish.py pr-pull` /
  `pr-update`, never `main`, and never overwrites a field a human changed on the branch.
- **Inspection budget:** headers via range requests (`tools/probe.py`), plus at most 500 MB of samples per
  record per run (`tools/sample.py`, enforced). Never download a whole dataset.
- **Untrusted data:** never run code that ships with a dataset, never unpickle, never `pip install` packages
  a dataset names. Samples live in temp dirs and are deleted.
- **Evidence:** listing / probe / sample record themselves in the run log; `validate.py --run-log` rejects
  claims (`method`, confirmed size, observed values) that aren't backed by a logged read.
- **Labels are computed, not added:** `.github/workflows/label.yml` runs `tools/labels.py` on every push:
  `dim:`, `org:`, `modality:`, `fmt:`, `dtype:`, `anno:`, `label-enc:`, `license:<spdx as written>`,
  `size:`, `auto-download` (size confirmed by a complete file listing, < 50 GB, open access), `enriched`,
  `license-verification-needed` (`license.spdx: unknown`) and `voxel-size-found` / `voxel-size-missing`
  (`imaging.voxel_size_nm` has x, y and, for 3D data, z), and for 3D / 3D+t `download-ready` /
  `download-not-ready` (`tools/readiness.py`; CI clones TensorSwitch for its planner). Merged PRs are relabelled from the record on
  `main` whenever records change there, so a voxel size filled in later shows up on them too.
  License labels are the SPDX id verbatim, with no interpretation.
- **Voxel size:** conversion can't proceed without it, so the enricher searches hard for it (skill step 4a:
  headers, repository APIs, archive READMEs, the full paper, code repos, upstream datasets), and makes a
  second pass over PRs enriched before that.
- **Download links:** TensorSwitch fetches files itself, so the enricher also searches hard for direct links it can
  use (skill step 4b: repository file APIs, the full landing page, zip members, the paper, code repos), checked with
  `tools/download_check.py`, for `technical.sample.urls` and, when one direct archive exists, `data.download_url`.
- **Conversion readiness:** the enricher also runs `tools/convertibility.py` (issues #518 / #519) with TensorSwitch's
  record planner, and fixes what blocks automatic conversion from evidence: concrete sample files, HDF5 dataset
  names, real globs in `path_pattern`, one organism per file set, a clear modality, `annotations.source`.
- **Auto-merge** (`tools/automerge.py`) merges PRs whose labels meet a fixed policy (3D / 3D+t PRs only when
  `download-ready`), but never a possible
  duplicate (`tools/common.py:similarity_reasons`) of a record on main or another open PR. Agents never merge,
  approve or close PRs themselves, and never add or remove the `hold` label.

## Tools (all in `tools/`, run from repo root)
| Command | Purpose |
|---|---|
| `python -m tools.sources <empiar\|zenodo\|bioimage_archive\|idr> "<query>" --limit N` | structured repository search → candidate JSON lines |
| `python tools/frontier.py next\|touch\|add --routine R` | which queries to run next; mark them searched |
| `python tools/dedup.py ...` | already in catalog / rejected? |
| `python tools/new_record.py --id ID --repository REPO --by ROUTINE` | schema skeleton ("TODO" fields must be replaced) |
| `python tools/validate.py [paths] [--run-log F] [--min-confidence 0.5]` | schema + catalog rules |
| `python tools/run_log.py start\|query\|fetched\|event\|finish` | structured run log (monitoring) |
| `python tools/publish.py state-pull\|state-push --routine R` / `dataset <file>` | restore/save search state; push one record to its own branch |
| `python tools/pr_text.py <file> --title\|--body [--run-log F]` | PR title and body for a record (use verbatim) |
| `python tools/paper.py --doi D \| --pmid P \| --title T` | find a paper and read its full text (Europe PMC / bioRxiv) |
| `python tools/peek_archive.py <zip url> [--cat member]` | list files inside a remote zip without downloading it, or print a text member |
| `python tools/listing.py <url> [<url> ...] --id ID` | full file listing → folder tree, exact total size (enricher) |
| `python tools/probe.py <url> [--glob G] --id ID` | shape / dtype / compression / voxel size from file headers (enricher) |
| `python tools/sample.py --id ID --raw S [--label S]` | download a small sample, measure it, delete it (enricher) |
| `python tools/labels.py <file>` | the PR labels a record gets |
| `python tools/download_check.py <url\|zip::member> --id ID [--tensorswitch SRC]` | is this a direct link TensorSwitch can fetch (one range request) |
| `python tools/convertibility.py [files] [--summary\|--json] [--tensorswitch SRC]` | what blocks automatic conversion (read-only report) |
| `python tools/readiness.py <file> [--tensorswitch SRC]` | download readiness: ready / not-ready / unknown, with reasons (the auto-merge gate and the download queue) |
| `python tools/download_queue.py next\|record\|list` | the download queue and the downloads list (`state/downloads.json` on `claude/state/downloader`) |
| `python tools/miao_layout.py <file> --root R --tensorswitch SRC --label-class C [--name N] [--whole]` | plan a record (sample unit, or every file with `--whole`) into the miao layout (miao#13), one crop per raw/label pair: MCP steps + label metadata, read-only |
| `python tools/miao_run.py <plan.json> [--finalize]` | run that plan crop by crop with TensorSwitch (resumable; downloads deleted per crop), or record crops the MCP already verified |
| `python tools/place.py <file>\|--all\|--pr-branches` | move records to `datasets/<dimensionality>/<modality>/` |
| `python tools/publish.py pr-pull <id>` / `pr-update <file>` | enricher: edit the record on an open PR's branch |
| `python tools/check_links.py [--oldest N] [--write]` | link rot check |
| `python tools/build_site.py` | build dashboard into `site/` |

Skills: `/find-datasets source=<repositories|literature|websearch>` (harvest), `/enrich-prs` (technical
inspection of open PRs), `/maintain-catalog` (daily upkeep), `/download-dataset <record>` (download a ready record's
sample unit, or the whole dataset, into the miao layout with the TensorSwitch MCP; output to `demo/` for now,
git-ignored). `/download-dataset next` is the unattended download agent, run hourly by cron on the workstation
(`tools/cron/download_next.sh`): one download-ready dataset per run, recorded in the downloads list.
