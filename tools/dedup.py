"""Check whether a candidate dataset is already in the catalog or was rejected.

    python tools/dedup.py --doi 10.6019/EMPIAR-10311
    python tools/dedup.py --repository EMPIAR --accession EMPIAR-10311
    python tools/dedup.py --url https://cremi.org/ --title "CREMI challenge"
    python tools/dedup.py ... --paper-doi 10.1038/s41597-020-00608-w --download-url <url>

Besides exact identity (DOI, accession, landing URL) it looks for likely re-deposits and versions of
the same dataset on main and in open PRs: a shared paper DOI, the same download URL, or a title
≥ 85% similar. Those come back as "possible-duplicate": open both pages and decide.

Checks main, the rejected list, and datasets already proposed on open claude/dataset/* branches
(run `git fetch origin` first).
Prints JSON {"status": "new" | "duplicate" | "pending" | "rejected" | "possible-duplicate", "matches": [...]}.
Exit code 0 = new, 1 = duplicate/pending/rejected, 3 = possible duplicate (check manually).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import (ROOT, identity_keys, iter_record_paths, load_rejected, load_yaml,  # noqa: E402
                          pending_records, similarity_reasons)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doi")
    ap.add_argument("--repository", default="")
    ap.add_argument("--accession")
    ap.add_argument("--url")
    ap.add_argument("--title")
    ap.add_argument("--paper-doi", action="append", default=[], help="DOI of a paper describing the data (repeatable)")
    ap.add_argument("--download-url")
    a = ap.parse_args()

    cand = {"doi": a.doi, "repository": a.repository, "accession": a.accession, "landing_url": a.url,
            "title": a.title, "publications": [{"doi": d} for d in a.paper_doi],
            "data": {"download_url": a.download_url}}
    keys = set(identity_keys(cand))
    if not keys and not (a.title or a.paper_doi or a.download_url):
        ap.error("give at least one of --doi, --accession, --url, --title, --paper-doi, --download-url")

    rejected = set(load_rejected())
    if keys & rejected:
        print(json.dumps({"status": "rejected", "matches": sorted(keys & rejected)}))
        sys.exit(1)

    open_prs = pending_records()
    pending = [{"branch": b, "keys": sorted(keys & set(identity_keys(r)))}
               for b, r in open_prs.items() if keys & set(identity_keys(r))]
    if pending:
        print(json.dumps({"status": "pending", "matches": pending}))
        sys.exit(1)

    exact, fuzzy = [], []
    for p in iter_record_paths():
        rec = load_yaml(p) or {}
        rel = str(p.relative_to(ROOT))
        hit = keys & set(identity_keys(rec))
        if hit:
            exact.append({"file": rel, "keys": sorted(hit)})
        elif why := similarity_reasons(cand, rec):
            fuzzy.append({"file": rel, "why": why})
    for b, rec in open_prs.items():  # an open PR may be a re-deposit of the same data, too
        if why := similarity_reasons(cand, rec):
            fuzzy.append({"branch": b, "why": why})

    if exact:
        print(json.dumps({"status": "duplicate", "matches": exact}))
        sys.exit(1)
    if fuzzy:
        print(json.dumps({"status": "possible-duplicate", "matches": fuzzy}))
        sys.exit(3)
    print(json.dumps({"status": "new", "matches": []}))


if __name__ == "__main__":
    main()
