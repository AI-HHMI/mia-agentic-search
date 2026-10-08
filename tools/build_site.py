"""Build the static monitoring dashboard (GitHub Pages) from records + run logs.

    python tools/build_site.py [--out site]

Writes <out>/index.html (self-contained) and <out>/catalog.json. Run logs come from main
plus the fetched state branch (tools/collect_runs.py).
The "not merged" table lists open dataset PRs with the reasons auto-merge gives (tools/automerge.py),
read from the PRs' labels and checks with `gh`; without GitHub access that table is left out.
The gallery at the end shows the datasets downloaded into demo/data/ (tools/demo_gallery.py), with
Fileglancer links. Where demo/data/ doesn't exist (demo/ is git-ignored, e.g. on GitHub) it uses the copy the
workstation publishes to the state branch, and it's left out if there is none.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.collect_runs import collect  # noqa: E402
from tools.automerge import HOLD_LABELS, criteria_failures, duplicate_reasons  # noqa: E402
from tools.demo_gallery import gallery, published_gallery  # noqa: E402
from tools.common import DATASET_BRANCH_PREFIX, ROOT, iter_record_paths, load_yaml, pending_records  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent / "site_template.html"
RUN_FIELDS = ("routine", "started_at", "finished_at", "status", "counts", "pr_urls", "pr_url", "duration_s", "dry_run", "stop_reason")


def unmerged_prs():
    """Open dataset PRs and why auto-merge leaves them open; None if GitHub can't be read."""
    try:
        out = subprocess.run(["gh", "pr", "list", "--state", "open", "--limit", "500", "--json",
                              "number,title,url,headRefName,labels,isDraft,createdAt,statusCheckRollup"],
                             cwd=ROOT, capture_output=True, text=True, timeout=120, check=True).stdout
    except (OSError, subprocess.SubprocessError) as e:
        print(f"note: open PRs not included ({(getattr(e, 'stderr', '') or str(e))[:300]})", file=sys.stderr)
        return None
    prs = [pr for pr in json.loads(out) if pr["headRefName"].startswith(DATASET_BRANCH_PREFIX)]
    # branch -> record, from the fetched refs of open PRs only (stale refs of closed PRs aren't duplicates)
    open_recs = pending_records([pr["headRefName"] for pr in prs])
    catalog = [load_yaml(p) for p in iter_record_paths()]
    rows = []
    for pr in prs:
        labels = [lab["name"] for lab in pr["labels"]]
        reasons = criteria_failures(labels)
        checks = [c for c in pr.get("statusCheckRollup") or [] if c.get("name") == "validate"]
        if not checks or any(c.get("conclusion") != "SUCCESS" for c in checks):
            reasons.append("`validate` check has not passed" if checks else "`validate` has not run")
        if pr["isDraft"]:
            reasons.append("draft")
        held = [lab for lab in labels if lab in HOLD_LABELS]
        if held:
            reasons.append(f"on hold ({', '.join(held)})")
        rec = open_recs.get(pr["headRefName"])
        if rec:
            reasons += duplicate_reasons(rec, catalog + list(open_recs.values()))
        rows.append({"number": pr["number"], "title": pr["title"], "url": pr["url"], "created_at": pr["createdAt"],
                     "dim": next((lab[4:] for lab in labels if lab.startswith("dim:")), None),
                     "reasons": reasons or ["meets all criteria; merges on the next auto-merge run"]})
    return sorted(rows, key=lambda r: r["number"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    a = ap.parse_args()
    out = ROOT / a.out
    out.mkdir(parents=True, exist_ok=True)

    records = []
    for p in iter_record_paths():
        r = load_yaml(p)
        r["_file"] = str(p.relative_to(ROOT))
        records.append(r)
    runs = [{k: r.get(k) for k in RUN_FIELDS} for r in collect()]

    (out / "catalog.json").write_text(json.dumps(records, indent=1, ensure_ascii=False))
    payload = json.dumps({"records": records, "runs": runs[:500], "unmerged": unmerged_prs(),
                          "gallery": gallery() or published_gallery()},
                         ensure_ascii=False).replace("</", "<\\/")
    (out / "index.html").write_text(TEMPLATE.read_text().replace("/*__DATA__*/null", payload))
    print(f"wrote {out.relative_to(ROOT)}/index.html ({len(records)} records, {len(runs)} runs)")


if __name__ == "__main__":
    main()
