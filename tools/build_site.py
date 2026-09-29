"""Build the static monitoring dashboard (GitHub Pages) from records + run logs.

    python tools/build_site.py [--out site]

Writes <out>/index.html (self-contained) and <out>/catalog.json. Run logs come from main
plus all fetched claude/* branches, so unmerged harvest runs are visible too.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.collect_runs import collect  # noqa: E402
from tools.common import ROOT, iter_record_paths, load_yaml  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent / "site_template.html"
RUN_FIELDS = ("routine", "started_at", "finished_at", "status", "counts", "pr_urls", "pr_url", "duration_s", "dry_run")


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
    payload = json.dumps({"records": records, "runs": runs[:500]}, ensure_ascii=False).replace("</", "<\\/")
    (out / "index.html").write_text(TEMPLATE.read_text().replace("/*__DATA__*/null", payload))
    print(f"wrote {out.relative_to(ROOT)}/index.html ({len(records)} records, {len(runs)} runs)")


if __name__ == "__main__":
    main()
