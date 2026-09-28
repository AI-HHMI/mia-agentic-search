"""Check landing/download URLs of records and update verification.url_ok.

    python tools/check_links.py [paths...] [--write] [--oldest N]

--oldest N only checks the N records with the oldest verification.last_checked.
Prints JSON {"checked": n, "broken": [{"file", "url", "error"}]}.
"""
import argparse
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import ROOT, dump_yaml, iter_record_paths, load_yaml, utcnow  # noqa: E402

HEADERS = {"User-Agent": "mia-agentic-search/1.0 (link check)"}


def url_ok(url):
    try:
        r = requests.head(url, headers=HEADERS, timeout=30, allow_redirects=True)
        if r.status_code in (403, 405, 501) or r.status_code >= 400:
            r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True, stream=True)
            r.close()
        return r.status_code < 400, f"HTTP {r.status_code}"
    except requests.RequestException as e:
        return False, type(e).__name__


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--oldest", type=int)
    a = ap.parse_args()

    recs = [(p, load_yaml(p)) for p in iter_record_paths(a.paths)]
    if a.oldest:
        recs.sort(key=lambda pr: pr[1]["verification"]["last_checked"])
        recs = recs[: a.oldest]

    broken = []
    for p, rec in recs:
        ok = True
        for url in filter(None, [rec.get("landing_url"), rec["data"].get("download_url")]):
            if url.startswith(("ftp://", "s3://")):
                continue
            good, msg = url_ok(url)
            if not good:
                ok = False
                broken.append({"file": str(p.relative_to(ROOT)), "url": url, "error": msg})
        if a.write:
            rec["verification"]["url_ok"] = ok
            rec["verification"]["last_checked"] = utcnow()
            dump_yaml(rec, p)
    print(json.dumps({"checked": len(recs), "broken": broken}, indent=2))


if __name__ == "__main__":
    main()
