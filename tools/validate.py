"""Validate dataset records against the schema and catalog-wide rules.

    python tools/validate.py [paths...] [--run-log state/runs/<file>.json] [--min-confidence 0.5]
    python tools/validate.py <file> --run-log <enricher log> --baseline <record as pulled>   # enricher

Exit code 0 = all records valid, 1 = errors found.
"""
import argparse
import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import (CONFIDENCE_CAPS, CONFIRMED_SIZE_SOURCES, DATASETS_DIR, DRAFTS_DIR, PAPER_HOSTS,  # noqa: E402
                          canonical_path, identity_keys, iter_record_paths, legacy_path, load_rejected, load_yaml,
                          similarity_reasons,
                          normalize_title, normalize_url, rel, validator)



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
    formats = (record.get("data") or {}).get("formats") or []
    if "other" in formats and len(formats) > 1:
        errors.append(f"data.formats {formats}: use `other` only when no listed format applies; "
                      "drop it (say in annotations.format / notes what the remaining files are)")
    errors += check_technical(record)
    return errors


def check_location(path, record):
    """(errors, warnings) for where the file lives. Drafts outside datasets/ are not checked."""
    try:
        path.relative_to(DATASETS_DIR)
    except ValueError:
        return [], []
    want = canonical_path(record)
    if want is None or path == want:
        return [], []
    if path == legacy_path(record):
        return [], [f"old layout; the record belongs in {rel(want)} (python tools/place.py {rel(path)})"]
    return [f"file must live in {rel(want)} (datasets/<dimensionality>/<first modality>/); "
            f"move it with `python tools/place.py {rel(path)}`"], []


def check_technical(record):
    """Claims in `technical` must match how deep the inspection went."""
    t = record.get("technical")
    if not isinstance(t, dict):
        return []
    errors = []
    if record.get("schema_version") != "1.2":
        errors.append("a record with `technical` must have schema_version '1.2'")
    arrays = [a for a in t.get("arrays") or [] if isinstance(a, dict)]
    if t.get("method") in ("header", "sample") and not arrays:
        errors.append(f"technical.method is {t.get('method')!r} but technical.arrays is empty")
    if t.get("method") != "sample":
        for a in arrays:
            for key in ("value_range", "n_ids_observed"):
                if a.get(key) is not None:
                    errors.append(f"technical.arrays[{a.get('path_pattern')}].{key} needs technical.method 'sample' "
                                  "(observed values come only from tools/sample.py)")
    if t.get("size_source") in CONFIRMED_SIZE_SOURCES:
        if (record.get("data") or {}).get("size_bytes") is None:
            errors.append("technical.size_source is 'file-listing' but data.size_bytes is null")
        if not (t.get("layout") or {}).get("listing_complete"):
            errors.append("technical.size_source is 'file-listing' but layout.listing_complete is false")
    return errors


def check_inspection_log(record, run, baseline=None):
    """With --run-log: the enricher's listing / header / sample claims must be backed by run log entries.
    Claims the record already made, unchanged, in `baseline` (the record as pulled from its PR branch) were
    backed by the run that made them, so a later pass that keeps them (e.g. one that only adds a size) isn't
    asked to inspect the files again."""
    t = record.get("technical")
    if not isinstance(t, dict):
        return []
    bt = ((baseline or {}).get("technical") or {}) if isinstance(baseline, dict) else {}
    bd = ((baseline or {}).get("data") or {}) if isinstance(baseline, dict) else {}
    errors, rid = [], record.get("id")
    fetched = {normalize_url(u) for u in run.get("fetched_urls", [])}
    known = {normalize_url(u) for u in bt.get("inspected_urls") or []}
    for u in t.get("inspected_urls") or []:
        if normalize_url(u) not in fetched and normalize_url(u) not in known:
            errors.append(f"technical.inspected_urls entry not in run log: {u}")
    mine = [e for e in run.get("inspected", []) if e.get("id") == rid]
    kinds = {e.get("kind") for e in mine}
    same_method = bool(bt) and bt.get("method") == t.get("method")
    if t.get("method") == "sample" and "sample" not in kinds and not same_method:
        errors.append("technical.method is 'sample' but the run log has no tools/sample.py entry for this id")
    if t.get("method") == "header" and not kinds & {"header", "sample"} and not same_method:
        errors.append("technical.method is 'header' but the run log has no tools/probe.py entry for this id")
    same_size = (bool(bt) and bt.get("size_source") == t.get("size_source")
                 and bd.get("size_bytes") == (record.get("data") or {}).get("size_bytes"))
    if t.get("size_source") in CONFIRMED_SIZE_SOURCES and not same_size:
        totals = {e.get("total_bytes") for e in mine if e.get("kind") == "listing" and e.get("size_source") == "file-listing"}
        size = (record.get("data") or {}).get("size_bytes")
        if not totals:
            errors.append("technical.size_source is 'file-listing' but no complete, exact tools/listing.py run is logged for this id")
        elif size not in totals:
            errors.append(f"data.size_bytes {size} does not equal the logged file-listing total ({sorted(totals)}); "
                          "a confirmed size must be the exact listed total")
    return errors


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="files/dirs to validate (default: datasets/)")
    ap.add_argument("--run-log", help="require every evidence_url to appear in this run log's fetched URLs")
    ap.add_argument("--baseline", help="the record before this run's edits (tools/publish.py pr-pull saves it); "
                    "its technical claims, unchanged, were backed by an earlier run's log; "
                    "its evidence_urls were logged by an earlier run and are not re-checked")
    ap.add_argument("--min-confidence", type=float, default=None,
                    help="fail records below this provenance.confidence (harvest PRs use 0.5)")
    a = ap.parse_args()

    v = validator()
    targets = list(iter_record_paths(a.paths))
    # drafts count for uniqueness too, so two drafts of one run can't be the same dataset
    all_paths = list(iter_record_paths([DATASETS_DIR] + ([DRAFTS_DIR] if DRAFTS_DIR.exists() else [])))
    rejected = set(load_rejected())
    fetched = run = None
    baseline_urls, base = set(), None
    if a.baseline:
        base = load_yaml(a.baseline) or {}
        baseline_urls = {normalize_url(u) for u in (base.get("provenance") or {}).get("evidence_urls", [])}
    if a.run_log:
        run = json.loads(Path(a.run_log).read_text())
        fetched = {normalize_url(u) for u in run.get("fetched_urls", [])}

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
            loc_errs, loc_warns = check_location(p, rec)
            errs += loc_errs
            if loc_warns:
                warnings.setdefault(p, []).extend(loc_warns)
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
                    if normalize_url(u) not in fetched and normalize_url(u) not in baseline_urls:
                        errs.append(f"evidence url not in run log fetched_urls: {u}")
                errs += check_inspection_log(rec, run, base)
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
    plist = [(p, r) for p, r in records.items() if isinstance(r, dict)]
    for i, (p, rp) in enumerate(plist):
        for q, rq in plist[i + 1:]:
            if p in target_set or q in target_set:
                if why := similarity_reasons(rp, rq):
                    warnings.setdefault(p if p in target_set else q, []).append(
                        f"possible duplicate of {rel(q if p in target_set else p)} ({', '.join(why)})")

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
