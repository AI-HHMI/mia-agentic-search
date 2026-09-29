"""Generate the PR title and body for a single dataset record.

    python tools/pr_text.py <record.yaml> --title      # "Add dataset: <short_name>"
    python tools/pr_text.py <record.yaml> --body [--run-log state/runs/<file>.json]

Agents must use this verbatim so every dataset PR has the same format.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import load_yaml, rel  # noqa: E402

DIM_LABELS = {"2D": "2D images", "3D": "3D volumes", "2D+t": "2D time-lapse", "3D+t": "3D time-lapse"}


def human_size(n):
    if n is None:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def _join(items, empty="unknown"):
    return ", ".join(str(i) for i in items) if items else empty


def title(r):
    return f"Add dataset: {r['short_name']}"


def body(r, path, run_log=None):
    im, da, an, ml, lic, pv = (r[k] for k in ("imaging", "data", "annotations", "ml", "license", "provenance"))
    vox = im["voxel_size_nm"]
    vox_s = f"{vox['x']} × {vox['y']} × {vox['z'] if vox['z'] is not None else '—'} nm (x × y × z)" if vox else "unknown"
    doi = f" · DOI [{r['doi']}](https://doi.org/{r['doi']})" if r.get("doi") else ""
    ann_s = (f"{_join(an['types'])} · {an['coverage']} coverage · {an['source']}"
             + (f" · format: {an['format']}" if an["format"] else "")) if an["present"] else "none (unlabelled)"
    bench_s = f" · benchmark: {ml['benchmark']}" if ml["benchmark"] else ""
    routine = pv["discovered_by"]
    log_s = f" · run log `{rel(run_log)}` on branch `claude/state/{routine}`" if run_log else ""
    pubs = []
    for p in r["publications"]:
        link = f"https://doi.org/{p['doi']}" if p.get("doi") else p.get("url")
        pubs.append(f"[{p['title']}]({link})" if link else p["title"])

    lines = [
        f"## {r['title']}",
        "",
        f"🔗 **Dataset page:** {r['landing_url']}",
        f"⬇️ **Download:** {da['download_url'] or 'not found; see dataset page'}",
        f"📄 **Paper:** {'; '.join(pubs) if pubs else 'none found'}",
        "",
        r["description"].strip(),
        "",
        "| | |",
        "|---|---|",
        f"| **Modality** | {_join(im['modality'])} |",
        f"| **Dimensionality** | {DIM_LABELS.get(im['dimensionality'], im['dimensionality'])} · channels: {im['channels'] or 'unknown'} |",
        f"| **Voxel / pixel size** | {vox_s} |",
        f"| **Data format** | {_join(da['formats'])} |",
        f"| **Size** | {human_size(da['size_bytes'])} · {da['n_items'] or 'unknown number of'} images/volumes |",
        f"| **Sample** | {_join(im['organism'], 'organism unknown')} · {im['sample'] or 'sample unknown'} |",
        f"| **Annotations** | {ann_s} |",
        f"| **ML tasks** | {_join(ml['tasks'])} · splits provided: {'yes' if ml['splits_provided'] else 'no'}{bench_s} |",
        f"| **Access / license** | {da['access']} · {lic['spdx']}{' (' + lic['url'] + ')' if lic['url'] else ''} |",
        f"| **Hosted at** | {r['repository']} {r.get('accession') or ''}{doi} |",
    ]
    if r.get("notes"):
        lines += ["", f"**Notes:** {r['notes'].strip()}"]
    lines += [
        "",
        "<details><summary>Sources the agent read</summary>",
        "",
        *[f"- {u}" for u in pv["evidence_urls"]],
        "",
        "</details>",
        "",
        "---",
        f"**Review:** merge to accept · close to reject (it won't be proposed again) · edit `{rel(path)}` in this PR to fix fields first.",
        f"_Found by `{routine}` on {pv['discovered_at'][:10]}{log_s}._",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("record")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--title", action="store_true")
    g.add_argument("--body", action="store_true")
    ap.add_argument("--run-log")
    a = ap.parse_args()
    r = load_yaml(a.record)
    print(title(r) if a.title else body(r, Path(a.record).resolve(), a.run_log))


if __name__ == "__main__":
    main()
