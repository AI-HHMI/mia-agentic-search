# mia-agentic-search

An agent-maintained catalog of **usable microscopy training datasets**. The git repo *is* the
database: one YAML record per dataset in `datasets/<repository-lowercase>/<id>.yaml`, validated
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
4. **Dedup before writing:** run `python tools/dedup.py --doi ... --repository ... --accession ... --url ... --title ...`.
   `duplicate`/`pending`/`rejected` → skip and log it. `possible-duplicate` → open both pages and decide.
5. **Validate before pushing:** `python tools/validate.py <your files> --run-log <run log> --min-confidence 0.5` must pass.
6. Never edit or delete records on `main` from a harvest run. Corrections belong to the maintainer routine.
7. Never push to `main`. **One dataset per PR:** publish each record with `tools/publish.py dataset`
   (branch `claude/dataset/<id>`), and use `tools/pr_text.py` for the PR title and body, verbatim.
   Search state goes to `claude/state/<routine>` via `tools/publish.py state-push`.
8. Slack notifications go to **`#mia-harvester`** via the Slack connector. Don't post to any other channel.
9. Be polite to servers: ≤ 1 request/second per host, no bulk downloads. Metadata only.

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
- `repository`: where the data is *hosted*. A dataset on its own website → `LabWebsite` or `other`.
- `voxel_size_nm`: in **nanometres**, so 0.116 µm → 116. For 2D data, `z: null`.
- `organism`: NCBI scientific names (`Mus musculus`, not "mouse").
- `license.spdx`: SPDX IDs (`CC-BY-4.0`, `CC0-1.0`, `CC-BY-NC-4.0`, `MIT`), `custom` or `unknown`.
- `short_name`: ≤ 50 characters naming the content and modality; it's the PR title (`Add dataset: <short_name>`).
- Timestamps are UTC ISO-8601 with `Z`.

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
| `python tools/peek_archive.py <zip url>` | list files inside a remote zip without downloading it |
| `python tools/check_links.py [--oldest N] [--write]` | link rot check |
| `python tools/build_site.py` | build dashboard into `site/` |

Skills: `/find-datasets source=<repositories|literature|websearch>` (harvest), `/maintain-catalog` (daily upkeep).
