# Agentic Harvesting of Open-Source Microscopy Data

AI agents search dataset repositories, papers and the web for **usable microscopy training
datasets**. They propose each one as a pull request, and a human decides what goes into the catalog.

**Dashboard:** https://ai-hhmi.github.io/mia-agentic-search/ · **Alerts:** Slack `#mia-harvester`

## How it works
Four [Claude scheduled routines](https://claude.ai/code/routines) run in the cloud:

| Routine | Schedule | Searches |
|---|---|---|
| `harvest-repositories` | hourly | EMPIAR, BioImage Archive, Zenodo, IDR (via their APIs) |
| `harvest-literature` | every 3 h | Papers that release datasets (bioRxiv, PubMed, journals, challenges) |
| `harvest-websearch` | every 3 h | Portals, challenge sites, Hugging Face, Kaggle, lab pages |
| `maintainer` | daily | Checks links, fills gaps in existing records, posts a daily Slack digest |

Each harvest run works through its search queue until it finds **at least one dataset that isn't
already in the catalog, in an open PR, or rejected**. Runs last at most 2 hours. Before writing
any field as unknown, the agent reads the dataset page, the file listing and the paper's full text.

## Reviewing datasets
Every dataset arrives as **its own PR**, labeled `new-datasets` and titled with a short name, e.g.
*CryoVesNet synaptic vesicles (cryo-ET)*. The PR links to the dataset, the download and the paper,
and has a table with modality, dimensionality, voxel size, data format, size, sample, annotations,
ML tasks and license.

- **Merge:** accept the dataset into the catalog.
- **Close:** reject it. It will never be proposed again.
- **Edit the YAML file in the PR:** fix a field before merging.

## Repository layout
| Path | Contents |
|---|---|
| `datasets/<repository>/<id>.yaml` | The catalog: one record per dataset |
| `schema/dataset.schema.json` | Record schema. CI rejects records that don't match it |
| `CLAUDE.md` | Rules for the agents, including what counts as a "usable" dataset |
| `.claude/skills/` | The agents' procedures: `find-datasets`, `maintain-catalog` |
| `tools/` | Python helpers: validation, dedup, paper reader, PR text, publishing, dashboard, watchdog |

Branches:
- **`main`:** the catalog. It's protected, so agents can only propose changes through PRs.
- **`claude/dataset/<id>`:** one branch per proposed dataset. It's deleted when its PR is closed.
- **`claude/state/<routine>`:** each agent's search progress and run logs. Don't delete these.
- **`rejections`:** the list of rejected datasets. Don't merge it into `main`.

## Monitoring
- **Dashboard:** health of each routine, runs and their outcomes, catalog statistics, and a searchable table.
- **Slack:**
  - failed runs and notable finds,
  - the daily digest,
  - watchdog alerts when a routine stops running or finds nothing for 3 runs in a row.
- **Run transcripts:** every agent session is at claude.ai/code/routines.

## Setup (already done for this repo)
1. **GitHub:**
   - Enable Pages (Settings → Pages → Source: GitHub Actions).
   - Create the labels `new-datasets` and `maintenance`.
   - Protect `main` with a ruleset: require a PR and the `validate` check; give Repository admin bypass.
2. **Slack webhook**, for the watchdog and CI alerts:
   - Create a Slack app (Blank app) at https://api.slack.com/apps and enable **Incoming Webhooks** for the channel.
   - Save the webhook URL as the repo secret `SLACK_WEBHOOK_URL`.
3. **Slack connector**, for the agents: claude.ai → Settings → Connectors → Slack. Then run
   `/invite @Claude` in the channel.
4. **Routines:**
   - Create the four routines in the table above on this repo. The names must match exactly.
   - Prompts: `/find-datasets source=repositories|literature|websearch` and `/maintain-catalog`.
   - Enable the Slack connector on each.
   - Set the environment's network access to **Full**.
   - Optional setup script: `pip install pyyaml jsonschema requests`.

## Local use
```bash
pip install -r requirements.txt
python tools/validate.py                                        # check the catalog
python tools/build_site.py && open site/index.html              # build the dashboard
claude -p "/find-datasets source=repositories limit=1 dry-run"  # test an agent run (no PRs)
```
