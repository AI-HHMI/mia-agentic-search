"""Validate dataset records against the schema and catalog-wide rules.

    python tools/validate.py [paths...] [--run-log state/runs/<file>.json] [--min-confidence 0.5]

Exit code 0 = all records valid, 1 = errors found.
"""
import argparse
import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import (CONFIDENCE_CAPS, PAPER_HOSTS, load_rejected, DATASETS_DIR, identity_keys, iter_record_paths,  # noqa: E402
                          load_yaml, normalize_title, normalize_url, rel, validator)

FUZZY_TITLE_THRESHOLD = 0.92


def check_record(path, record, v):
    errors = []
    for e in sorted(v.iter_errors(record), key=lambda e: list(e.absolute_path)):
        loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
        errors.append(f"{loc}: {e.message}")
    if not isinstance(record, dict):
        return errors
    if record.get("id") != path.stem:
        errors.append(f"id {record.get('id')!r} must equal file name {path.stem!r}")
    pubs = record.get("publications") or []
    evidence = (record.get("provenance") or {}).get("evidence_urls") or []
    pub_links = [p.get("doi") or p.get("url") for p in pubs if isinstance(p, dict)]
    if any(pub_links) and not any(any(h in u.lower() for h in PAPER_HOSTS) or any(l and l.lower() in u.lower() for l in pub_links)
                                  for u in evidence if isinstance(u, str)):
        errors.append("a paper is listed but no paper source (full text / article page) is in evidence_urls; "
                      "read it with tools/paper.py and cite the URLs it used")
    repo = record.get("repository")
    if isinstance(repo, str) and path.parent.name != repo.lower():
        errors.append(f"file must live in datasets/{repo.lower()}/ (repository={repo})")
    return errors


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="files/dirs to validate (default: datasets/)")
    ap.add_argument("--run-log", help="require every evidence_url to appear in this run log's fetched URLs")
    ap.add_argument("--min-confidence", type=float, default=None,
                    help="fail records below this provenance.confidence (harvest PRs use 0.5)")
    a = ap.parse_args()

    v = validator()
    targets = list(iter_record_paths(a.paths))
    all_paths = list(iter_record_paths([DATASETS_DIR]))
    rejected = set(load_rejected())
    fetched = None
    if a.run_log:
        fetched = {normalize_url(u) for u in json.loads(Path(a.run_log).read_text()).get("fetched_urls", [])}

    problems, warnings = {}, {}
    records = {}
    for p in all_paths:
        try:
            records[p] = load_yaml(p)
        except Exception as e:  # noqa: BLE001
            problems.setdefault(p, []).append(f"YAML parse error: {e}")
    for p in targets:
        if p not in records:
            try:
                records[p] = load_yaml(p)
            except Exception as e:  # noqa: BLE001
                problems.setdefault(p, []).append(f"YAML parse error: {e}")

    target_set = set(targets)
    for p in targets:
        rec = records.get(p)
        if rec is None:
            continue
        errs = check_record(p, rec, v)
        if isinstance(rec, dict):
            if rec.get("id") in rejected:
                errs.append(f"id {rec['id']!r} was rejected (state/rejected.yaml or rejections branch)")
            for k in identity_keys(rec):
                if k in rejected:
                    errs.append(f"{k} was rejected (state/rejected.yaml or rejections branch)")
            conf = (rec.get("provenance") or {}).get("confidence")
            ver = rec.get("verification") or {}
            if isinstance(conf, (int, float)):
                for flag, cap, why in CONFIDENCE_CAPS:
                    if ver.get(flag) is False and conf > cap:
                        errs.append(f"confidence {conf} exceeds cap {cap} ({why}: verification.{flag}=false)")
            if a.min_confidence is not None and isinstance(conf, (int, float)) and conf < a.min_confidence:
                errs.append(f"confidence {conf} < {a.min_confidence}")
            if fetched is not None:
                for u in (rec.get("provenance") or {}).get("evidence_urls", []):
                    if normalize_url(u) not in fetched:
                        errs.append(f"evidence url not in run log fetched_urls: {u}")
        if errs:
            problems.setdefault(p, []).extend(errs)

    # Catalog-wide uniqueness: the tree being validated contains main + the PR's records.
    seen = {}
    titles = {}
    for p, rec in records.items():
        if not isinstance(rec, dict):
            continue
        for k in identity_keys(rec):
            if k.startswith("url:"):
                continue  # several datasets can share a landing page (e.g. challenge sites)
            if k in seen and seen[k] != p:
                for q in (p, seen[k]):
                    if q in target_set:
                        other = seen[k] if q == p else p
                        problems.setdefault(q, []).append(f"duplicate {k} (also in {rel(other)})")
            seen.setdefault(k, p)
        titles[p] = normalize_title(rec.get("title"))
    tlist = list(titles.items())
    for i, (p, t) in enumerate(tlist):
        for q, u in tlist[i + 1:]:
            if t and u and (p in target_set or q in target_set):
                if difflib.SequenceMatcher(None, t, u).ratio() >= FUZZY_TITLE_THRESHOLD:
                    warnings.setdefault(p if p in target_set else q, []).append(
                        f"title very similar to {rel(q if p in target_set else p)}; possible duplicate")

    for p, ws in warnings.items():
        for w in ws:
            print(f"WARN  {rel(p)}: {w}")
    for p, errs in problems.items():
        for e in dict.fromkeys(errs):
            print(f"ERROR {rel(p)}: {e}")
    print(f"{len(targets)} record(s) checked, {len(problems)} with errors, {len(warnings)} with warnings")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
