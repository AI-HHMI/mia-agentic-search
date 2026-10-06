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
import datetime
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import load_yaml  # noqa: E402
from tools.convertibility import FAMILIES, _planner, family  # noqa: E402

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


def crop_dir(dataset_dir, record_id):
    """The record's crop if this record was converted before (manifest.json), else the next free number."""
    manifest = dataset_dir / "manifest.json"
    crops = json.loads(manifest.read_text()) if manifest.exists() else {}
    for name, info in crops.items():
        if info.get("record") == record_id:
            return dataset_dir / name, crops
    used = [int(m.group(1)) for p in dataset_dir.glob("crop-*.zarr") if (m := CROP.match(p.name))]
    used += [int(CROP.match(n).group(1)) for n in crops if CROP.match(n)]
    return dataset_dir / f"crop-{max(used, default=0) + 1:03d}.zarr", crops


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
    shape = array.get("shape") or []
    axes = array.get("axes") or ""
    size = None
    if axes and len(axes) == len(shape) and all(a in axes for a in "zyx"):
        size = [shape[axes.index(a)] for a in "xyz"]
    else:
        review.append("bbox.size (array axes not stated)")
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("record")
    ap.add_argument("--root", default="demo", help="output root; data goes to <root>/data/, downloads to <root>/staging/")
    ap.add_argument("--tensorswitch", required=True, metavar="SRC", help="TensorSwitch src/ folder (unified branch)")
    ap.add_argument("--label-class", action="append", default=[],
                    help="label_class per label array, in record order (repeat for several); see miao#13")
    ap.add_argument("--name", help="dataset part of the folder name (default: from the record id)")
    ap.add_argument("--organism", help="which organism this sample unit is, when the record lists several")
    a = ap.parse_args()
    r = load_yaml(a.record)
    im = r.get("imaging") or {}
    root = Path(a.root).resolve()
    stop, review = [], []

    fams, _ = family(im.get("modality") or [])
    fam = family(im.get("modality", [])[:1])[0]
    fam = next(iter(fam)) if len(fam) == 1 else (next(iter(fams)) if len(fams) == 1 else None)
    if fam is None:
        stop.append(f"modality family unclear ({im.get('modality')}): fix imaging.modality first")
    orgs = im.get("organism") or []
    org = a.organism or (orgs[0] if len(orgs) == 1 else None)
    if org is None:
        stop.append(f"organism: the record lists {orgs or 'none'}; pass --organism for this sample unit")
    elif org not in ORGANISM_SHORT:
        stop.append(f"no short name for organism {org!r}: add it to ORGANISM_SHORT in tools/miao_layout.py")
    vs = im.get("voxel_size_nm") or {}
    if not (vs.get("x") and vs.get("y") and vs.get("z")):
        stop.append("imaging.voxel_size_nm is missing or incomplete")
    if str(im.get("dimensionality")) != "3D":
        stop.append(f"dimensionality {im.get('dimensionality')}: only 3D is covered by this layout for now")

    plan = _planner(a.tensorswitch).plan_record(r, str(root / "staging" / r["id"]))
    if plan.get("status") != "ready":
        stop.append(f"TensorSwitch planner status {plan.get('status')}: {plan.get('warnings')}")
    arrays = (r.get("technical") or {}).get("arrays") or []
    planned = [p for p in plan.get("arrays", []) if p.get("convertible")]
    if any(p["role"] not in ("raw", "label") for p in planned):
        stop.append("the sample has restoration targets or other roles; miao#13 only defines raw/ and labels/")
    label_arrays = [p for p in planned if p["role"] == "label"]
    if len(a.label_class) != len(label_arrays):
        stop.append(f"{len(label_arrays)} label array(s) but {len(a.label_class)} --label-class given "
                    f"(take each from the record; vocabulary: {', '.join(LABEL_CLASSES)})")

    name = slug(a.name or default_name(r))
    dataset_dir = root / "data" / f"{fam}-{ORGANISM_SHORT.get(org, 'unknown')}-{name}"
    crop, _ = crop_dir(dataset_dir, r["id"])
    out = {"record": r["id"], "dataset_dir": str(dataset_dir), "crop": str(crop),
           "staging": str(root / "staging" / r["id"]), "steps": [], "labels": {}, "review": review, "stop": stop}
    if stop:
        print(json.dumps(out, indent=2))
        return

    today = datetime.date.today().isoformat()
    voxel = {k: vs[k] for k in "xyz"}
    keys = {}                               # planner label_key -> miao label folder
    for p, cls in zip(label_arrays, a.label_class):
        rec_array = arrays[p["index"]]
        attrs = label_attrs(r, rec_array, cls, voxel, today)
        info = {"instance": "instance", "semantic": "semantic", "point": "points"}.get(attrs["segmentation_type"], "labels")
        key = f"{attrs['provenance'] or 'unknown'}-{slug(cls).replace('-', '_')}-{info}"
        keys[p["convert_args"]["label_key"]] = key
        out["labels"][key] = attrs
        review += [f"{key}: {x}" for x in attrs["review_needed"]]
    raw_attrs = {"source_record": r["id"], "dataset_doi": r.get("doi"), "license": (r.get("license") or {}).get("spdx"),
                 "publication": ((r.get("publications") or [{}])[0]).get("doi"), "title": r.get("title")}
    for step in plan["steps"]:
        args = dict(step["args"])
        if step["tool"] in ("convert", "submit_job"):
            args["output_path"] = str(crop)
            args["preset"] = "miaai"
            # full OME-NGFF pyramid; the method is explicit because "auto" guesses from the file name,
            # and averaging instance IDs would invent IDs that don't exist
            args["auto_multiscale"] = True
            args["downsample_method"] = "mode" if args.get("is_label") else "mean"
            if args.get("is_label"):
                key = keys[args["label_key"]]
                args["label_key"] = key
                args["extra_attributes"] = json.dumps(out["labels"][key])
            else:
                args["image_key"] = "raw"
                args["extra_attributes"] = json.dumps(raw_attrs)
        elif step["tool"] == "verify_output":
            args["output_path"] = str(crop)
            args["labels"] = ";".join(f"{keys.get(k, k)}={v}" for k, v in
                                      (item.split("=", 1) for item in args.get("labels", "").split(";") if item))
        out["steps"].append({"tool": step["tool"], "args": args})
    out["manifest_entry"] = {crop.name: {"record": r["id"], "files": [s["args"]["spec"] for s in out["steps"]
                                                                     if s["tool"] == "fetch_dataset"],
                                         "labels": list(keys.values()), "converted": today}}
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
