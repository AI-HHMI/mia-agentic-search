"""Git plumbing for harvest runs, so agents never juggle branches by hand.

    python tools/publish.py state-pull --routine R     # restore frontier + run logs from claude/state/R
    python tools/publish.py dataset <record.yaml>       # push record to its own branch claude/dataset/<id>
    python tools/publish.py state-push --routine R     # save frontier + run logs to claude/state/R

The working tree stays on origin/main. Records are left uncommitted there and each one is
committed in a temporary worktree, so every dataset branch contains exactly one new file.
State branches are orphans holding only state/ files; they are pushed directly and never reviewed.
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import (DATASET_BRANCH_PREFIX, FRONTIER_DIR, ROOT, RUNS_DIR,  # noqa: E402
                          STATE_BRANCH_PREFIX, git, load_yaml, rel)
from tools.pr_text import title  # noqa: E402


def run(*args, cwd=ROOT):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{r.stderr.strip()}")
    return r.stdout


def remote_exists(branch):
    return bool(git("ls-remote", "--heads", "origin", branch).strip())


def state_paths(routine):
    return [FRONTIER_DIR / f"{routine}.yaml", *sorted(RUNS_DIR.glob(f"{routine}-*.json"))]


def state_pull(routine):
    branch = STATE_BRANCH_PREFIX + routine
    if not remote_exists(branch):
        print(f"no {branch} yet; starting from the frontier on main")
        return
    run("fetch", "-q", "origin", f"+{branch}:refs/remotes/origin/{branch}")
    n = 0
    for path in git("ls-tree", "-r", "--name-only", f"origin/{branch}", "state/").split():
        dest = ROOT / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(run("show", f"origin/{branch}:{path}"))
        n += 1
    print(f"restored {n} state file(s) from {branch}")


def _worktree(start_ref=None, orphan_branch=None):
    tmp = Path(tempfile.mkdtemp(prefix="mia-wt-"))
    shutil.rmtree(tmp)
    if orphan_branch:
        git("branch", "-D", orphan_branch)  # leftover from an interrupted run
        run("worktree", "add", "-q", "--orphan", "-b", orphan_branch, str(tmp))
    else:
        run("worktree", "add", "-q", "--detach", str(tmp), start_ref)
    return tmp


def _cleanup(tmp):
    git("worktree", "remove", "--force", str(tmp))


def state_push(routine):
    branch = STATE_BRANCH_PREFIX + routine
    if remote_exists(branch):
        run("fetch", "-q", "origin", f"+{branch}:refs/remotes/origin/{branch}")
        tmp = _worktree(f"origin/{branch}")
    else:
        tmp = _worktree(orphan_branch=f"tmp-state-{routine}")
    try:
        for src in state_paths(routine):
            if src.exists():
                dest = tmp / src.relative_to(ROOT)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
        run("add", "state", cwd=tmp)
        if git("-C", str(tmp), "status", "--porcelain").strip():
            run("commit", "-q", "-m", f"state({routine}): frontier + run logs", cwd=tmp)
            run("push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=tmp)
            print(f"pushed {branch}")
        else:
            print("state unchanged")
    finally:
        _cleanup(tmp)
        git("branch", "-D", f"tmp-state-{routine}")


def dataset(record_path):
    src = Path(record_path).resolve()
    rec = load_yaml(src)
    branch = DATASET_BRANCH_PREFIX + rec["id"]
    if remote_exists(branch):
        sys.exit(f"{branch} already exists on origin; this dataset is already proposed")
    run("fetch", "-q", "origin", "main")
    tmp = _worktree("origin/main")
    try:
        dest = tmp / src.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        run("add", str(dest.relative_to(tmp)), cwd=tmp)
        run("commit", "-q", "-m", title(rec), cwd=tmp)
        run("push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=tmp)
    finally:
        _cleanup(tmp)
    print(branch)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("state-pull", "state-push"):
        sub.add_parser(name).add_argument("--routine", required=True)
    sub.add_parser("dataset").add_argument("record")
    a = ap.parse_args()
    if a.cmd == "state-pull":
        state_pull(a.routine)
    elif a.cmd == "state-push":
        state_push(a.routine)
    else:
        print(f"{rel(Path(a.record).resolve())} -> ", end="", flush=True)
        dataset(a.record)


if __name__ == "__main__":
    main()
