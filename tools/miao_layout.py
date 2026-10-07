"""Plan the download + conversion of a record into the miao layout (AI-HHMI/miao#13). Read-only.

    python tools/miao_layout.py <record.yaml> --root demo --tensorswitch ../tensorswitch/src \\
        --label-class neurite [--name snemi3d] [--organism "Mus musculus"]

Asks TensorSwitch's record planner for the record's sample unit (one raw + its labels), then rewrites
its steps into the layout agreed in miao#13:

    <root>/data/{modality}-{organism}-{name}/
    └── crop-NNN.zarr/
        ├── raw/                                   OME-NGFF multiscales (preset miaai)
        └── labels/{provenance}-{label_class}-{info}/zarr.json   label metadata (miao#13 vocabulary)

Prints one JSON object: the target folders, the ordered MCP steps (fetch_dataset / convert /
verify_output, ready to run as given), the label metadata written with each label, `review` (fields
left null because the record doesn't settle them) and `stop` (reasons not to run at all). Nothing is
downloaded or written. The tool never guesses: an organism, modality family or label class it can't
take from the record or the flags is a `stop`.
"""
import argparse
import copy
import datetime
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import load_yaml  # noqa: E402
from tools.convertibility import FAMILIES, _planner, family, input_axes_supported, tiff_axes_needs  # noqa: E402

# NCBI scientific name -> short folder name. Extend as records need it; an organism not listed is a stop.
ORGANISM_SHORT = {
    "Homo sapiens": "human", "Mus musculus": "mouse", "Rattus norvegicus": "rat",
    "Drosophila melanogaster": "fly", "Caenorhabditis elegans": "worm", "Danio rerio": "zebrafish",
    "Platynereis dumerilii": "platynereis", "Arabidopsis thaliana": "arabidopsis", "Gallus gallus": "chicken",
    "Macaca mulatta": "macaque", "Saccharomyces cerevisiae": "yeast", "Escherichia coli": "ecoli",
    "Trypanosoma brucei": "trypanosoma", "Xenopus laevis": "xenopus", "Sus scrofa": "pig",
    "Bos taurus": "cow", "Chlamydomonas reinhardtii": "chlamydomonas", "Tribolium castaneum": "tribolium",
    "Schmidtea mediterranea": "planaria", "Vibrio cholerae": "vibrio",
}
# miao#13 controlled vocabularies, from the record's fields
SEGMENTATION_TYPE = {"instance-ids": "instance", "semantic-ids": "semantic", "binary": "semantic", "points-table": "point"}
PROVENANCE = {"manual": "manual_gt", "proofread": "proofread", "automatic": "auto_pred"}
LABEL_CLASSES = ("neurite", "cell", "nucleus", "mitochondria", "synapse", "vesicle", "myelin", "blood_vessel")
CROP = re.compile(r"^crop-(\d{3})\.zarr$")


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def default_name(r):
    """Record id without its accession prefix and modality words: zenodo-7142003-snemi3d-neurites-sstem -> snemi3d-neurites."""
    words = r["id"].split("-")
    if len(words) > 2 and re.fullmatch(r"\d+", words[1]):
        words = words[2:]
    modality = {slug(m).replace("-", "") for fam in FAMILIES.values() for m in fam} | {"em", "lm", "sem", "tem", "cryoet"}
    kept = [w for w in words if w not in modality]
    return "-".join(kept) or r["id"]


def load_manifest(dataset_dir):
    manifest = dataset_dir / "manifest.json"
    return json.loads(manifest.read_text()) if manifest.exists() else {}


def assign_crops(dataset_dir, record_id, file_sets):
    """A crop name per sample: the same crop as before for files converted earlier (manifest.json), else
    the next free numbers, in the order given (sorted sample names, so numbering is stable across runs)."""
    crops = load_manifest(dataset_dir)
    known = {(info.get("record"), tuple(sorted(info.get("files") or []))): name for name, info in crops.items()}
    used = {int(m.group(1)) for p in dataset_dir.glob("crop-*.zarr") if (m := CROP.match(p.name))}
    used |= {int(m.group(1)) for n in crops if (m := CROP.match(n))}
    out = []
    for files in file_sets:
        name = known.get((record_id, tuple(sorted(files))))
        if name is None:
            n = max(used, default=0) + 1
            used.add(n)
            name = f"crop-{n:03d}.zarr"
        out.append(dataset_dir / name)
    return out


def label_attrs(r, array, label_class, voxel, today):
    """miao#13 label metadata from the record; fields it doesn't settle are null and listed in review_needed."""
    an = r.get("annotations") or {}
    review = []
    seg = SEGMENTATION_TYPE.get(array.get("encoding"))
    prov = PROVENANCE.get(an.get("source"))
    if seg is None:
        review.append(f"segmentation_type (encoding: {array.get('encoding')})")
    if prov is None:
        review.append(f"provenance (annotations.source: {an.get('source')})")
    coverage = None
    if array.get("encoding") == "points-table":
        coverage = "sparse_points"
    elif an.get("coverage") == "dense" and array.get("alignment") == "same-grid":
        coverage = "full_volume"
    elif an.get("coverage") == "sparse":
        coverage = "sparse_crop"
    else:
        review.append(f"coverage (annotations.coverage: {an.get('coverage')}, alignment: {array.get('alignment')})")
    size = None          # filled from the converted array by tools/miao_run.py (crops differ in shape)
    review.append("proofreading_status (not in the record)")
    pub = (r.get("publications") or [{}])[0]
    return {
        "label_class": label_class,
        "segmentation_type": seg,
        "provenance": prov,
        "proofreading_status": None,
        "coverage": coverage,
        "bbox": {"offset": [0, 0, 0], "size": size, "unit": "voxel",
                 "resolution_nm": [voxel["x"], voxel["y"], voxel["z"]]},
        "source": {"tool": None, "annotator": None, "model": None, "model_version": None},
        "created": today,
        "notes": f"converted from catalog record {r['id']} ({array.get('path_pattern')}); "
                 f"'created' is the conversion date; annotation source per the record: {an.get('source')}",
        "source_record": r["id"],
        "dataset_doi": r.get("doi"),
        "publication": pub.get("doi") or pub.get("url"),
        "license": (r.get("license") or {}).get("spdx"),
        "review_needed": review,
    }


_ZIPS = {}


def tiff_header(spec):
    """tools/probe.py's reading of a TIFF's header (a few KB by range requests; zip members in place)."""
    from tools.peek_archive import zip_entries
    from tools.probe import http_url, probe_url, probe_zip_member
    url, _, member = spec.partition("::")
    try:
        if not member:
            return probe_url(url)[0]
        if url not in _ZIPS:
            _ZIPS[url] = {e["name"]: e for e in zip_entries(http_url(url))[1]}
        entry = _ZIPS[url].get(member)
        return probe_zip_member(http_url(url), entry, 8 * 10**6)[0] if entry else {"error": f"{member} not in the zip"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def tiff_axes(r, plan, input_axes):
    """For each TIFF the plan converts: the axes to pass as input_axes ({input_path: axes}), stop reasons and notes.
    TensorSwitch's TIFF reader names samples per pixel `s` and the pages of a plain multi-page TIFF `i` (neither a
    channel nor z), so until convert accepts input_axes those files are a stop, found from the header. With
    input_axes the record's axes are passed (`s` as `c`: samples are channels to OME-NGFF)."""
    arrays = (r.get("technical") or {}).get("arrays") or []
    given, stops, notes = {}, [], []
    for p in plan["arrays"]:
        a = arrays[p["index"]] if p["index"] < len(arrays) else {}
        if not p.get("convertible") or a.get("format") != "tiff" or not p.get("fetch"):
            continue
        spec, rec_axes = p["fetch"]["spec"], a.get("axes")
        name = spec.rsplit("::", 1)[-1].rsplit("/", 1)[-1]
        if input_axes:
            if rec_axes:
                given[p["convert_args"]["input_path"]] = rec_axes.replace("s", "c")
            continue                           # no axes: already a stop (tiff_axes_needs)
        h = tiff_header(spec)
        # what tifffile (TensorSwitch's reader) makes of it: samples per pixel -> S, the pages of a TIFF with no
        # ImageJ / OME / tifffile-shape metadata -> I. tifffile-shaped files give Q, which TensorSwitch ignores
        # and replaces by z/y/x from the number of dimensions, so those convert correctly.
        if "error" in h or "tiff_layout" not in h:
            notes.append(f"{name}: TIFF header not read ({h.get('error') or '; '.join(h.get('notes', []))}); axes unchecked")
            continue
        how = None
        if h["samples_per_pixel"] > 1:
            how = f"{h['samples_per_pixel']} samples per pixel (TensorSwitch names that axis `s`)"
        elif h["tiff_layout"] == "plain" and (h.get("pages_counted") or 1) > 1:
            how = f"{h['pages_counted']}+ pages with no ImageJ/OME metadata (TensorSwitch names that axis `i`)"
        if how:
            stops.append(f"{name} ({p['role']}, record axes {rec_axes}): {how}, so the output gets no channel/z "
                         f"scale and verify_output can't match it to the source; needs TensorSwitch's input_axes")
    return given, stops, notes


def crop_steps(r, sample, plan, crop, cls_by_index, voxel, today, raw_attrs, input_axes=None):
    """Rewrite one TensorSwitch sample plan into steps writing to `crop` (miao#13 names and metadata)."""
    arrays = (r.get("technical") or {}).get("arrays") or []
    labels, keys = {}, {}
    for p in plan["arrays"]:
        if p.get("convertible") and p["role"] == "label":
            cls = cls_by_index[p["index"]]
            attrs = label_attrs(r, arrays[p["index"]], cls, voxel, today)
            info = {"instance": "instance", "semantic": "semantic", "point": "points"}.get(attrs["segmentation_type"], "labels")
            key = f"{attrs['provenance'] or 'unknown'}-{slug(cls).replace('-', '_')}-{info}"
            keys[p["convert_args"]["label_key"]] = key
            labels[key] = attrs
    steps = []
    for step in plan["steps"]:
        args = dict(step["args"])
        if step["tool"] in ("convert", "submit_job"):
            args["output_path"] = str(crop)
            args["preset"] = "miaai"
            # full OME-NGFF pyramid; the method is explicit because "auto" guesses from the file name,
            # and averaging instance IDs would invent IDs that don't exist
            args["auto_multiscale"] = True
            args["downsample_method"] = "mode" if args.get("is_label") else "mean"
            if (input_axes or {}).get(args.get("input_path")):
                args["input_axes"] = input_axes[args["input_path"]]   # from technical.arrays[].axes
            if args.get("is_label"):
                key = keys[args["label_key"]]
                args["label_key"] = key
                args["extra_attributes"] = json.dumps(labels[key])
            else:
                args["image_key"] = "raw"
                args["extra_attributes"] = json.dumps(raw_attrs)
        elif step["tool"] == "verify_output":
            args["output_path"] = str(crop)
            args["labels"] = ";".join(f"{keys.get(k, k)}={v}" for k, v in
                                      (item.split("=", 1) for item in args.get("labels", "").split(";") if item))
        steps.append({"tool": step["tool"], "args": args})
    files = [s["args"]["spec"] for s in steps if s["tool"] == "fetch_dataset"]
    staged = [s["args"]["input_path"] for s in steps if s["tool"] in ("convert", "submit_job")]
    return {"crop": str(crop), "sample": sample, "files": files, "staged_files": sorted(set(staged)),
            "labels": labels, "steps": steps,
            "manifest_entry": {"record": r["id"], "sample": sample, "files": files,
                               "labels": list(keys.values()), "converted": today}}


def emit(out, path):
    text = json.dumps(out, indent=2)
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text + "\n")
        print(json.dumps({"plan": str(path), "crops": len(out["crops"]), "stop": out["stop"]}))
    else:
        print(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("record")
    ap.add_argument("--root", default="demo", help="output root; data goes to <root>/data/, downloads to <root>/staging/")
    ap.add_argument("--tensorswitch", required=True, metavar="SRC", help="TensorSwitch src/ folder (unified branch)")
    ap.add_argument("--label-class", action="append", default=[],
                    help="label_class per label array, in record order (repeat for several); see miao#13")
    ap.add_argument("--name", help="dataset part of the folder name (default: from the record id)")
    ap.add_argument("--organism", help="which organism this data is, when the record lists several")
    ap.add_argument("--whole", action="store_true",
                    help="every file of the dataset (one crop per raw/label pair; needs a zip download, max 50 GB) "
                         "instead of the sample unit")
    ap.add_argument("--labelled-only", action="store_true", help="--whole: skip samples that have no label")
    ap.add_argument("--out", help="write the plan to this file instead of stdout (its folder is created)")
    a = ap.parse_args()
    r = load_yaml(a.record)
    im = r.get("imaging") or {}
    root = Path(a.root).resolve()
    staging = root / "staging" / r["id"]
    stop, review, notes = [], [], []

    fams, _ = family(im.get("modality") or [])
    fam = family(im.get("modality", [])[:1])[0]
    fam = next(iter(fam)) if len(fam) == 1 else (next(iter(fams)) if len(fams) == 1 else None)
    if fam is None:
        stop.append(f"modality family unclear ({im.get('modality')}): fix imaging.modality first")
    orgs = im.get("organism") or []
    org = a.organism or (orgs[0] if len(orgs) == 1 else None)
    if org is None:
        stop.append(f"organism: the record lists {orgs or 'none'}; pass --organism")
    elif org not in ORGANISM_SHORT:
        stop.append(f"no short name for organism {org!r}: add it to ORGANISM_SHORT in tools/miao_layout.py")
    if a.whole and len(orgs) > 1:
        stop.append("--whole with several organisms: files can't be assigned to an organism automatically yet")
    vs = im.get("voxel_size_nm") or {}
    if not (vs.get("x") and vs.get("y") and vs.get("z")):
        stop.append("imaging.voxel_size_nm is missing or incomplete")
    if str(im.get("dimensionality")) != "3D":
        stop.append(f"dimensionality {im.get('dimensionality')}: only 3D is covered by this layout for now")

    rp = _planner(a.tensorswitch)
    arrays = (r.get("technical") or {}).get("arrays") or []
    in_axes = input_axes_supported(a.tensorswitch)
    stop += tiff_axes_needs(arrays, in_axes)
    label_index = [i for i, x in enumerate(arrays) if x.get("role") == "label"]
    if any(x.get("role") not in ("raw", "label") for x in arrays):
        stop.append("the record has restoration targets or other roles; miao#13 only defines raw/ and labels/")
    if len(a.label_class) != len(label_index):
        stop.append(f"{len(label_index)} label array(s) but {len(a.label_class)} --label-class given "
                    f"(take each from the record; vocabulary: {', '.join(LABEL_CLASSES)})")
    cls_by_index = dict(zip(label_index, a.label_class))

    # the samples: (name, sample record, TensorSwitch plan) per crop
    samples, unpaired, skipped = [], [], []
    base = rp.plan_record(r, str(staging))
    if base.get("status") != "ready":
        stop.append(f"TensorSwitch planner status {base.get('status')}: {base.get('warnings')}")
    if a.whole and not stop:
        whole = rp.plan_dataset(r, str(staging))
        unpaired, skipped = whole.get("unpaired", []), whole.get("skipped", [])
        if whole["status"] in ("blocked", "skipped"):
            stop.append(f"whole-dataset plan {whole['status']}: {whole.get('warnings')}")
        zip_url = rp._zip_url(r)
        for smp in whole.get("samples", []):
            sub = copy.deepcopy(r)
            sub["technical"]["arrays"] = [{k: v for k, v in x.items() if k not in ("shape", "shape_varies")} for x in arrays]
            sub["technical"]["sample"] = {"urls": [f"{zip_url}::{f}" for f in smp["files"]]}
            sp = rp.plan_record(sub, str(staging))
            has_label = any(p.get("convertible") and p["role"] == "label" for p in sp["arrays"])
            has_raw = any(p.get("convertible") and p["role"] == "raw" for p in sp["arrays"])
            if not has_raw:
                notes.append(f"{smp['name']}: label without raw image, skipped")
            elif a.labelled_only and not has_label:
                notes.append(f"{smp['name']}: no label, skipped (--labelled-only)")
            elif sp["status"] != "ready" and not (sp["status"] == "partial" and all(
                    p["role"] == "label" and not p.get("files") for p in sp["arrays"] if not p.get("convertible"))):
                notes.append(f"{smp['name']}: planner status {sp['status']}, skipped: {sp['warnings']}")
            else:
                samples.append((smp["name"], sp))
        if not samples and not stop:
            stop.append("no sample of the dataset could be planned")
    elif not stop:
        samples.append(("sample-unit", base))

    # TIFF axes: probe each crop's TIFFs (or pass the record's axes when TensorSwitch accepts input_axes).
    # Files with the same layout behave the same, so one stop reason per distinct message is enough.
    axes_by_sample = []
    for smp_name, sp in samples:
        given, s_stops, s_notes = tiff_axes(r, sp, in_axes)
        axes_by_sample.append(given)
        stop += [x for x in s_stops if x not in stop]
        notes += [f"{smp_name}: {x}" for x in s_notes]
        if s_stops:
            break                       # the layout is shared by the dataset's files: don't probe every one

    name = slug(a.name or default_name(r))
    dataset_dir = root / "data" / f"{fam}-{ORGANISM_SHORT.get(org, 'unknown')}-{name}"
    out = {"record": r["id"], "scope": "dataset" if a.whole else "sample-unit", "dataset_dir": str(dataset_dir),
           "staging": str(staging), "crops": [], "unpaired": unpaired, "skipped": skipped, "notes": notes,
           "review": review, "stop": stop}
    if stop:
        emit(out, a.out)
        return

    today = datetime.date.today().isoformat()
    voxel = {k: vs[k] for k in "xyz"}
    raw_attrs = {"source_record": r["id"], "dataset_doi": r.get("doi"), "license": (r.get("license") or {}).get("spdx"),
                 "publication": ((r.get("publications") or [{}])[0]).get("doi"), "title": r.get("title")}
    file_sets = [[s["args"]["spec"] for s in sp["steps"] if s["tool"] == "fetch_dataset"] for _, sp in samples]
    for (sample, sp), crop, given in zip(samples, assign_crops(dataset_dir, r["id"], file_sets), axes_by_sample):
        c = crop_steps(r, sample, sp, crop, cls_by_index, voxel, today, raw_attrs, given)
        out["crops"].append(c)
    for key, attrs in (out["crops"][0]["labels"] if out["crops"] else {}).items():
        review += [f"{key}: {x}" for x in attrs["review_needed"]]
    emit(out, a.out)


if __name__ == "__main__":
    main()
