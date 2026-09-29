"""Generate the PR title and body for a single dataset record.

    python tools/pr_text.py <record.yaml> --title      # one line
    python tools/pr_text.py <record.yaml> --body [--run-log state/runs/<file>.json]

Agents must use this verbatim so every dataset PR has the same informative format.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import CONFIDENCE_CAPS, confidence_band, load_yaml, rel  # noqa: E402

TITLE_MAX = 150


def human_size(n):
    if n is None:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def _join(items, empty="—"):
    return ", ".join(str(i) for i in items) if items else empty


def title(r):
    ann = _join(r["annotations"]["types"], "no labels") if r["annotations"]["present"] else "no labels"
    where = " ".join(x for x in [r["repository"], r.get("accession")] if x)
    suffix = f" ({'/'.join(r['imaging']['modality'])} · {ann} · {where})"
    head = "Add dataset: "
    room = TITLE_MAX - len(head) - len(suffix)
    t = r["title"] if len(r["title"]) <= room else r["title"][: max(room - 1, 20)].rstrip() + "…"
    return head + t + suffix


def _check(ok, text):
    return f"- [{'x' if ok else ' '}] {text}"


def body(r, path, run_log=None):
    im, da, an, ml, lic, pv, ve = (r[k] for k in ("imaging", "data", "annotations", "ml", "license", "provenance", "verification"))
    vox = im["voxel_size_nm"]
    vox_s = f"{vox['x']} × {vox['y']} × {vox['z'] if vox['z'] is not None else '—'} nm" if vox else "unknown"
    conf = pv["confidence"]
    band = confidence_band(conf)
    caps = [f"{why} (max {cap})" for flag, cap, why in CONFIDENCE_CAPS if ve.get(flag) is False]
    doi = f"[{r['doi']}](https://doi.org/{r['doi']})" if r.get("doi") else "—"
    ann_s = f"{_join(an['types'])} · coverage {an['coverage']} · source {an['source']}" if an["present"] else "none (unlabelled)"
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
        f"📄 **Paper(s):** {'; '.join(pubs) if pubs else '—'}",
        "",
        r["description"].strip(),
        "",
        "| | |",
        "|---|---|",
        f"| **Hosted at** | {r['repository']} {r.get('accession') or ''} · DOI {doi} |",
        f"| **Modality** | {_join(im['modality'])} · {im['dimensionality']} · voxel {vox_s} · channels {im['channels'] or '?'} |",
        f"| **Sample** | {_join(im['organism'], 'organism unknown')} · {im['sample'] or '—'} |",
        f"| **Annotations** | {ann_s} |",
        f"| **ML tasks** | {_join(ml['tasks'])} · splits provided: {'yes' if ml['splits_provided'] else 'no'}{bench_s} |",
        f"| **Data** | {_join(da['formats'])} · {human_size(da['size_bytes'])} · {da['n_items'] or '?'} images/volumes · access: {da['access']} |",
        f"| **License** | {lic['spdx']}{' — ' + lic['url'] if lic['url'] else ''} |",
        "",
        f"### Confidence: {band} ({conf:.2f})",
        "_How likely it is that, after checking the sources below, you'll find every field in this record "
        "correct **and** the dataset really usable for training as described. "
        "High ≥ 0.85 · Medium 0.65–0.85 · Low 0.5–0.65._",
        "",
        f"**Why:** {pv['confidence_rationale'].strip()}",
    ]
    if caps:
        lines += ["", f"**Limited by:** {'; '.join(caps)}."]
    lines += [
        "",
        "**What the agent verified**",
        _check(ve["url_ok"], "Dataset page reachable"),
        _check(ve["license_found"], f"License / terms found ({lic['spdx']})"),
        _check(ve["annotations_verified"], "Annotation files seen in a file listing" if an["present"] else "Annotations: none claimed"),
        "",
        "<details><summary>Sources the agent read</summary>",
        "",
        *[f"- {u}" for u in pv["evidence_urls"]],
        "",
        "</details>",
    ]
    if r.get("notes"):
        lines += ["", f"**Notes:** {r['notes'].strip()}"]
    lines += [
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
