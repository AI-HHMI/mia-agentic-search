"""Decide which dataset PRs are merged automatically, and merge them (CI only).

    python tools/automerge.py              # report only: which PRs qualify and why others don't
    python tools/automerge.py --merge      # also merge the qualifying ones

A PR qualifies when the record on its current head commit gets all of these labels
(computed with tools/labels.py, i.e. exactly the labels the PR shows once labelling has run):
  - enriched                      the enricher has inspected the files
  - dim:2D, dim:2D+t, dim:3D or dim:3D+t      (any dimensionality, as long as it is set)
  - size:<1GB | 1-10GB | 10-50GB | 50-500GB    (below 500 GB; not size:unknown)
  - a known license: license:<spdx> other than license:unknown (any license, incl. custom, for now)
  - at least one fmt:… and no fmt:other         every data format is known
and also: the `validate` check passed on that commit, the PR is not a draft, has no HOLD_LABELS,
GitHub reports it mergeable, and it isn't a possible duplicate (tools/common.py:similarity_reasons:
same paper DOI, same download URL or landing page, or a ≥ 85% similar title) of a record on main,
of a PR merged earlier in the same run, or of another open PR. Possible duplicates wait for a human. Merging uses --match-head-commit, so a push that lands while
this runs stops the merge. Merged branches are deleted (the on-pr-closed workflow doesn't run for
merges made by the workflow token, and a leftover branch makes dedup report the dataset as pending).

Edit the constants below to change the policy.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import DATASET_BRANCH_PREFIX, ROOT, iter_record_paths, load_yaml, similarity_reasons  # noqa: E402
from tools.labels import labels_for, record_on_pr  # noqa: E402

REQUIRED = ["enriched"]
ALLOWED_DIMS = ["dim:2D", "dim:2D+t", "dim:3D", "dim:3D+t"]  # all four; must just be set
ALLOWED_SIZES = ["size:<1GB", "size:1-10GB", "size:10-50GB", "size:50-500GB"]
# The license only has to be known for now (not `unknown`); which licenses are acceptable is decided later.
# Set this to a list of SPDX ids to allow only those again (e.g. ["CC0-1.0", "CC-BY-4.0", "BSD-3-Clause"]).
ALLOWED_LICENSES = None
HOLD_LABELS = ["hold", "do-not-merge"]


def gh(*args, check=True):
    r = subprocess.run(["gh", *args], cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def criteria_failures(labels):
    """Reasons a PR with these labels doesn't qualify (empty list = qualifies)."""
    fails = [f"missing `{lab}`" for lab in REQUIRED if lab not in labels]
    if not set(labels) & set(ALLOWED_DIMS):
        dims = [lab for lab in labels if lab.startswith("dim:")]
        fails.append(f"dimensionality not allowed ({', '.join(dims) or 'no dim label'})")
    size = [lab for lab in labels if lab.startswith("size:")]
    if not set(size) & set(ALLOWED_SIZES):
        fails.append(f"size not below 500 GB ({', '.join(size) or 'no size label'})")
    lic = [lab.removeprefix("license:") for lab in labels if lab.startswith("license:")]
    if not lic or "unknown" in lic or "license-verification-needed" in labels:
        fails.append("license unknown (license-verification-needed)")
    elif ALLOWED_LICENSES is not None and not set(lic) & set(ALLOWED_LICENSES):
        fails.append(f"license not on the allow-list ({', '.join(lic)})")
    fmts = [lab for lab in labels if lab.startswith("fmt:")]
    if not fmts:
        fails.append("no data format")
    elif "fmt:other" in fmts:
        fails.append("a data format is `other` (unknown)")
    return fails


def validate_passed(repo, sha):
    runs = json.loads(gh("api", f"repos/{repo}/commits/{sha}/check-runs?check_name=validate"))["check_runs"]
    return bool(runs) and all(r["status"] == "completed" and r["conclusion"] == "success" for r in runs)


def evaluate(repo, pr):
    rec, paths = record_on_pr(pr["number"])
    if rec is None:
        return None, [f"expected one record, found {paths}"], None
    sha = subprocess.run(["git", "rev-parse", f"refs/remotes/origin/pr-{pr['number']}"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.strip()
    fails = criteria_failures(labels_for(rec))
    if sha != pr["headRefOid"]:
        fails.append("head moved while checking")
    if pr["isDraft"]:
        fails.append("draft")
    held = [lab["name"] for lab in pr["labels"] if lab["name"] in HOLD_LABELS]
    if held:
        fails.append(f"on hold ({', '.join(held)})")
    if not validate_passed(repo, sha):
        fails.append("`validate` check has not passed on the head commit")
    if pr["mergeable"] == "CONFLICTING":
        fails.append("merge conflict")
    return rec, fails, sha


def duplicate_reasons(rec, others):
    """'possible duplicate of <id> (...)' for each record in `others` that looks like the same dataset."""
    return [f"possible duplicate of {o.get('id')} ({', '.join(why)})"
            for o in others if o is not rec and o.get("id") != rec.get("id") and (why := similarity_reasons(rec, o))]



def merge(repo, pr, rec, sha):
    gh("pr", "merge", str(pr["number"]), "--merge", "--match-head-commit", sha,
       "--subject", f"Auto-merge #{pr['number']}: {rec['short_name']}")
    gh("api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{pr['headRefName']}", check=False)
    lic = rec["license"]["spdx"]
    gh("pr", "comment", str(pr["number"]), "--body",
       f"🤖 **Auto-merged**: enriched, dimensionality set, size below 500 GB, license known (`{lic}`), "
       f"all data formats known, `validate` passed. Policy: `tools/automerge.py`.", check=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--merge", action="store_true", help="merge qualifying PRs (default: report only)")
    a = ap.parse_args()
    repo = os.environ.get("GH_REPO") or gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner").strip()
    prs = json.loads(gh("pr", "list", "--state", "open", "--limit", "500", "--json",
                        "number,title,headRefName,headRefOid,isDraft,labels,mergeable"))
    rows, merged = [], 0
    catalog = [load_yaml(p) for p in iter_record_paths()]  # main; merged records are appended below
    checked = []
    for pr in sorted(prs, key=lambda p: p["number"]):
        if not pr["headRefName"].startswith(DATASET_BRANCH_PREFIX):
            continue
        try:
            rec, fails, sha = evaluate(repo, pr)
        except Exception as e:  # noqa: BLE001
            rec, fails, sha = None, [f"error: {e}"], None
        checked.append((pr, rec, fails, sha))
    open_recs = [rec for _, rec, _, _ in checked if rec]
    for pr, rec, fails, sha in checked:
        if rec:
            fails = fails + duplicate_reasons(rec, catalog + open_recs)
        verdict = "qualifies" if not fails else "stays open"
        if not fails and a.merge:
            try:
                merge(repo, pr, rec, sha)
                verdict, merged = "**merged**", merged + 1
                catalog.append(rec)
            except RuntimeError as e:
                verdict, fails = "merge failed", [str(e)[:300]]
        rows.append(f"| #{pr['number']} | {pr['title']} | {verdict} | {'; '.join(fails) or '—'} |")
        print(f"#{pr['number']} {verdict}: {'; '.join(fails) or 'all criteria met'}")
    summary = ["## Auto-merge " + ("(merging)" if a.merge else "(report only: set repo variable AUTO_MERGE=true to merge)"),
               "", "| PR | Dataset | Result | Why not |", "|---|---|---|---|", *rows]
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text("\n".join(summary) + "\n")
    if merged:  # merges by the workflow token don't trigger push workflows; rebuild the dashboard explicitly
        gh("workflow", "run", "pages.yml", check=False)
    print(f"{merged} merged")


if __name__ == "__main__":
    main()
