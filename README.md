# Agentic Harvesting of Open-Source Microscopy Data

AI agents find **usable microscopy training datasets**, inspect their files, and propose each one as a
pull request. The repo is the catalog: one YAML record per dataset in
`datasets/<dimensionality>/<modality>/<id>.yaml`, checked against `schema/dataset.schema.json`.

**Dashboard:** https://ai-hhmi.github.io/mia-agentic-search/ · **Alerts:** Slack `#mia-harvester`

## The pipeline
1. **Find.** Three hourly harvest routines search repositories (EMPIAR, BioImage Archive, Zenodo, IDR),
   papers, and the web. Each new dataset becomes one PR (`tools/publish.py dataset`), which refuses
   anything already on `main`, in an open PR, or rejected.
2. **Inspect.** The hourly enricher reads each PR's file listing, headers and a small sample, and fills
   in the record's `technical` block: size, shapes, dtypes, labels, voxel size, download links.
3. **Label.** On every push, CI sets the PR's labels from its record (`tools/labels.py`), including
   `download-ready` / `download-not-ready` (`tools/readiness.py`).
4. **Merge.** If the PR is enriched, has a known license and known formats, and isn't a possible
   duplicate, GitHub auto-merges it once `validate` passes (`tools/automerge.py`). Anything else waits
   for a human.
5. **Download.** A cron job on the workstation (`tools/cron/download_next.sh`) takes the next
   `download-ready` dataset and converts it to OME-Zarr in the miao layout with TensorSwitch.

A daily maintainer routine checks links, fills gaps in records, and posts a Slack digest. A watchdog
alerts when a routine stops running or keeps failing.

## Reviewing
- **Merge** a PR to accept it. **Close** it to reject it; a closed dataset is never proposed again.
- **Edit the YAML** in the PR to fix a field. Agents never overwrite a field you changed.
- Label **`hold`** to stop auto-merge; label **`not-duplicate`** to clear a false duplicate flag.

## Layout
| Path | Contents |
|---|---|
| `datasets/` | the catalog |
| `CLAUDE.md` | rules for the agents, including what counts as "usable" |
| `.claude/skills/` | `find-datasets`, `enrich-prs`, `maintain-catalog`, `download-dataset` |
| `tools/` | validation, dedup, inspection, labels, merge policy, publishing, download, dashboard |
| `state/rejected.yaml` | hand-curated rejections (closed PRs are rejected automatically) |

Branches: `main` (protected) · `claude/dataset/<id>` (one per proposed dataset) · `state` (search state,
run logs, downloads list).

## Local use
```bash
pip install -r requirements.txt
python tools/validate.py                                   # check the catalog
python tools/readiness.py --summary                        # what blocks downloads (needs $TENSORSWITCH_SRC)
claude -p "/find-datasets source=repositories target=1 dry-run"
```
Settings: repo variables `AUTO_MERGE=true` (merging on) and `PAUSED` (silences the watchdog), secret
`SLACK_WEBHOOK_URL`; repo settings *Allow auto-merge* and *Automatically delete head branches* on.
