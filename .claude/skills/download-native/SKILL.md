---
name: download-native
description: Download a catalog record's sample unit (or chosen file sets) and write it into the miao layout (AI-HHMI/miao#13) without TensorSwitch, with tools/native.py. The agent inspects the files and settles their axes and transforms itself, then checks the result numerically (identity, pyramid, grid, label sanity, raw/label alignment against flips, transposes and shifts) and visually (it looks at overlay slices and judges whether the labels sit on the structures). Args are the record (path or id), optionally root=<folder> (default demo-native), name=<short dataset name>, label_class=<class>[,<class>...] (one per label array), organism=<NCBI name>, files=<url1|url2>;<url3|url4> (explicit crops instead of the sample unit), keep-source and dry-run. With `next` instead of a record, it runs unattended on its own downloads list.
---

# download-native

Arguments: `$ARGUMENTS`. Parse the record (a path under `datasets/` or a record id), `root` (default
`demo-native`), `name`, `label_class`, `organism`, `files`, `keep-source` and `dry-run`.
If the argument is `next`, follow **Unattended mode** at the end instead.

This is the second download agent. It writes the same layout and format as `/download-dataset` but does the
conversion itself (`tools/native.py`, `tools/native_io.py`, `tools/native_qc.py`; plain Python, no TensorSwitch),
and it doesn't trust a conversion until the numbers **and** a look at the data agree.

Read `CLAUDE.md` first. Its rules still hold: **never invent values** (a label class, organism or axis order
the files and the record don't settle is asked for, not guessed), metadata comes from the record, nothing here
edits a record or touches `main`. In addition:
- **Where the data goes is `root`, and only `root`:** `<root>/data/` for crops, `<root>/staging/<id>/` for
  downloads and the spec. `root` stays out of git (`demo-native/` is git-ignored).
- **One crop at a time:** fetch, convert, check, review, finalize one crop before the next. Staging never holds
  more than one crop's files.
- **Untrusted data:** never run code that ships with a dataset, never unpickle, never `pip install` what it names.
  The readers only parse array formats (TIFF, HDF5, MRC, NIfTI). Don't write ad-hoc readers.
- **Transforms need evidence.** The TIFF Orientation tag is applied automatically. Any other flip, channel
  selection or `orientation=ignore` needs a `reason=` that cites file metadata or the dataset's documentation.
  **Never transform labels to make an alignment check pass.** A misalignment you can't explain from the files
  is a finding to report (and a failed crop), not something to fix by hand.

## Output layout (miao#13)
```
<root>/data/{modality}-{organism}-{name}/          e.g. demo-native/data/lm-arabidopsis-go-nuclear-ovule/
├── manifest.json                                  crop -> record, sample, source files, label layers, converter
└── crop-NNN.zarr/
    ├── raw/                                       OME-NGFF 0.5 multiscales s0, s1, … (mean)
    ├── labels/{provenance}-{label_class}-{info}/  same, mode; zarr.json holds the miao#13 label metadata
    ├── conversion.json                            what was done to each array (axes, orientation, transforms)
    ├── verification.json                          the numeric checks (tools/native_qc.py)
    └── qc/                                        metrics.json, overlay-xy.png, overlay-ortho.png, review.json
```
Folder names, label keys and label metadata come from the same code as `/download-dataset`
(`tools/miao_layout.py`): `modality` from the first `imaging.modality`, `organism` from `ORGANISM_SHORT`,
`name` short and hyphenated (default from `short_name` / `title`), label folder `{provenance}-{class}-{info}`.
Format: zarr v3, 128³ chunks in 512³ shards (channel axis 1), zstd 5, dtype kept. One difference from
TensorSwitch: **every array is written in canonical order** (`c, z, y, x` for raw, `c` only with more than one
channel; `z, y, x` for labels), so raw and labels always share axis order.

## 1. Plan
Settle `label_class` (one per label array, in record order; miao#13 vocabulary `neurite`, `cell`, `nucleus`,
`mitochondria`, `synapse`, `vesicle`, `myelin`, `blood_vessel`, or another short string the record states),
`organism` (if the record lists several) and `name`. If the record doesn't settle the label class, **ask**.
```bash
python tools/native.py plan <record.yaml> --root <root> --label-class <class> [--label-class …] --name <name> \
    [--organism "<NCBI name>"] [--crop-files "<url1>|<url2>" …] --out <root>/staging/<id>-spec.json
```
- `stop` not empty: report the reasons and end.
- `notes`: a file that matches no record array needs its role: `set … <index> role=raw|label`.
- Without `files=` the crop is the record's sample unit (`technical.sample.urls`). With `files=`, each
  `url|url` group is one crop (list a zip with `python tools/peek_archive.py <zip url>` to find the pairs).
- Show the user the dataset folder, the crops, the label keys and the `review` list. `dry-run` stops here.

## 2. Fetch and inspect
```bash
python tools/native.py fetch <spec> --crop crop-001.zarr
python tools/native.py inspect <spec> --crop crop-001.zarr
```
`inspect` prints, per file: tifffile's axes (`axes_in_file`), shape, dtype, the TIFF Orientation, samples per
pixel and whether those samples are identical, ImageJ metadata, the voxel size stored in the file, HDF5 dataset
names, value range, and the record's `axes` / `shape` / `dtype` for comparison.

## 3. Decide each array's axes and transforms
For every array, set the axes **of the array as read** (one letter per dimension, from `c z y x`; `s` counts as
`c`) and, for HDF5/zarr, the dataset:
```bash
python tools/native.py set <spec> crop-001.zarr raw axes=zyx reason="tifffile zyx, 263 ImageJ slices, record zyx"
python tools/native.py set <spec> crop-001.zarr label axes=yxz reason="50 samples per pixel = the 50 z slices"
python tools/native.py set <spec> crop-001.zarr raw dataset=volumes/raw axes=zyx reason="…"
```
How to decide, in this order of evidence:
1. **The file's own metadata:** ImageJ `slices`/`channels`/`frames`, OME axes, tifffile's axes for shaped files,
   HDF5 attributes, MRC = `zyx`, NIfTI = `xyz`.
2. **The record:** `technical.arrays[].axes` and `shape` (written by the enricher from the same files).
3. **Consistency between arrays:** the label's z/y/x must match the raw's. A plain multi-page TIFF (`i`) is z when
   its page count matches the raw's z, or the record says so.
4. tifffile's `i` (plain pages) and `q` are guesses: settle them from 1–3. If nothing settles an axis, **ask**
   (unattended: record `skipped`).
Then, only with evidence:
- `transform=select:c:0` when the samples are identical (grey stored as RGB; `inspect` says `samples_identical`).
- `transform=flip:<axis>` only when documentation or metadata says the data is stored mirrored.
- `orientation=ignore` only when the dataset documents that its Orientation tag is wrong.
- `dtype=uint16` for float labels whose values are whole numbers.
Every `set` is logged in the crop's `decisions` and copied into `conversion.json`.

## 4. Convert and check
```bash
python tools/native.py convert <spec> --crop crop-001.zarr
python tools/native.py check <spec> --crop crop-001.zarr      # verify.json + qc/metrics.json + overlays
```
Numeric checks (each pass / fail / unverified):
- `identity:*`: s0 equals the source read again with the same settings. `pyramid:*`: level shapes and a
  recomputed block. `voxel_size`: s0 scale equals the record. `grid:*`: label z/y/x equals raw z/y/x.
- `labels:*`: integer dtype, number of IDs, foreground between 0.05 % and 98 %.
- `alignment:*`: how well the labels fit the image as stored vs. flipped (z, y, x), rotated 180°, transposed and
  shifted (≤ 10 %). `fail`: a wrong transform fits clearly better. `unverified`: the image says little, or a
  wrong transform fits about as well; then the visual review decides.

If a check fails, **don't patch it away**: first re-read the evidence for your axes decisions. If they were
wrong, fix them with `set` and convert again. If they were right, the crop has failed: stop with this crop and
report what failed, with the numbers (e.g. "labels fit better after 'flip y', gain 3.5; no Orientation tag, no
documentation: data or record problem").

## 5. Visual review (look at the data)
Read both images with the Read tool: `<crop>/qc/overlay-xy.png` (five xy slices through z) and
`<crop>/qc/overlay-ortho.png` (xz and yz through the middle, at physical aspect). Each row shows the raw, the raw
with label outlines, and the labels **flipped in y on purpose**, so you can see what misalignment looks like here.
Answer these for each image, then decide:
1. Do the outlines sit on the structures they claim to be (`label_class`): nuclei on nuclei, cells on cells,
   neurites along membranes? Do they follow the edges, rather than cutting across them?
2. Is the as-stored column clearly better than the flipped column? (If both look equally plausible, say so.)
3. Is anything systematically off: all labels displaced the same way, labels in empty background, a z range
   with labels but no signal (or the reverse), labels on a different channel's structures?
4. Does the coverage match the record (`coverage: dense` should label most visible structures in the slice)?

Record it, with what you saw per image:
```bash
python tools/native.py review <spec> crop-001.zarr --verdict pass|fail|unsure \
    --reason "<one sentence>" --saw "<overlay-xy: …; overlay-ortho: …>"
```
`pass` only if 1–3 are clearly yes / clearly no problem. `unsure` when you can't tell (low contrast, labels too
small to see): the crop is then not recorded, and it is reported for a person to look at.

## 6. Finalize, then report
```bash
python tools/native.py finalize <spec> [--keep-source] --summary-out <root>/staging/<id>-summary.json
```
It records each crop whose verification passed (or was unverified **only** on alignment) **and** whose review
is `pass` in `manifest.json`, fills each label's `bbox.size`, and deletes the crop's downloads. Other crops keep
their output and downloads for inspection; it exits 1 and names them.

Report, short and factual: the dataset folder and crops written / failed; per crop the axes and transforms you
set and why; the numeric checks (alignment scores as stored vs. the best wrong transform); your visual verdict and
what you saw; files left out; each label's `review_needed`; anything you asked the user.

## Unattended mode (`next`)
The cron job (`tools/cron/download_native_next.sh`) runs `claude -p "/download-native next root=<root>"`.
Nobody is there to answer, so this mode **never asks**, and it converts **one dataset** per run. Allowed tools:
`python tools/…`, `mkdir -p`, `ls`, `du`, `find`, `cat`, Read / Glob / Grep (no variable assignments or shell
redirects; write files only with the tools' `--out` / `--summary-out`).
```bash
python tools/publish.py state-pull --routine downloader-native               # restores state/downloads-native.json
python tools/download_queue.py --agent native next --root <root> --max-gb 5  # smallest dataset not on the list
```
- `"next": null`: report that and stop.
- Otherwise steps 1–6 for that record, sample unit only, with these changes:
  - **Names:** settle `label_class`, `organism` and `name` from the record alone. If the label class isn't
    stated clearly (title, `annotations.format`, `classes`), or a label holds several classes, record `skipped`
    and take the next record (skipping doesn't count as the run's dataset).
  - **Axes:** if the evidence in step 3 doesn't settle an axis, record `skipped` with what was ambiguous.
  - **Review:** `unsure` counts as failed.
- **Record the outcome**, always, then save the list:
  ```bash
  python tools/download_queue.py --agent native record <id> downloaded --plan <spec> --summary <summary>   # finalize exit 0
  python tools/download_queue.py --agent native record <id> failed --reason "<crop: first failing check or review>" --plan <spec> --summary <summary>
  python tools/download_queue.py --agent native record <id> skipped --reason "<why>"
  python tools/publish.py state-push --routine downloader-native
  ```
  A record that failed twice isn't offered again; `skipped` is permanent until someone removes the entry.
- Never edit a record, never push to `main`. The only thing pushed is the downloads list.
- End with one short line: what was downloaded (crops, size on disk, alignment + visual verdict) or why nothing was.
