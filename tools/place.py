"""Move dataset records to where they belong: datasets/<dimensionality>/<first modality>/<id>.yaml.

    python tools/place.py <record.yaml> [...]       # move these files (git mv when tracked)
    python tools/place.py --all                     # every record under datasets/
    python tools/place.py --pr-branches [--dry-run] # one `layout:` commit per open claude/dataset/* PR

Run it after changing a record's dimensionality or first modality (the validator says when).
--pr-branches moves the record on every open dataset PR branch, pushing without force: if a
branch changed meanwhile (e.g. the enricher pushed), it is re-read and retried.
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import (DATASET_BRANCH_PREFIX, DATASETS_DIR, ROOT, _NoDatesLoader, canonical_path,  # noqa: E402
                          git, iter_record_paths, load_yaml, rel)
from tools.publish import LAYOUT_PREFIX, _cleanup, _worktree, run  # noqa: E402


def move_file(path):
    path = Path(path).resolve()
    target = canonical_path(load_yaml(path))
    if target is None:
        print(f"skip {rel(path)}: dimensionality / modality not valid")
        return None
    if target == path:
        return path
    target.parent.mkdir(parents=True, exist_ok=True)
    if git("ls-files", "--error-unmatch", str(path)).strip():
        run("mv", str(path), str(target))
    else:
        shutil.move(path, target)
    for d in (path.parent, path.parent.parent):  # drop emptied folders
        if d != DATASETS_DIR and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    print(f"{rel(path)} -> {rel(target)}")
    return target


def move_on_branch(branch, dry_run, attempts=3):
    for attempt in range(attempts):
        try:
            run("fetch", "-q", "origin", "main", f"+{branch}:refs/remotes/origin/{branch}")
        except SystemExit:  # merged (and deleted) or closed since the PR list was read
            return "skipped: branch no longer exists"
        ref = f"origin/{branch}"
        paths = [p for p in run("diff", "--name-only", "--diff-filter=AM", f"origin/main...{ref}", "--", "datasets/").split()
                 if p.endswith(".yaml")]
        if len(paths) != 1:
            return f"skipped: expected one record, found {paths}"
        rec = yaml.load(run("show", f"{ref}:{paths[0]}"), Loader=_NoDatesLoader)
        target = canonical_path(rec)
        if target is None:
            return "skipped: dimensionality / modality not valid"
        new = str(target.relative_to(ROOT))
        if new == paths[0]:
            return "already in place"
        if dry_run:
            return f"would move {paths[0]} -> {new}"
        tmp = _worktree(ref)
        try:
            (tmp / new).parent.mkdir(parents=True, exist_ok=True)
            run("mv", paths[0], new, cwd=tmp)
            run("commit", "-q", "-m", f"{LAYOUT_PREFIX}move record to {str(Path(new).parent)}/", cwd=tmp)
            r = subprocess.run(["git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], cwd=tmp,
                               capture_output=True, text=True)
        finally:
            _cleanup(tmp)
        if r.returncode == 0:
            return f"moved {paths[0]} -> {new}"
    return "failed: branch kept changing; try again later"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--pr-branches", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.pr_branches:
        prs = json.loads(subprocess.run(["gh", "pr", "list", "--state", "open", "--limit", "500", "--json",
                                         "number,headRefName"], cwd=ROOT, capture_output=True, text=True,
                                        check=True).stdout)
        for pr in sorted(prs, key=lambda p: p["number"]):
            if pr["headRefName"].startswith(DATASET_BRANCH_PREFIX):
                print(f"#{pr['number']} {pr['headRefName']}: {move_on_branch(pr['headRefName'], a.dry_run)}")
        return
    targets = list(iter_record_paths([DATASETS_DIR])) if a.all else [Path(f) for f in a.files]
    if not targets:
        ap.error("give files, --all or --pr-branches")
    for p in targets:
        move_file(p)


if __name__ == "__main__":
    main()
