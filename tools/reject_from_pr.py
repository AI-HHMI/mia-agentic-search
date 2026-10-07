"""Record every dataset added by a closed-unmerged PR as rejected.

    python tools/reject_from_pr.py <base_sha> <head_sha> --out <path/to/rejected.yaml>

The workflow writes --out inside a worktree of the unprotected `rejections` branch,
because GITHUB_TOKEN cannot push to the protected main branch.

The record id is always rejected. Its identity keys (DOI, accession, landing URL) are rejected only when no
record on main or on another open claude/dataset/* branch still has them: a PR closed as a duplicate shares
those keys with the record that was kept, and rejecting them would reject that one too.
"""
import argparse
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import (ROOT, _NoDatesLoader, dump_yaml, identity_keys, iter_record_paths,  # noqa: E402
                          load_rejected, load_yaml, pending_records)


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base")
    ap.add_argument("head")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    out = Path(a.out)
    existing = (load_yaml(out) or {}).get("rejected") or [] if out.exists() else []
    rejected = list(dict.fromkeys(existing + load_rejected()))

    merge_base = git("merge-base", a.base, a.head).strip()
    added = git("diff", "--name-only", "--diff-filter=A", merge_base, a.head, "--", "datasets/").split()
    new, kept = [], []
    others = [load_yaml(p) or {} for p in iter_record_paths()] + list(pending_records().values())
    for path in added:
        if not path.endswith(".yaml"):
            continue
        rec = yaml.load(git("show", f"{a.head}:{path}"), Loader=_NoDatesLoader) or {}
        held = {k for o in others if o.get("id") != rec.get("id") for k in identity_keys(o)}
        kept += sorted(set(identity_keys(rec)) & held)
        for key in [rec.get("id"), *(k for k in identity_keys(rec) if k not in held)]:
            if key and key not in rejected:
                rejected.append(key)
                new.append(key)
    out.parent.mkdir(parents=True, exist_ok=True)
    dump_yaml({"rejected": rejected}, out)
    print(f"added {len(new)} rejected key(s) from {len(added)} file(s)"
          + (f"; not rejected, another record has them: {kept}" if kept else ""))


if __name__ == "__main__":
    main()
