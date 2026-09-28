"""Check whether a candidate dataset is already in the catalog or was rejected.

    python tools/dedup.py --doi 10.6019/EMPIAR-10311
    python tools/dedup.py --repository EMPIAR --accession EMPIAR-10311
    python tools/dedup.py --url https://cremi.org/ --title "CREMI challenge"

Prints JSON {"status": "new" | "duplicate" | "rejected" | "possible-duplicate", "matches": [...]}.
Exit code 0 = new, 1 = duplicate/rejected, 3 = possible duplicate (check manually).
"""
import argparse
import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import (REJECTED_PATH, ROOT, identity_keys, iter_record_paths,  # noqa: E402
                          load_yaml, normalize_title)

FUZZY = 0.85


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doi")
    ap.add_argument("--repository", default="")
    ap.add_argument("--accession")
    ap.add_argument("--url")
    ap.add_argument("--title")
    a = ap.parse_args()

    cand = {"doi": a.doi, "repository": a.repository, "accession": a.accession, "landing_url": a.url}
    keys = set(identity_keys(cand))
    if not keys and not a.title:
        ap.error("give at least one of --doi, --accession, --url, --title")

    rejected = set((load_yaml(REJECTED_PATH) or {}).get("rejected", [])) if REJECTED_PATH.exists() else set()
    if keys & rejected:
        print(json.dumps({"status": "rejected", "matches": sorted(keys & rejected)}))
        sys.exit(1)

    exact, fuzzy = [], []
    t = normalize_title(a.title)
    for p in iter_record_paths():
        rec = load_yaml(p) or {}
        rel = str(p.relative_to(ROOT))
        hit = keys & set(identity_keys(rec))
        if hit:
            exact.append({"file": rel, "keys": sorted(hit)})
        elif t:
            ratio = difflib.SequenceMatcher(None, t, normalize_title(rec.get("title"))).ratio()
            if ratio >= FUZZY:
                fuzzy.append({"file": rel, "title_similarity": round(ratio, 3)})

    if exact:
        print(json.dumps({"status": "duplicate", "matches": exact}))
        sys.exit(1)
    if fuzzy:
        print(json.dumps({"status": "possible-duplicate", "matches": fuzzy}))
        sys.exit(3)
    print(json.dumps({"status": "new", "matches": []}))


if __name__ == "__main__":
    main()
