"""Check whether a candidate dataset is already in the catalog or was rejected.

    python tools/dedup.py --doi 10.6019/EMPIAR-10311
    python tools/dedup.py --repository EMPIAR --accession EMPIAR-10311
    python tools/dedup.py --url https://cremi.org/ --title "CREMI challenge"
    python tools/dedup.py ... --paper-doi 10.1038/s41597-020-00608-w --download-url <url>
    python tools/dedup.py --record <record.yaml>      # a written record (itself excluded); CI uses this

Exact identity is a shared DOI or accession. A shared landing page (without either) and likely
re-deposits or versions on main and in open PRs (common.similarity_reasons) come back as
"possible-duplicate": open both pages and decide.

Fetches main and the dataset branches first, then checks main, the rejected list and the records
proposed in open PRs.
Prints JSON {"status": "new" | "duplicate" | "pending" | "rejected" | "possible-duplicate", "matches": [...]}.
Exit code 0 = new, 1 = duplicate/pending/rejected, 3 = possible duplicate (check manually).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import (fetch_catalog, identity_keys, load_rejected, load_yaml, main_records,  # noqa: E402
                          open_pr_branches, pending_records, similarity_reasons)


def check(cand, skip_self=False):
    """(status, matches) of a candidate record against fresh main, open PRs and rejections. With skip_self,
    a record with the candidate's id is the candidate itself (its PR, or the main record it edits)."""
    keys, me = set(identity_keys(cand)), cand.get("id") if skip_self else None
    if hit := keys & set(load_rejected()):
        return "rejected", sorted(hit)

    def exact(rec):  # a shared DOI / accession; a shared landing page alone (challenge sites) is only "possible"
        return sorted(k for k in keys & set(identity_keys(rec)) if not k.startswith("url:"))

    def similar(rec):
        return similarity_reasons(cand, rec) + (["same landing page"] if keys & set(identity_keys(rec)) else [])

    fetch_catalog()
    open_prs = {b: r for b, r in pending_records(open_pr_branches()).items() if not me or r.get("id") != me}
    if pending := [{"branch": b, "keys": exact(r)} for b, r in open_prs.items() if exact(r)]:
        return "pending", pending
    main = {p: r for p, r in main_records().items() if not me or r.get("id") != me}
    if dup := [{"file": p, "keys": exact(r)} for p, r in main.items() if exact(r)]:
        return "duplicate", dup
    fuzzy = [{"file": p, "why": why} for p, r in main.items() if (why := similar(r))]
    fuzzy += [{"branch": b, "why": why} for b, r in open_prs.items() if (why := similar(r))]
    return ("possible-duplicate", fuzzy) if fuzzy else ("new", [])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record", help="check this record file instead of the fields below")
    ap.add_argument("--doi")
    ap.add_argument("--repository", default="")
    ap.add_argument("--accession")
    ap.add_argument("--url")
    ap.add_argument("--title")
    ap.add_argument("--paper-doi", action="append", default=[], help="DOI of a paper describing the data (repeatable)")
    ap.add_argument("--download-url")
    a = ap.parse_args()
    cand = load_yaml(a.record) if a.record else {
        "doi": a.doi, "repository": a.repository, "accession": a.accession, "landing_url": a.url, "title": a.title,
        "publications": [{"doi": d} for d in a.paper_doi], "data": {"download_url": a.download_url}}
    if not (a.record or identity_keys(cand) or a.title or a.paper_doi or a.download_url):
        ap.error("give --record or at least one of --doi, --accession, --url, --title, --paper-doi, --download-url")
    status, matches = check(cand, skip_self=bool(a.record))
    print(json.dumps({"status": status, "matches": matches}))
    sys.exit({"new": 0, "possible-duplicate": 3}.get(status, 1))


if __name__ == "__main__":
    main()
