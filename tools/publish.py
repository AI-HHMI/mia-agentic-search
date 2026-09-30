"""Git plumbing for harvest runs, so agents never juggle branches by hand.

    python tools/publish.py state-pull --routine R     # restore frontier + run logs from claude/state/R
    python tools/publish.py dataset <record.yaml>       # push record to its own branch claude/dataset/<id>
    python tools/publish.py state-push --routine R     # save frontier + run logs to claude/state/R
    python tools/publish.py pr-pull <id>               # enricher: put claude/dataset/<id>'s record in the tree
    python tools/publish.py pr-update <record.yaml>    # enricher: commit it back onto that branch

The working tree stays on origin/main. Records are left uncommitted there and each one is
committed in a temporary worktree, so every dataset branch contains exactly one new file.
State branches are orphans holding only state/ files; they are pushed directly and never reviewed.

pr-pull records the branch head it read (state/.enrich/, git-ignored). pr-update commits on top of
exactly that head and pushes without force, so if anyone pushed in the meantime the push is
refused instead of overwriting their edit: pull again and redo the change.
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import (DATASET_BRANCH_PREFIX, FRONTIER_DIR, ROOT, RUNS_DIR,  # noqa: E402
                          STATE_BRANCH_PREFIX, STATE_DIR, git, load_yaml, rel)
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


ENRICH_DIR = STATE_DIR / ".enrich"
ENRICH_PREFIX = "enrich: "  # commit subject prefix; any other later commit on a dataset branch is a human edit


def pr_pull(rec_id):
    branch = DATASET_BRANCH_PREFIX + rec_id
    if not remote_exists(branch):
        sys.exit(f"{branch} does not exist on origin")
    run("fetch", "-q", "origin", "main", f"+{branch}:refs/remotes/origin/{branch}")
    ref = f"origin/{branch}"
    sha = run("rev-parse", ref).strip()
    paths = [p for p in run("diff", "--name-only", "--diff-filter=AM", f"origin/main...{ref}", "--", "datasets/").split()
             if p.endswith(".yaml")]
    if len(paths) != 1:
        sys.exit(f"{branch} should change exactly one record, found {paths}")
    path = paths[0]
    text = run("show", f"{ref}:{path}")
    (ROOT / path).parent.mkdir(parents=True, exist_ok=True)
    (ROOT / path).write_text(text)
    ENRICH_DIR.mkdir(parents=True, exist_ok=True)
    baseline = ENRICH_DIR / f"{rec_id}.orig.yaml"
    baseline.write_text(text)
    (ENRICH_DIR / f"{rec_id}.json").write_text(json.dumps({"branch": branch, "sha": sha, "path": path}))
    commits = []
    for line in run("log", "--reverse", "--format=%H%x09%an%x09%cn%x09%s", f"origin/main..{ref}").splitlines():
        h, author, committer, subject = line.split("\t", 3)
        commits.append({"sha": h[:10], "author": author, "committer": committer, "subject": subject})
    human = [c for c in commits[1:] if not c["subject"].startswith(ENRICH_PREFIX)]
    print(json.dumps({"path": path, "baseline": rel(baseline), "branch": branch, "head": sha[:10],
                      "enriched_before": any(c["subject"].startswith(ENRICH_PREFIX) for c in commits),
                      "human_edits": human, "commits": commits}, indent=1))


def pr_update(record_path):
    src = Path(record_path).resolve()
    rec = load_yaml(src)
    meta_path = ENRICH_DIR / f"{rec['id']}.json"
    if not meta_path.exists():
        sys.exit(f"run `publish.py pr-pull {rec['id']}` first")
    meta = json.loads(meta_path.read_text())
    tmp = _worktree(meta["sha"])
    try:
        dest = tmp / src.relative_to(ROOT)
        if dest != tmp / meta["path"]:  # repository changed, so the record moves to another folder
            run("rm", "-q", meta["path"], cwd=tmp)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        run("add", str(dest.relative_to(tmp)), cwd=tmp)
        if not git("-C", str(tmp), "status", "--porcelain").strip():
            print("record unchanged; nothing pushed")
            return
        run("commit", "-q", "-m", ENRICH_PREFIX + rec["short_name"], cwd=tmp)
        r = subprocess.run(["git", "push", "-q", "origin", f"HEAD:refs/heads/{meta['branch']}"], cwd=tmp,
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"push refused: {meta['branch']} changed since pr-pull (someone pushed?). "
                     f"Run pr-pull again and redo your edits.\n{r.stderr.strip()}")
    finally:
        _cleanup(tmp)
    src.unlink()
    meta_path.unlink()
    print(f"pushed {meta['branch']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("state-pull", "state-push"):
        sub.add_parser(name).add_argument("--routine", required=True)
    sub.add_parser("dataset").add_argument("record")
    sub.add_parser("pr-pull").add_argument("id")
    sub.add_parser("pr-update").add_argument("record")
    a = ap.parse_args()
    if a.cmd == "state-pull":
        state_pull(a.routine)
    elif a.cmd == "state-push":
        state_push(a.routine)
    elif a.cmd == "pr-pull":
        pr_pull(a.id)
    elif a.cmd == "pr-update":
        pr_update(a.record)
    else:
        print(f"{rel(Path(a.record).resolve())} -> ", end="", flush=True)
        dataset(a.record)


if __name__ == "__main__":
    main()
