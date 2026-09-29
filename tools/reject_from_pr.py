"""Record every dataset added by a closed-unmerged PR as rejected.

    python tools/reject_from_pr.py <base_sha> <head_sha> --out <path/to/rejected.yaml>

The workflow writes --out inside a worktree of the unprotected `rejections` branch,
because GITHUB_TOKEN cannot push to the protected main branch.
"""
import argparse
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import ROOT, _NoDatesLoader, dump_yaml, identity_keys, load_rejected, load_yaml  # noqa: E402


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
    new = []
    for path in added:
        if not path.endswith(".yaml"):
            continue
        rec = yaml.load(git("show", f"{a.head}:{path}"), Loader=_NoDatesLoader) or {}
        for key in [rec.get("id"), *identity_keys(rec)]:
            if key and key not in rejected:
                rejected.append(key)
                new.append(key)
    out.parent.mkdir(parents=True, exist_ok=True)
    dump_yaml({"rejected": rejected}, out)
    print(f"added {len(new)} rejected key(s) from {len(added)} file(s)")


if __name__ == "__main__":
    main()
