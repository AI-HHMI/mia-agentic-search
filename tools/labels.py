"""PR labels derived from a dataset record, so labels always match the YAML; auto-merge follows from them.

    python tools/labels.py <record.yaml>                 # print the labels, one per line
    python tools/labels.py sync --pr N [--merge]         # CI: set PR N's labels from the record on its head
    python tools/labels.py sync --all-open [--merge]     # CI: same for every open claude/dataset/* PR

Agents never add labels by hand: they fill in the record and .github/workflows/label.yml runs `sync`
on every push. Labels outside the managed prefixes (e.g. new-datasets, hold, not-duplicate) are left alone;
merged PRs keep the labels they were merged with.

With --merge, a PR that meets tools/automerge.py gets GitHub's native auto-merge (`gh pr merge --auto
--match-head-commit`), which merges once the required `validate` check passes; a PR that no longer meets
it has auto-merge disabled.

  dim:3D · org:Mus musculus · modality:FIB-SEM · fmt:tiff · anno:instance-segmentation
  license:CC-BY-4.0 (the SPDX id as written, not interpreted) · size:1-10GB
  enriched
  voxel-size-missing (imaging.voxel_size_nm lacks x, y or, for 3D data, z)
  download-ready / download-not-ready (tools/readiness.py, the one rule for downloads). Needs
  $TENSORSWITCH_SRC; without it a record that passes every other check gets neither label.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.automerge import failures  # noqa: E402
from tools.common import (DATASET_BRANCH_PREFIX, ROOT, git, iter_record_paths, load_yaml, pending_records,  # noqa: E402
                          voxel_size_found)

# prefix -> (colour, description)
GROUPS = {
    "dim:": ("0E8A16", "Dimensionality"),
    "org:": ("5319E7", "Organism"),
    "modality:": ("1D76DB", "Imaging modality"),
    "fmt:": ("C5DEF5", "File format"),
    "anno:": ("FBCA04", "Annotation type"),
    "license:": ("D93F0B", "License as stated by the dataset (SPDX id, not interpreted)"),
    "size:": ("BFDADC", "Total download size"),
}
FLAGS = {
    "enriched": ("006B75", "Technical metadata filled in by the enricher routine"),
    "voxel-size-missing": ("E99695", "No voxel / pixel size in the record yet; conversion needs it"),
    "download-ready": ("0E8A16", "/download-dataset can convert it unattended (tools/readiness.py)"),
    "download-not-ready": ("B60205", "Something blocks the download; see tools/readiness.py"),
}
# No longer set; sync still removes them from open PRs.
RETIRED = {"auto-download", "license-verification-needed", "size-estimated", "voxel-size-found"}
RETIRED_PREFIXES = ("dtype:", "label-enc:")
SIZE_BUCKETS = [(10**9, "<1GB"), (10 * 10**9, "1-10GB"), (50 * 10**9, "10-50GB"), (500 * 10**9, "50-500GB")]
MAX_LEN = 50  # GitHub's label name limit


def _label(prefix, value):
    return (prefix + str(value))[:MAX_LEN].rstrip()   # GitHub strips a trailing space, so a cut there would 404


def labels_for(r):
    im, da, an = r.get("imaging") or {}, r.get("data") or {}, r.get("annotations") or {}
    out = []
    if im.get("dimensionality"):
        out.append(_label("dim:", im["dimensionality"]))
    orgs = im.get("organism") or []
    out += [_label("org:", o) for o in orgs[:4]] or ["org:unknown"]
    out += [_label("modality:", m) for m in im.get("modality") or []]
    out += [_label("fmt:", f) for f in da.get("formats") or []]
    out += [_label("anno:", t) for t in an.get("types") or []] if an.get("present") else ["anno:none"]
    out.append(_label("license:", (r.get("license") or {}).get("spdx") or "unknown"))
    size = da.get("size_bytes")
    out.append("size:" + ("unknown" if size is None else next((n for lim, n in SIZE_BUCKETS if size < lim), ">500GB")))
    if r.get("technical"):
        out.append("enriched")
    if not voxel_size_found(im):
        out.append("voxel-size-missing")
    from tools.readiness import readiness
    status = readiness(r)[0]
    if status != "unknown":
        out.append("download-ready" if status == "ready" else "download-not-ready")
    return list(dict.fromkeys(out))


def managed(name):
    return name in FLAGS or name in RETIRED or name.startswith(RETIRED_PREFIXES) or any(name.startswith(p) for p in GROUPS)


def gh(*args):
    """`gh ...` with 3 tries (GitHub's API fails transiently); exits on the last failure."""
    for attempt in range(3):
        r = subprocess.run(["gh", *args], cwd=ROOT, capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout
        if attempt < 2:
            time.sleep(5 * 2 ** attempt)
    sys.exit(f"gh {' '.join(args)} failed:\n{r.stderr.strip()}")


def sync(pr, rec, sha, repo_labels, others, merge):
    """Set PR `pr`'s labels from `rec` (its record at branch commit `sha`); with `merge`, (un)set auto-merge."""
    n = str(pr["number"])
    want = labels_for(rec)
    have = {lab["name"] for lab in pr["labels"]}
    add = sorted(set(want) - have)
    remove = sorted(name for name in have - set(want) if managed(name))
    for name in add:
        if name not in repo_labels:
            color, desc = FLAGS.get(name) or GROUPS[next(p for p in GROUPS if name.startswith(p))]
            gh("label", "create", name, "--color", color, "--description", desc, "--force")
            repo_labels.add(name)
    if add or remove:
        gh("pr", "edit", n, *(f"--add-label={x}" for x in add), *(f"--remove-label={x}" for x in remove))
    verdict = ""
    if merge and sha == pr["headRefOid"]:  # otherwise a push is on its way, and its own run decides
        fails = failures(want, pr, rec, others)
        if not fails:
            gh("pr", "merge", n, "--auto", "--merge", "--match-head-commit", sha, "--delete-branch")
            verdict = "; auto-merge on"
        elif pr.get("autoMergeRequest"):
            gh("pr", "merge", n, "--disable-auto")
            verdict = f"; auto-merge off: {'; '.join(fails)}"
    print(f"#{n} {rec.get('id')}: +{add} -{remove}{verdict}")


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
    ap.add_argument("--merge", action="store_true", help="enable / disable auto-merge (repo variable AUTO_MERGE)")
    a = ap.parse_args(sys.argv[2:])
    prs = [pr for pr in json.loads(gh("pr", "list", "--state", "open", "--limit", "500", "--json",
                                      "number,headRefName,headRefOid,isDraft,labels,autoMergeRequest"))
           if pr["headRefName"].startswith(DATASET_BRANCH_PREFIX)]
    # Every dataset branch in one fetch: the PR's own record, and the open ones it may duplicate.
    subprocess.run(["git", "fetch", "-q", "--prune", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main",
                    f"+refs/heads/{DATASET_BRANCH_PREFIX}*:refs/remotes/origin/{DATASET_BRANCH_PREFIX}*"],
                   cwd=ROOT, check=True)
    heads = dict(line.split() for line in git("for-each-ref", "--format=%(refname:short) %(objectname)",
                                              f"refs/remotes/origin/{DATASET_BRANCH_PREFIX}").splitlines())
    recs = pending_records([pr["headRefName"] for pr in prs])
    others = [load_yaml(p) for p in iter_record_paths()] + list(recs.values())
    repo_labels = {lab["name"] for lab in json.loads(gh("label", "list", "--limit", "1000", "--json", "name"))}
    failed = []
    for pr in prs:
        if a.pr and pr["number"] != a.pr:
            continue
        rec = recs.get(pr["headRefName"])
        if rec is None:
            print(f"#{pr['number']}: no new record on {pr['headRefName']}; labels unchanged")
            continue
        try:
            sync(pr, rec, heads.get("origin/" + pr["headRefName"]), repo_labels, others, a.merge)
        except SystemExit as e:  # one failing PR is reported and doesn't stop the others
            print(f"#{pr['number']}: FAILED {e}")
            failed.append(pr["number"])
    if failed:
        sys.exit(f"{len(failed)} PR(s) failed: {failed}")


if __name__ == "__main__":
    main()
