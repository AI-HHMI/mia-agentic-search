"""Find a dataset's paper and read its full text, even when the publisher page blocks us.

    python tools/paper.py --doi 10.1083/jcb.202402169 [--out /tmp/paper.txt]
    python tools/paper.py --pmid 40909564
    python tools/paper.py --title "CryoVesNet synaptic vesicle segmentation"

Uses Europe PMC (open-access full text for journals *and* preprints), Crossref and bioRxiv.
Prints metadata, the URLs used (log each with `run_log.py fetched`), and "key sentences"
mentioning voxel/pixel size, formats, annotations, splits, licenses and data availability.
The complete text goes to --out (default /tmp/paper-<id>.txt); read it for anything still unknown.
"""
import argparse
import difflib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import polite_get  # noqa: E402

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
# Case-sensitive units, so concentrations like "5 µM" don't match.
UNITS = re.compile(r"\d+(\.\d+)?\s*(nm|µm|μm|um|microns?)\b")
STRONG = re.compile(r"pixel size|voxel|isotropic|resolution of|file format|\.(tiff?|h5|hdf5|n5|zarr|mrc)\b|ome-zarr|"
                    r"annotat|ground[- ]truth|data availability|licen[cs]e|deposited", re.I)
KEY = re.compile(
    r"(pixel size|voxel|resolution|isotropic|"
    r"\.(tiff?|h5|hdf5|n5|zarr|mrc|czi|nd2|png)\b|ome-|hdf5|zarr|file format|"
    r"annotat|ground[- ]truth|label|mask|proofread|manual(ly)? (traced|segment)|"
    r"training|validation|test set|split|benchmark|"
    r"licen[cs]e|cc[- ]by|cc0|data availability|deposited|available (at|from)|accession|empiar|zenodo|biostudies|idr\d)",
    re.I)
used = []


def get(url, **params):
    r = polite_get(url, params=params or None, timeout=40)
    r.raise_for_status()
    used.append(r.url)
    return r


def epmc_search(query):
    res = get(f"{EPMC}/search", query=query, format="json", resultType="core", pageSize=3).json()
    return res.get("resultList", {}).get("result", [])


def jats_text(xml):
    """Flatten JATS XML into '## Section' headings + paragraphs, including tables and supplements."""
    root = ET.fromstring(xml)
    out = []

    def text(el):
        return re.sub(r"\s+", " ", "".join(el.itertext())).strip()

    abstract = root.find(".//abstract")
    if abstract is not None:
        out += ["## Abstract", text(abstract)]
    for sec in root.iter("sec"):
        t = sec.find("title")
        paras = [text(p) for p in sec.findall("p")]
        if paras:
            out.append(f"## {text(t) if t is not None else 'Section'}")
            out += paras
    for tag in ("table-wrap", "fig"):
        for el in root.iter(tag):
            cap = el.find(".//caption")
            if cap is not None:
                out.append(f"[{tag}] {text(cap)}")
            if tag == "table-wrap":
                for row in el.iter("tr"):
                    out.append(" | ".join(text(c) for c in row))
    for el in root.iter("supplementary-material"):
        out.append(f"[supplement] {text(el)}")
    for el in root.iter("ext-link"):
        href = el.get("{http://www.w3.org/1999/xlink}href")
        if href:
            out.append(f"[link] {href}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--doi")
    g.add_argument("--pmid")
    g.add_argument("--title")
    ap.add_argument("--out")
    ap.add_argument("--max-key", type=int, default=60, help="max key sentences to print")
    a = ap.parse_args()

    meta, full = {}, None
    try:
        if a.doi:
            hits = epmc_search(f'DOI:"{a.doi}"')
        elif a.pmid:
            hits = epmc_search(f"EXT_ID:{a.pmid} AND SRC:MED")
        else:
            # Title search is fuzzy; only accept a hit whose title really matches.
            norm = lambda t: re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()  # noqa: E731
            cands = epmc_search(f'TITLE:"{a.title}"') or epmc_search(a.title)
            scored = sorted(((difflib.SequenceMatcher(None, norm(a.title), norm(h.get("title"))).ratio(), h) for h in cands),
                            key=lambda x: -x[0])
            if scored and scored[0][0] < 0.8:
                print(f"NOT FOUND: closest Europe PMC title is {scored[0][1].get('title')!r} "
                      f"(similarity {scored[0][0]:.2f}). Look for the paper's DOI on the dataset page, the "
                      "publisher site, arXiv or Google Scholar via WebSearch instead.")
                sys.exit(1)
            hits = [h for _, h in scored]
        if hits:
            h = hits[0]
            meta = {k: h.get(k) for k in ("title", "doi", "pmid", "pmcid", "source", "id", "isOpenAccess",
                                          "journalTitle", "pubYear", "authorString")}
            meta["abstract"] = re.sub(r"<[^>]+>", "", h.get("abstractText") or "")
            ft_id = h.get("pmcid") or (h["id"] if h.get("source") == "PPR" else None)
            if ft_id:
                try:
                    full = jats_text(get(f"{EPMC}/{ft_id}/fullTextXML").text)
                except (requests.RequestException, ET.ParseError):
                    full = None
        doi = meta.get("doi") or a.doi
        if doi and not meta.get("title"):
            cr = get(f"https://api.crossref.org/works/{doi}").json()["message"]
            meta.update(title=(cr.get("title") or [None])[0], doi=doi, journalTitle=(cr.get("container-title") or [None])[0])
        if doi and full is None and doi.startswith("10.1101/"):
            coll = get(f"https://api.biorxiv.org/details/biorxiv/{doi}").json().get("collection") or []
            if coll and coll[-1].get("jatsxml"):
                full = jats_text(get(coll[-1]["jatsxml"]).text)
    except requests.RequestException as e:
        print(json.dumps({"error": str(e), "urls_used": used}), file=sys.stderr)
        sys.exit(2)

    if not meta and full is None:
        print("NOT FOUND: no matching paper in Europe PMC / Crossref. Try --title with other keywords.")
        sys.exit(1)

    print(json.dumps({k: v for k, v in meta.items() if k != "abstract"}, ensure_ascii=False))
    print("\nURLS USED (log each with run_log.py fetched):")
    for u in dict.fromkeys(used):
        print(f"  {u}")
    if full is None:
        print("\nFULL TEXT: not available (not open access). Read the abstract below, then try the publisher"
              " page, the preprint, or the supplementary material with WebFetch.")
        print(f"\nABSTRACT: {meta.get('abstract', '')}")
        return
    out = Path(a.out or f"/tmp/paper-{(meta.get('pmcid') or meta.get('id') or 'x')}.txt")
    out.write_text(f"{meta.get('title')}\n\n{full}\n")
    print(f"\nFULL TEXT: {len(full):,} chars written to {out}. Read the Methods and Data availability sections there.")
    sentences = [s.strip() for s in re.split(r"(?<=[.;])\s+|\n", full)
                 if (KEY.search(s) or UNITS.search(s)) and 20 < len(s) < 600]
    sentences.sort(key=lambda s: not STRONG.search(s))  # most informative first, otherwise in reading order
    print(f"\nKEY SENTENCES ({min(len(sentences), a.max_key)} of {len(sentences)}):")
    for s in sentences[: a.max_key]:
        print(f"  - {s}")


if __name__ == "__main__":
    main()
