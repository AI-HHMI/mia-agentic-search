"""PR labels derived from a dataset record, so labels always match the YAML.

    python tools/labels.py <record.yaml>          # print the labels, one per line
    python tools/labels.py sync --pr N            # CI: set PR N's labels from the record on its head
    python tools/labels.py sync --all-open        # CI: same for every open claude/dataset/* PR
    python tools/labels.py sync --merged          # CI: merged claude/dataset/* PRs, from the record now on main

Agents never add labels by hand: they fill in the record and .github/workflows/label.yml runs `sync`
on every push. Labels outside the managed prefixes (e.g. new-datasets) are left alone.

  dim:3D · org:Mus musculus · modality:FIB-SEM · fmt:tiff · dtype:uint16 · anno:instance-segmentation
  label-enc:instance-ids · license:CC-BY-4.0 (the SPDX id as written, not interpreted) · size:1-10GB
  auto-download (size confirmed by a complete file listing, < 50 GB, open access) · enriched
  license-verification-needed (license.spdx is unknown)
  size-estimated (data.size_bytes is an estimate from shapes x dtypes: technical.size_source estimated)
  voxel-size-found / voxel-size-missing (imaging.voxel_size_nm has x, y and, for 3D data, z)
  download-ready / download-not-ready (3D and 3D+t only: tools/readiness.py; needs $TENSORSWITCH_SRC,
  without it neither label is set, so the auto-merge gate fails closed)
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import (AUTO_DOWNLOAD_MAX_BYTES, CONFIRMED_SIZE_SOURCES, DATASET_BRANCH_PREFIX,  # noqa: E402
                          ROOT, _NoDatesLoader, load_yaml)

# prefix -> (colour, description)
GROUPS = {
    "dim:": ("0E8A16", "Dimensionality"),
    "org:": ("5319E7", "Organism"),
    "modality:": ("1D76DB", "Imaging modality"),
    "fmt:": ("C5DEF5", "File format"),
    "dtype:": ("BFD4F2", "Pixel data type of the raw images (inspected)"),
    "anno:": ("FBCA04", "Annotation type"),
    "label-enc:": ("FEF2C0", "How labels are stored (inspected)"),
    "license:": ("D93F0B", "License as stated by the dataset (SPDX id, not interpreted)"),
    "size:": ("BFDADC", "Total download size"),
}
FLAGS = {
    "auto-download": ("0E8A16", "Size confirmed by a complete file listing, < 50 GB, open access"),
    "enriched": ("006B75", "Technical metadata filled in by the enricher routine"),
    "license-verification-needed": ("B60205", "No license found yet; a human (or agent) needs to find or request it"),
    "size-estimated": ("FBCA04", "size: label from an estimate (files x voxels x bytes per voxel), not a file listing"),
    "voxel-size-found": ("0E8A16", "imaging.voxel_size_nm is filled (x, y and, for 3D data, z)"),
    "voxel-size-missing": ("E99695", "No voxel / pixel size in the record yet; conversion needs it"),
    "download-ready": ("0E8A16", "3D: /download-dataset can convert it unattended (tools/readiness.py)"),
    "download-not-ready": ("B60205", "3D: something blocks the download; see tools/readiness.py"),
}
SIZE_BUCKETS = [(10**9, "<1GB"), (10 * 10**9, "1-10GB"), (50 * 10**9, "10-50GB"), (500 * 10**9, "50-500GB")]
MAX_LEN = 50  # GitHub's label name limit


def _label(prefix, value):
    return (prefix + str(value))[:MAX_LEN].rstrip()   # GitHub strips a trailing space, so a cut there would 404


def voxel_size_found(im):
    vs = im.get("voxel_size_nm") or {}
    return bool(vs.get("x") and vs.get("y") and (vs.get("z") or str(im.get("dimensionality", "")).startswith("2D")))


def labels_for(r):
    im, da, an = r.get("imaging") or {}, r.get("data") or {}, r.get("annotations") or {}
    tech = r.get("technical") or {}
    arrays = [a for a in tech.get("arrays") or [] if isinstance(a, dict)]
    out = []
    if im.get("dimensionality"):
        out.append(_label("dim:", im["dimensionality"]))
    orgs = im.get("organism") or []
    out += [_label("org:", o) for o in orgs[:4]] or ["org:unknown"]
    out += [_label("modality:", m) for m in im.get("modality") or []]
    out += [_label("fmt:", f) for f in da.get("formats") or []]
    out += [_label("dtype:", d) for d in dict.fromkeys(a.get("dtype") for a in arrays if a.get("role") in ("raw", "target"))
            if d and d != "unknown"]
    out += [_label("anno:", t) for t in an.get("types") or []] if an.get("present") else ["anno:none"]
    out += [_label("label-enc:", e) for e in dict.fromkeys(a.get("encoding") for a in arrays if a.get("role") == "label")
            if e and e != "unknown"]
    spdx = (r.get("license") or {}).get("spdx") or "unknown"
    out.append(_label("license:", spdx))
    if spdx == "unknown":
        out.append("license-verification-needed")
    size = da.get("size_bytes")
    if size is None:
        out.append("size:unknown")
    else:
        out.append("size:" + next((name for limit, name in SIZE_BUCKETS if size < limit), ">500GB"))
        if tech.get("size_source") == "estimated":
            out.append("size-estimated")
    if (size is not None and size < AUTO_DOWNLOAD_MAX_BYTES and tech.get("size_source") in CONFIRMED_SIZE_SOURCES
            and da.get("access") == "open"):
        out.append("auto-download")
    if tech:
        out.append("enriched")
    out.append("voxel-size-found" if voxel_size_found(im) else "voxel-size-missing")
    if str(im.get("dimensionality")) in ("3D", "3D+t"):
        from tools.readiness import readiness
        status = readiness(r)[0]
        if status != "unknown":
            out.append("download-ready" if status == "ready" else "download-not-ready")
    return list(dict.fromkeys(out))


def managed(name):
    return name in FLAGS or any(name.startswith(p) for p in GROUPS)


def style(name):
    if name in FLAGS:
        return FLAGS[name]
    prefix = next(p for p in GROUPS if name.startswith(p))
    return GROUPS[prefix]


def _run(*cmd, check=True):
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"{' '.join(cmd)} failed:\n{r.stderr.strip()}")
    return r.stdout


def record_on_pr(number):
    """The dataset record added or changed by PR `number` (read from git, never executed)."""
    ref = f"refs/remotes/origin/pr-{number}"
    _run("git", "fetch", "-q", "origin", f"+pull/{number}/head:{ref}")
    _run("git", "fetch", "-q", "origin", "main")
    paths = [p for p in _run("git", "diff", "--name-only", "--diff-filter=AM", f"origin/main...{ref}", "--", "datasets/").split()
             if p.endswith(".yaml")]
    if len(paths) != 1:
        return None, paths
    return yaml.load(_run("git", "show", f"{ref}:{paths[0]}"), Loader=_NoDatesLoader), paths


def record_on_main(record_id, paths_by_id):
    """The record now on main for a merged PR (it may have been corrected since the merge)."""
    path = paths_by_id.get(record_id)
    if path is None:
        return None, []
    return yaml.load(_run("git", "show", f"origin/main:{path}"), Loader=_NoDatesLoader), [path]


def sync(number, repo_labels, rec=None, paths=None, have=None):
    if rec is None:
        rec, paths = record_on_pr(number)
    if rec is None:
        print(f"#{number}: expected one record, found {paths}; labels unchanged")
        return
    want = set(labels_for(rec))
    if have is None:
        have = {lab["name"] for lab in json.loads(_run("gh", "pr", "view", str(number), "--json", "labels"))["labels"]}
    add = sorted(want - have)
    remove = sorted(n for n in have - want if managed(n))
    for name in add:
        if name not in repo_labels:
            color, desc = style(name)
            _run("gh", "label", "create", name, "--color", color, "--description", desc, "--force")
            repo_labels.add(name)
    cmd = ["gh", "pr", "edit", str(number)]
    for name in add:
        cmd += ["--add-label", name]
    for name in remove:
        cmd += ["--remove-label", name]
    if add or remove:
        _run(*cmd)
    print(f"#{number} {rec.get('id')}: +{add} -{remove}")


def sync_all(items, repo_labels):
    """sync() each (number, kwargs); one failing PR is reported and doesn't stop the others."""
    failed = []
    for number, kw in items:
        try:
            sync(number, repo_labels, **kw)
        except SystemExit as e:
            print(f"#{number}: FAILED {e}")
            failed.append(number)
    if failed:
        sys.exit(f"{len(failed)} PR(s) failed: {failed}")


def main():
    if sys.argv[1:2] != ["sync"]:
        ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        ap.add_argument("record")
        print("\n".join(labels_for(load_yaml(ap.parse_args().record))))
        return
    ap = argparse.ArgumentParser(prog="labels.py sync")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pr", type=int)
    g.add_argument("--all-open", action="store_true")
    g.add_argument("--merged", action="store_true")
    a = ap.parse_args(sys.argv[2:])
    repo_labels = {lab["name"] for lab in json.loads(_run("gh", "label", "list", "--limit", "1000", "--json", "name"))}
    if a.pr:
        sync(a.pr, repo_labels)
        return
    if a.merged:
        _run("git", "fetch", "-q", "origin", "main")
        paths_by_id = {Path(p).stem: p for p in _run("git", "ls-tree", "-r", "--name-only", "origin/main", "--", "datasets/").split()
                       if p.endswith(".yaml")}
        prs = json.loads(_run("gh", "pr", "list", "--state", "merged", "--limit", "2000", "--json", "number,headRefName,labels"))
        items = []
        for pr in prs:
            if pr["headRefName"].startswith(DATASET_BRANCH_PREFIX):
                rec, paths = record_on_main(pr["headRefName"].removeprefix(DATASET_BRANCH_PREFIX), paths_by_id)
                items.append((pr["number"], {"rec": rec, "paths": paths, "have": {lab["name"] for lab in pr["labels"]}}))
        sync_all(items, repo_labels)
        return
    prs = json.loads(_run("gh", "pr", "list", "--state", "open", "--limit", "500", "--json", "number,headRefName"))
    sync_all([(pr["number"], {}) for pr in prs if pr["headRefName"].startswith(DATASET_BRANCH_PREFIX)], repo_labels)


if __name__ == "__main__":
    main()
