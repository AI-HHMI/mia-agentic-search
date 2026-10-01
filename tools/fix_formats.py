"""Drop `other` from data.formats wherever a known format is listed too (CLAUDE.md, field conventions).

    python tools/fix_formats.py <record.yaml> [...]       # fix these files in place
    python tools/fix_formats.py --all                     # every record under datasets/
    python tools/fix_formats.py --pr-branches [--dry-run] # one `format:` commit per open claude/dataset/* PR

Only `data.formats` changes. Branches are pushed without force; one that changed meanwhile is re-read
and retried. Records listing only `other` are left alone and reported.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import (DATASET_BRANCH_PREFIX, DATASETS_DIR, ROOT, _NoDatesLoader,  # noqa: E402
                          iter_record_paths, load_yaml, rel)
from tools.publish import FORMAT_PREFIX, _cleanup, _worktree, run  # noqa: E402


def fixed(formats):
    """The corrected list, or None if nothing needs to change."""
    if "other" in formats and len(formats) > 1:
        return [f for f in formats if f != "other"]
    return None


def fix_file(path):
    """Delete the `- other` line from data.formats as text, so nothing else in the file is reformatted."""
    path = Path(path)
    rec = load_yaml(path)
    new = fixed(rec["data"]["formats"])
    if new is None:
        return "other only" if rec["data"]["formats"] == ["other"] else None
    lines = path.read_text().splitlines(keepends=True)
    out, in_data, in_formats = [], False, False
    for line in lines:
        stripped = line.rstrip("\n")
        if not line.startswith(" "):
            in_data = stripped == "data:"
        if in_data and stripped.strip() == "formats:":
            in_formats = True
        elif in_formats and not stripped.lstrip().startswith("- "):
            in_formats = False
        if in_formats and stripped.strip() in ("- other", "- 'other'", '- "other"'):
            continue
        out.append(line)
    path.write_text("".join(out))
    after = load_yaml(path)
    expect = {**rec, "data": {**rec["data"], "formats": new}}
    if after != expect:
        path.write_text("".join(lines))
        raise SystemExit(f"{rel(path)}: unexpected layout of data.formats; left unchanged")
    return f"{rec['data']['formats']} -> {new}"


def fix_branch(branch, dry_run, attempts=3):
    for _ in range(attempts):
        try:
            run("fetch", "-q", "origin", "main", f"+{branch}:refs/remotes/origin/{branch}")
        except SystemExit:
            return "skipped: branch no longer exists"
        ref = f"origin/{branch}"
        paths = [p for p in run("diff", "--name-only", "--diff-filter=AM", f"origin/main...{ref}", "--", "datasets/").split()
                 if p.endswith(".yaml")]
        if len(paths) != 1:
            return f"skipped: expected one record, found {paths}"
        formats = yaml.load(run("show", f"{ref}:{paths[0]}"), Loader=_NoDatesLoader)["data"]["formats"]
        new = fixed(formats)
        if new is None:
            return "other only (kept)" if formats == ["other"] else "nothing to fix"
        if dry_run:
            return f"would change {formats} -> {new}"
        tmp = _worktree(ref)
        try:
            fix_file(tmp / paths[0])
            run("add", paths[0], cwd=tmp)
            run("commit", "-q", "-m", f"{FORMAT_PREFIX}drop `other` next to known formats ({', '.join(new)})", cwd=tmp)
            r = subprocess.run(["git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], cwd=tmp,
                               capture_output=True, text=True)
        finally:
            _cleanup(tmp)
        if r.returncode == 0:
            return f"changed {formats} -> {new}"
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
                print(f"#{pr['number']}: {fix_branch(pr['headRefName'], a.dry_run)}")
        return
    targets = list(iter_record_paths([DATASETS_DIR])) if a.all else [Path(f) for f in a.files]
    if not targets:
        ap.error("give files, --all or --pr-branches")
    for p in targets:
        msg = fix_file(p)
        if msg:
            print(f"{rel(p)}: {msg}")


if __name__ == "__main__":
    main()
