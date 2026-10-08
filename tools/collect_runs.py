"""Gather run logs from the working tree plus the state branch (dashboard, watchdog, digest).

Requires `git fetch origin +state:refs/remotes/origin/state` beforehand.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import RUNS_DIR, STATE_BRANCH, git_objects, tree_names  # noqa: E402


def collect(include_dry_runs=False):
    runs = {p.name: json.loads(p.read_text()) for p in RUNS_DIR.glob("*.json")} if RUNS_DIR.exists() else {}
    # two batched reads (the run-log folder, then the logs) instead of one git call per file
    spec = f"origin/{STATE_BRANCH}:state/runs"
    tree = git_objects([spec]).get(spec)
    wanted = {name: f"{spec}/{name}" for name in (tree_names(tree[1]) if tree and tree[0] == "tree" else [])
              if name.endswith(".json") and name not in runs}
    blobs = git_objects(wanted.values())
    for name, s in wanted.items():
        try:
            runs[name] = json.loads(blobs[s][1])
        except (KeyError, json.JSONDecodeError):
            continue
    out = [r for r in runs.values() if include_dry_runs or not r.get("dry_run")]
    return sorted(out, key=lambda r: r["started_at"], reverse=True)


if __name__ == "__main__":
    print(json.dumps(collect(), indent=1))
