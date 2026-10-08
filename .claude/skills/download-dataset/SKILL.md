---
name: download-dataset
description: Download a ready catalog record (its sample unit, or with whole the full dataset, crop by crop) and convert it to OME-Zarr in the miao layout (AI-HHMI/miao#13) with TensorSwitch, verified against the source. Args are the record (path or id), optionally root=<folder> (default demo), name=<short dataset name>, label_class=<class>[,<class>...] (one per label array), organism=<NCBI name>, whole, keep-source and dry-run. With `next` instead of a record, it runs unattended: it takes the next download-ready dataset that isn't on the downloads list, downloads it, and records the outcome (the hourly cron job).
---

# download-dataset

Arguments: `$ARGUMENTS`. Parse the record (a path under `datasets/` or a record id), `root` (default
`demo`), `name`, `label_class`, `organism`, `whole`, `keep-source` and `dry-run`.
If the argument is `next`, follow **Unattended mode** at the end instead.

**Where the data goes is `root`, and only `root`:** converted crops go to `<root>/data/`, downloads in
progress to `<root>/staging/<record id>/`. `tools/miao_layout.py --root` builds every path from it.

Read `CLAUDE.md` first. Its rules still hold: **never invent values** (a label class, organism or
voxel size the record doesn't settle is asked for, not guessed), metadata comes from the record, and
nothing here edits a record or touches `main`. This skill downloads data, so in addition:
- **Scope.** By default only the record's sample unit (`technical.sample.urls`: one raw plus its labels).
  With `whole`: every file of the dataset, one crop per raw/label pair. Raw files with no label become
  raw-only crops; labels with no raw are skipped and reported. `whole` needs the
  record's data in one zip, and at most 50 GB (TensorSwitch's whole-dataset planner); folder and FTP
  datasets are not supported yet.
- **One crop at a time:** a crop's files are fetched, converted, verified, recorded and deleted before
  the next crop starts, so staging never holds more than one crop.
- Never run code that ships with a dataset, never unpickle. Downloads stay in `<root>/staging/`.
- `demo/` is git-ignored as a whole: it stays local and never goes to GitHub. Never commit converted data,
  and if you choose another `root`, keep it out of git too.

## Output layout (miao#13)
```
<root>/data/{modality}-{organism}-{name}/          e.g. demo/data/em-mouse-snemi3d/
├── manifest.json                                  crop -> record id, source files, label layers
└── crop-NNN.zarr/
    ├── raw/                                       OME-NGFF multiscales: s0 + pyramid (preset miaai)
    └── labels/{provenance}-{label_class}-{info}/  e.g. labels/manual_gt-neurite-instance/
        └── zarr.json                              label metadata (vocabulary below)
```
- `modality`: the family of the first `imaging.modality`: `em`, `lm`, `xray` or `exm`.
- `organism`: the short name from `tools/miao_layout.py:ORGANISM_SHORT` (`Mus musculus` → `mouse`), else a
  slug of the NCBI name (`Chlorocebus sabaeus` → `chlorocebus-sabaeus`).
  One organism per dataset folder; a record with several needs `organism=` per sample unit.
- `name`: short, lowercase, hyphenated: the dataset plus a region if it helps (`snemi3d`,
  `h01-cortex`). If not given, take it from the record's `short_name` / `title`, not from its id.
- **Format** (set by `tools/miao_layout.py`, don't change it): TensorSwitch preset `miaai` (zarr3, 128³ chunks
  in 512³ shards, zstd-5, C-order, source axis order and dtype kept) with `auto_multiscale`, which writes a
  full pyramid (s0, s1, …), with factors from the voxel size. Raw is downsampled with `mean` and labels with
  `mode`. Set it explicitly, because `auto` guesses from the file name, and averaging instance IDs would
  invent IDs that don't exist.
- `crop-NNN`: one per raw/label pair. The same files always get the same crop (looked up in
  `manifest.json`). New pairs get the next free numbers, in the order of the sorted sample names.
  `manifest.json` maps each crop to its record, sample name, source files and label layers.
- Label folder `{provenance}-{label_class}-{info}`:
  - `provenance` from `annotations.source`: `manual` → `manual_gt`, `proofread` → `proofread`, `automatic` → `auto_pred`
  - `info` from the encoding: `instance`, `semantic` or `points`
  - Label `zarr.json` fields (miao#13): `label_class`, `segmentation_type`, `provenance`,
    `proofreading_status`, `coverage`, `bbox` (offset, size, unit, `resolution_nm`), `source` (tool,
    annotator, model, model_version), `created`, `notes`, plus `source_record`, `dataset_doi`,
    `publication`, `license`. A field the record doesn't settle is `null` and listed in `review_needed`.
    `bbox.size` is measured from each crop's converted array when the crop is recorded.

## 0. Setup
```bash
TS_REPO=${TENSORSWITCH_REPO:-../tensorswitch}        # TensorSwitch checkout on the `unified` branch, with `pixi install` done
TS="$TS_REPO/src"
```

## 1. Is the record ready?
```bash
python tools/readiness.py <record.yaml> --tensorswitch "$TS"
```
Continue only if it says `ready` (`note:` lines don't block; they name labels the crops go without).
Otherwise stop and report the reasons. Gaps are fixed in the record, through the enricher on an open
PR or `/maintain-catalog` for records on `main`, never here.

## 2. Settle the names
Read the record in full (title, description, `annotations`, `technical.arrays`, `notes`).
- **`label_class`, one per label array, in record order.** Use the miao#13 vocabulary (`neurite`,
  `cell`, `nucleus`, `mitochondria`, `synapse`, `vesicle`, `myelin`, `blood_vessel`), or another
  short, consistent string. It must be the structure the record says is annotated (the title, the
  annotation format, `classes`). If the record doesn't say, or a label holds several classes, **ask
  the user** (AskUserQuestion) rather than pick one.
- **`organism`** when the record lists several: which one this sample unit is, per the record's notes.
  If the notes don't say, ask. `whole` doesn't support several organisms yet: the plan stops.
- **`name`**, as above.

## 3. Plan
```bash
mkdir -p <root>/staging
python tools/miao_layout.py <record.yaml> --root <root> --tensorswitch "$TS" \
    --label-class <class> [--label-class <class2> ...] --name <name> [--organism "<NCBI name>"] \
    [--whole] > <root>/staging/<id>-plan.json
```
The plan holds `dataset_dir`, `staging`, and `crops`. Each crop has its own `crop` path, `sample` name,
`files`, `labels` metadata and ordered `steps` (tool + args). The plan also holds `unpaired` / `skipped`
/ `notes` (files left out, with the reason), `review` (fields left null) and `stop`. Listing the zip
needs the network; nothing is downloaded.
- `stop` not empty: report the reasons and end. Never work around one by hand.
- Show the user the target folder, the number of crops, the label folder names, the total download
  size (`data.size_bytes` with `whole`, else `technical.sample.size_bytes`), what was left out and why,
  and the `review` list. With `whole` and more than about 10 GB, confirm with the user before you start.
- `dry-run`: stop here.

## 4. Download, convert, verify
```bash
pixi run --manifest-path "$TS_REPO/pyproject.toml" python "$PWD/tools/miao_run.py" "$PWD/<root>/staging/<id>-plan.json" [--keep-source]
```
The runner calls TensorSwitch's MCP functions in-process. Per crop, in turn: fetch its files, `convert`,
`verify_output` (`overall` must be `pass`), then add the crop to `manifest.json`, fill each label's
`bbox.size`, and delete its downloads.
- **Resumable:** crops already recorded with a passing verification are skipped, and a half-written crop
  from an interrupted run is started over. After an interruption, just run the same command again.
- **A failing crop** keeps its output and downloads. The run goes on to the next crop, then exits 1 and
  lists the failures in its summary line. Don't delete or retry blindly.
- **Over 2 GB:** a crop the plan sends to `submit_job` (the LSF cluster) is reported as failed; the queue
  doesn't offer sample units that large. For a long `whole` run, start the runner in the background and
  watch its JSON lines.

## 5. Report
Short and factual:
- the dataset folder, the crops written / already done / failed (from the runner's summary), one crop's folder tree (`find <crop> -maxdepth 3 -not -path '*/c/*'`), and the size on disk
- `verify_output`: overall per crop, plus the identity and voxel-size checks; the failing checks of any
  failed crop
- files left out (`unpaired`, `notes`) and why
- the label folder names, and each label's `review_needed` (what a person still has to fill in, e.g.
  `proofreading_status`)
- anything you asked the user and their answer (label class, organism)

## Example
`datasets/3D/ssSEM/zenodo-7142003-snemi3d-neurites-sstem.yaml` (SNEMI3D, 6 × 6 × 30 nm, mouse ssSEM):
```bash
python tools/miao_layout.py datasets/3D/ssSEM/zenodo-7142003-snemi3d-neurites-sstem.yaml \
    --root demo --tensorswitch ../tensorswitch/src --label-class neurite --name snemi3d \
    > demo/staging/zenodo-7142003-snemi3d-neurites-sstem-plan.json
```
→ `demo/data/em-mouse-snemi3d/crop-001.zarr/` with `raw/` (100 × 1024 × 1024 uint8) and
`labels/manual_gt-neurite-instance/` (uint16 instance IDs), `coverage: full_volume`. Each has levels
s0–s2 (6 → 12 → 24 nm in x/y; z stays 30 nm). `verify_output: pass` (whole-array identity for both
arrays at s0, all levels consistent). Two zip members, about 315 MB downloaded; 131 MB on disk after
conversion; about 90 s.

With `--whole`, the same record plans 2 crops: `crop-001` (train, raw + labels, already done above, so
it's skipped) and `crop-002` (the unlabeled test volume, raw only). Whole-dataset run of
`embedseg-mouse-skull-nuclei-cbg` (`--label-class nucleus --name embedseg-mouse-skull --whole`):
`demo/data/lm-mouse-embedseg-mouse-skull/` with 3 crops (train X1, train X2_left, test X2_right), each
raw + `labels/manual_gt-nucleus-instance/`, all `pass`, about 26 s.

## Unattended mode (`next`)
The hourly cron job (`tools/cron/download_next.sh`) runs `claude -p "/download-dataset next"`. Nobody is
there to answer, so this mode **never asks**. It converts **one dataset** per run, then stops.
The cron job sets `$TENSORSWITCH_SRC` and `$TENSORSWITCH_REPO`; use them instead of `$TS` / `$TS_REPO`
(no `export` or variable assignments: the allowed tools are `python tools/…`, `pixi run --manifest-path …`,
`mkdir -p`, `ls`, `du`, `find`, `cat`, and Read / Glob / Grep).
```bash
python tools/publish.py state-pull --routine downloader          # restores state/downloads.json
python tools/download_queue.py next --root <root> --max-gb 10     # the next download-ready dataset, smallest first
```
- `"next": null` means nothing is left. Report that and stop.
- Otherwise work on that record, steps 2–5 above, with these changes:
  - **Names.** Settle `label_class`, `organism` and `name` from the record alone. If the label class
    isn't stated clearly (title, `annotations.format`, `classes`), or a label holds several classes,
    don't guess: record `skipped` with the reason, then go on to the next record from `next` (one
    record a run still holds; skipping doesn't count as the run's record).
  - **Scope.** Plan with `--whole` when `mode` is `whole`, else the sample unit.
  - **Planning.** Write the plan with `--out <root>/staging/<id>-plan.json` (no shell redirects in this mode).
  - **Running.** Use the runner (step 4) with `--summary-out <root>/staging/<id>-summary.json`:
    `pixi run --manifest-path "$TENSORSWITCH_REPO/pyproject.toml" python "$PWD/tools/miao_run.py" <plan> --summary-out <summary>`
- **Record the outcome**, always, then save the list:
  ```bash
  python tools/download_queue.py record <id> downloaded --plan <plan> --summary <summary>   # runner exit 0
  python tools/download_queue.py record <id> failed --reason "<failed crops, first check>" --plan <plan> --summary <summary>
  python tools/download_queue.py record <id> skipped --reason "<why>"                      # e.g. plan stop reasons
  python tools/publish.py state-push --routine downloader
  ```
  A record that failed twice isn't offered again; `skipped` is permanent until someone removes the entry.
- Never edit a record, never push to `main`. The only thing pushed is the downloads list, to
  the `state` branch.
- End with one short line: what was downloaded (crops, size on disk) or why nothing was.
