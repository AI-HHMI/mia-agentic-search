"""Generate the PR title and body for a single dataset record.

    python tools/pr_text.py <record.yaml> --title      # the short_name
    python tools/pr_text.py <record.yaml> --body [--run-log state/runs/<file>.json]
    python tools/pr_text.py <record.yaml> --body --run-log <harvest log> --enrich-log <enricher log>

Agents must use this verbatim so every dataset PR has the same format.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import human_size, load_yaml, rel  # noqa: E402

DIM_LABELS = {"2D": "2D images", "3D": "3D volumes", "2D+t": "2D time-lapse", "3D+t": "3D time-lapse"}


def _join(items, empty="unknown"):
    return ", ".join(str(i) for i in items) if items else empty


def title(r):
    return r["short_name"]


SIZE_SOURCE = {"file-listing": "confirmed from a complete file listing", "page-stated": "as stated on the dataset page",
               "paper": "as stated in the paper", "estimated": "estimate", "unknown": "source unknown"}


def _cell(v):
    return str(v).replace("|", "\\|").replace("\n", " ") if v not in (None, "", []) else "—"


def technical(t):
    """Markdown for the enricher's `technical` block."""
    arrays = t.get("arrays") or []
    lines = ["", "### Technical inspection", "", f"Deepest check: **{t['method']}**", ""]
    if arrays:
        lines += ["| Role | Files | Axes · shape | dtype | Compression | Values | Labels | Alignment |",
                  "|---|---|---|---|---|---|---|---|"]
        for a in arrays:
            shape = "×".join(str(n) for n in a["shape"]) if a.get("shape") else "?"
            axes = f"{a['axes']} · {shape}" if a.get("axes") else f"{shape} (axes not stated)"
            if a.get("shape_varies"):
                axes += " (varies)"
            vals = [f"{a['value_range'][0]:g}–{a['value_range'][1]:g}" if a.get("value_range") else None,
                    a.get("normalization") if a.get("normalization") not in (None, "unknown") else None]
            labels = []
            if a.get("encoding"):
                labels.append(a["encoding"])
            if a.get("classes"):
                labels.append(", ".join(f"{c['id']}={c['name']}" for c in a["classes"][:8]))
            align = a.get("alignment") or "—"
            if a.get("alignment_notes"):
                align += f": {a['alignment_notes']}"
            lines.append(f"| {a['role']} | `{_cell(a['path_pattern'])}` · {a['format']} | {_cell(axes)} | {a['dtype']} | "
                         f"{_cell(a['compression'])} | {_cell(' · '.join(v for v in vals if v))} | {_cell(' · '.join(labels))} | {_cell(align)} |")
    smp = t.get("sample")
    if smp:
        lines += ["", f"🧪 **Quick-test sample** ({human_size(smp.get('size_bytes'))}): "
                  + (smp.get("description") or ""), *[f"- `{u}`" for u in smp["urls"]]]
    lay = t.get("layout") or {}
    if lay.get("tree"):
        n = f"{lay['n_files']} files" if lay.get("n_files") is not None else "files"
        lines += ["", f"<details><summary>Folder structure ({n}{'' if lay.get('listing_complete') else ', listing incomplete'})</summary>",
                  "", "```", lay["tree"].rstrip(), "```", "", "</details>"]
    if t.get("notes"):
        lines += ["", f"**Inspection notes:** {t['notes'].strip()}"]
    return lines


def body(r, path, run_log=None, enrich_log=None):
    im, da, an, ml, lic, pv = (r[k] for k in ("imaging", "data", "annotations", "ml", "license", "provenance"))
    vox = im["voxel_size_nm"]
    vox_s = f"{vox['x']} × {vox['y']} × {vox['z'] if vox['z'] is not None else '—'} nm (x × y × z)" if vox else "unknown"
    doi = f" · DOI [{r['doi']}](https://doi.org/{r['doi']})" if r.get("doi") else ""
    ann_s = (f"{_join(an['types'])} · {an['coverage']} coverage · {an['source']}"
             + (f" · format: {an['format']}" if an["format"] else "")) if an["present"] else "none (unlabelled)"
    bench_s = f" · benchmark: {ml['benchmark']}" if ml["benchmark"] else ""
    routine = pv["discovered_by"]
    src = (r.get("technical") or {}).get("size_source")
    size_src = f" ({SIZE_SOURCE[src]})" if src and da["size_bytes"] is not None else ""
    log_s = f" · run log `{rel(run_log)}` on branch `state`" if run_log else ""
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
        f"| **Size** | {human_size(da['size_bytes'])}{size_src} · {da['n_items'] or 'unknown number of'} images/volumes |",
        f"| **Sample** | {_join(im['organism'], 'organism unknown')} · {im['sample'] or 'sample unknown'} |",
        f"| **Annotations** | {ann_s} |",
        f"| **ML tasks** | {_join(ml['tasks'])} · splits provided: {'yes' if ml['splits_provided'] else 'no'}{bench_s} |",
        f"| **Access / license** | {da['access']} · {lic['spdx']}{' (' + lic['url'] + ')' if lic['url'] else ''} |",
        f"| **Hosted at** | {r['repository']} {r.get('accession') or ''}{doi} |",
    ]
    if r.get("notes"):
        lines += ["", f"**Notes:** {r['notes'].strip()}"]
    if r.get("technical"):
        lines += technical(r["technical"])
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
    if r.get("technical"):
        elog = f" · run log `{rel(enrich_log)}` on branch `state`" if enrich_log else ""
        lines.append(f"_Inspected by `enricher`{elog}. "
                     "Labels are set automatically from the record._")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("record")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--title", action="store_true")
    g.add_argument("--body", action="store_true")
    ap.add_argument("--run-log", help="the harvest run that found the dataset")
    ap.add_argument("--enrich-log", help="the enricher run that inspected it")
    a = ap.parse_args()
    r = load_yaml(a.record)
    print(title(r) if a.title else body(r, Path(a.record).resolve(), a.run_log, a.enrich_log))


if __name__ == "__main__":
    main()
