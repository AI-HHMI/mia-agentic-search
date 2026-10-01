# Agentic Harvesting of Open-Source Microscopy Data

AI agents find **usable microscopy training datasets** in repositories, papers and on the web,
inspect their files, and propose each one as a pull request. PRs that meet a fixed policy are
merged automatically; the rest wait for a human.

**Dashboard:** https://ai-hhmi.github.io/mia-agentic-search/ · **Alerts:** Slack `#mia-harvester`

## How it works
Five [Claude routines](https://claude.ai/code/routines) run in the cloud:

| Routine | Runs (UTC) | Does |
|---|---|---|
| `harvest-repositories` | hourly at :36 | Searches EMPIAR, BioImage Archive, Zenodo, IDR |
| `harvest-literature` | hourly at :10 | Searches papers that release data (bioRxiv, PubMed, journals, challenges) |
| `harvest-websearch` | hourly at :11 | Searches portals, challenge sites, Hugging Face, Kaggle, lab pages |
| `enricher` | hourly at :45 | Inspects the files of open dataset PRs and fills in the technical details |
| `maintainer` | daily at 12:00 | Checks links, fills gaps in existing records, posts a Slack digest |

1. **Harvest.** Each run publishes up to **10 new datasets**, each as its own PR, skipping anything
   already in the catalog, in an open PR, rejected, or a likely re-deposit of the same data.
2. **Enrich.** The enricher reads file listings, headers and a small sample (≤ 500 MB) of each
   dataset: folder tree, exact size, shapes, dtypes, compression, value ranges, label encoding,
   raw ↔ label alignment and license. The PR gets a *Technical inspection* section and a report comment.
3. **Label.** A workflow sets the PR's labels from its record on every push (see below).
4. **Merge.** Auto-merge merges PRs that meet the policy; everything else stays open for review.

## Reviewing PRs
- **Merge** to accept, **close** to reject (it won't be proposed again).
- **Edit the YAML** in the PR to fix a field first; agents never overwrite a field you changed.
- Add the label **`hold`** to stop auto-merge from merging a PR.

**Labels** (computed by `tools/labels.py`, never set by hand): `dim:` · `org:` · `modality:` · `fmt:` ·
`dtype:` · `anno:` · `label-enc:` · `license:<SPDX id>` · `size:` · `auto-download` (confirmed size < 50 GB,
open access) · `enriched` · `license-verification-needed` (no license found).

**Auto-merge** (`tools/automerge.py`, runs after each labelling and hourly; on while the repo
variable `AUTO_MERGE` is `true`) merges a PR when all of these hold:
- `enriched`, with a dimensionality (2D, 2D+t, 3D or 3D+t);
- size below 500 GB (not `size:unknown`);
- a known license (not `license:unknown`);
- only known formats (no `fmt:other`);
- `validate` passed, not a draft, no `hold`;
- not a possible duplicate of a record on `main` or another open PR.

The dashboard lists every open PR that isn't merged, grouped by reason.

## Repository
| Path | Contents |
|---|---|
| `datasets/<dimensionality>/<modality>/<id>.yaml` | The catalog, one record per dataset, e.g. `datasets/3D/FIB-SEM/cremi.yaml` |
| `schema/dataset.schema.json` | Record schema (v1.2; `technical` holds the enricher's findings) |
| `CLAUDE.md` | Rules for the agents, including what counts as "usable" |
| `.claude/skills/` | Agent procedures: `find-datasets`, `enrich-prs`, `maintain-catalog` |
| `tools/` | Validation, dedup, file inspection, labels, auto-merge, publishing, dashboard |
| `state/rejected.yaml` | Datasets that must not be proposed again |

Branches: `main` (protected catalog) · `claude/dataset/<id>` (one per proposed dataset, deleted when
its PR closes) · `claude/state/<routine>` (search state and run logs) · `rejections` (closed PRs).

## Monitoring
- **Dashboard:** catalog by dimensionality (modality and organism), new records per day, why open
  PRs aren't merged, routine health and recent runs. Rebuilt hourly and after every merge.
- **Slack:** failed runs, the daily digest, and watchdog alerts when a routine goes stale.
- **Transcripts:** every agent session at claude.ai/code/routines.

## Setup (done for this repo)
1. **GitHub:** Pages from GitHub Actions; a ruleset on `main` requiring a PR and the `validate` check;
   repo variable `AUTO_MERGE=true`; secret `SLACK_WEBHOOK_URL` (Slack incoming webhook).
2. **Slack connector** for the agents (claude.ai → Settings → Connectors), then `/invite @Claude` in the channel.
3. **Routines** as in the table, with prompts `/find-datasets source=repositories|literature|websearch`,
   `/enrich-prs` and `/maintain-catalog`; Slack connector enabled; network access **Full**.
   Harvesters use Sonnet, enricher and maintainer Opus.

## Local use
```bash
pip install -r requirements.txt                  # + requirements-inspect.txt for the enricher tools
python tools/validate.py                         # check the catalog
python tools/build_site.py                       # build the dashboard into site/
python tools/automerge.py                        # which open PRs would auto-merge, and why not (no merging)
claude -p "/find-datasets source=repositories target=1 dry-run"   # test a harvest run (no PRs)
claude -p "/enrich-prs pr=126 dry-run"                            # test the enricher on one PR (no pushes)
```
