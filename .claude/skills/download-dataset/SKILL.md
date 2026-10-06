---
name: download-dataset
description: Download a ready catalog record's sample unit and convert it to OME-Zarr in the miao layout (AI-HHMI/miao#13) with the TensorSwitch MCP, verified against the source. Args are the record (path or id), optionally root=<folder> (default demo), name=<short dataset name>, label_class=<class>[,<class>...] (one per label array), organism=<NCBI name>, keep-source and dry-run.
---

# download-dataset

Arguments: `$ARGUMENTS`. Parse the record (a path under `datasets/` or a record id), `root` (default
`demo`), `name`, `label_class`, `organism`, `keep-source` and `dry-run`.

Read `CLAUDE.md` first. Its rules still hold: **never invent values** (a label class, organism or
voxel size the record doesn't settle is asked for, not guessed), metadata comes from the record, and
nothing here edits a record or touches `main`. This skill downloads data, so in addition:
- Only the record's **sample unit** (`technical.sample.urls`: one raw plus its labels) is downloaded.
  Whole datasets are out of scope for now.
- Never run code that ships with a dataset, never unpickle. Downloads stay in `<root>/staging/`.
- `demo/data/` and `demo/staging/` are git-ignored. Never commit converted data.

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
- `organism`: the short name from `tools/miao_layout.py:ORGANISM_SHORT` (`Mus musculus` → `mouse`).
  One organism per dataset folder; a record with several needs `organism=` per sample unit.
- `name`: short, lowercase, hyphenated: the dataset plus a region if it helps (`snemi3d`,
  `h01-cortex`). If not given, take it from the record's `short_name` / `title`, not from its id.
- **Format** (set by `tools/miao_layout.py`, don't change it): TensorSwitch preset `miaai` (zarr3, 128³ chunks
  in 512³ shards, zstd-5, C-order, source axis order and dtype kept) with `auto_multiscale`, which writes a
  full pyramid (s0, s1, …), with factors from the voxel size. Raw is downsampled with `mean` and labels with
  `mode`. Set it explicitly, because `auto` guesses from the file name, and averaging instance IDs would
  invent IDs that don't exist.
- `crop-NNN`: the record's existing crop if it was converted before (from `manifest.json`), else the
  next free number.
- Label folder `{provenance}-{label_class}-{info}`:
  - `provenance` from `annotations.source`: `manual` → `manual_gt`, `proofread` → `proofread`, `automatic` → `auto_pred`
  - `info` from the encoding: `instance`, `semantic` or `points`
  - Label `zarr.json` fields (miao#13): `label_class`, `segmentation_type`, `provenance`,
    `proofreading_status`, `coverage`, `bbox` (offset, size, unit, `resolution_nm`), `source` (tool,
    annotator, model, model_version), `created`, `notes`, plus `source_record`, `dataset_doi`,
    `publication`, `license`. A field the record doesn't settle is `null` and listed in `review_needed`.

## 0. Setup
```bash
TS_REPO=${TENSORSWITCH_REPO:-../tensorswitch}        # TensorSwitch checkout on the `unified` branch, with `pixi install` done
TS="$TS_REPO/src"
```
Check the TensorSwitch MCP tools are available (`fetch_dataset`, `convert`, `verify_output`; look them
up with ToolSearch "tensorswitch"). If they aren't, tell the user they can register it:
```bash
claude mcp add --transport stdio tensorswitch -- pixi run --manifest-path "$TS_REPO/pyproject.toml" python -m tensorswitch_v2.mcp_server
```
and continue with the **runner** (step 4b), which calls the same functions.

## 1. Is the record ready?
```bash
python tools/convertibility.py <record.yaml> --tensorswitch "$TS"
```
Continue only if it reports **no needs** and `tensorswitch: ready`. Otherwise stop, and report the
needs and planner warnings to the user. Gaps are fixed in the record, through the enricher on an open
PR or `/maintain-catalog` for records on `main`, never here.

## 2. Settle the names
Read the record in full (title, description, `annotations`, `technical.arrays`, `notes`).
- **`label_class`, one per label array, in record order.** Use the miao#13 vocabulary (`neurite`,
  `cell`, `nucleus`, `mitochondria`, `synapse`, `vesicle`, `myelin`, `blood_vessel`), or another
  short, consistent string. It must be the structure the record says is annotated (the title, the
  annotation format, `classes`). If the record doesn't say, or a label holds several classes, **ask
  the user** (AskUserQuestion) rather than pick one.
- **`organism`** when the record lists several: which one this sample unit is, per the record's notes.
  If the notes don't say, ask.
- **`name`**, as above.

## 3. Plan
```bash
mkdir -p <root>/staging
python tools/miao_layout.py <record.yaml> --root <root> --tensorswitch "$TS" \
    --label-class <class> [--label-class <class2> ...] --name <name> [--organism "<NCBI name>"] \
    > <root>/staging/<id>-plan.json
```
The plan holds `dataset_dir`, `crop`, the ordered `steps` (tool + args), the label metadata in
`labels`, `review` (fields left null) and `stop`.
- `stop` not empty: report the reasons and end. Never work around one by hand.
- Show the user the target folder, the label folder names, the files to download with their sizes
  (`technical.sample.size_bytes`), and the `review` list.
- `dry-run`: stop here.

## 4. Download, convert, verify
**a. With the MCP tools.** Run `steps` in order, passing each step's `args` **verbatim**:
- `fetch_dataset`: for a sample over ~300 MB add `background=True`, and call again with the same
  arguments until `status` is `success`. A cut-off download resumes.
- `convert`: must return `status: success`. A step the plan gives as `submit_job` (sample over 2 GB)
  needs the user's LSF `project`; follow it with `check_job_status` until it is done.
- `verify_output`: `overall` must be `pass`. `fail` or `unverified` means stop: keep the output and
  the downloads, and report the failing checks. Don't delete or retry blindly.

Then record the crop and clean up:
```bash
python tools/miao_run.py <root>/staging/<id>-plan.json --finalize [--keep-source]
```
It refuses unless the crop's `verification.json` says `pass`. It then adds the crop to `manifest.json`
and deletes `<root>/staging/<id>/`.

**b. Without the MCP (runner).** Same steps, same checks, one command:
```bash
pixi run --manifest-path "$TS_REPO/pyproject.toml" python "$PWD/tools/miao_run.py" "$PWD/<root>/staging/<id>-plan.json" [--keep-source]
```
It stops at the first step that doesn't succeed and finalizes only after `verify_output` passes.
The runner downloads in the foreground: for samples over 2 GB use the MCP path (a) instead.

## 5. Report
Short and factual:
- the crop path, its folder tree (`find <crop> -maxdepth 3 -not -path '*/c/*'`), and its size on disk
- `verify_output`: overall, plus the identity and voxel-size checks
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
