"""Gather run logs from the working tree plus every remote `claude/*` branch.

Harvest run logs live on each routine's PR branch until it is merged, so monitoring
(dashboard, watchdog) must look at the branches, not just main.
Requires `git fetch origin '+refs/heads/claude/*:refs/remotes/origin/claude/*'` beforehand.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import ROOT, RUNS_DIR, git_objects, tree_names  # noqa: E402


def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout


def collect(all_branches=True, include_dry_runs=False):
    runs = {}
    if RUNS_DIR.exists():
        for p in RUNS_DIR.glob("*.json"):
            runs[p.name] = json.loads(p.read_text())
    if all_branches:
        refs = _git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/claude/").split()
        # two batched reads (run-log folders, then the logs) instead of git calls per branch and file
        trees = git_objects(f"{ref}:state/runs" for ref in refs)
        wanted = {}
        for ref in refs:
            tree = trees.get(f"{ref}:state/runs")
            for name in tree_names(tree[1]) if tree and tree[0] == "tree" else []:
                if name.endswith(".json") and name not in runs and name not in wanted:
                    wanted[name] = f"{ref}:state/runs/{name}"
        blobs = git_objects(wanted.values())
        for name, spec in wanted.items():
            try:
                runs[name] = json.loads(blobs[spec][1])
            except (KeyError, json.JSONDecodeError):
                continue
    out = [r for r in runs.values() if include_dry_runs or not r.get("dry_run")]
    return sorted(out, key=lambda r: r["started_at"], reverse=True)


if __name__ == "__main__":
    print(json.dumps(collect(), indent=1))
