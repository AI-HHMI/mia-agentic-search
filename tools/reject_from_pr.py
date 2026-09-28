"""Record every dataset added by a closed-unmerged PR in state/rejected.yaml.

    python tools/reject_from_pr.py <base_sha> <head_sha>
"""
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import REJECTED_PATH, ROOT, _NoDatesLoader, dump_yaml, identity_keys, load_yaml  # noqa: E402


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def main():
    base, head = sys.argv[1], sys.argv[2]
    merge_base = git("merge-base", base, head).strip()
    added = git("diff", "--name-only", "--diff-filter=A", merge_base, head, "--", "datasets/").split()
    data = (load_yaml(REJECTED_PATH) if REJECTED_PATH.exists() else None) or {}
    rejected = data.setdefault("rejected", [])
    new = []
    for path in added:
        if not path.endswith(".yaml"):
            continue
        rec = yaml.load(git("show", f"{head}:{path}"), Loader=_NoDatesLoader) or {}
        for key in [rec.get("id"), *identity_keys(rec)]:
            if key and key not in rejected:
                rejected.append(key)
                new.append(key)
    dump_yaml(data, REJECTED_PATH)
    print(f"added {len(new)} rejected key(s) from {len(added)} file(s)")


if __name__ == "__main__":
    main()
