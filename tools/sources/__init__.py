"""Thin clients for structured dataset repositories.

Each client returns *candidates*: loosely normalized dicts that give the agent a
starting point. Candidates are NOT records. The agent must still open the
landing page, check the "usable" criteria, and fill in a schema-valid record.

    python -m tools.sources <source> "<query>" [--limit N]
"""
import argparse
import html
import json
import re
import sys

import requests

from tools.common import polite_get


def _get(url, **params):
    r = polite_get(url, params=params or None, timeout=30)
    r.raise_for_status()
    return r.json()


def _strip_html(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _candidate(**kw):
    base = dict(repository=None, accession=None, doi=None, title=None, description=None,
                landing_url=None, authors=[], date_published=None, size=None, license=None,
                hints={})
    base.update(kw)
    return base


def zenodo(query, limit=10):
    # bestmatch: "mostrecent" floods results with unrelated new uploads for multi-word queries
    data = _get("https://zenodo.org/api/records", q=query, type="dataset",
                size=limit, sort="bestmatch")
    out = []
    for h in data["hits"]["hits"]:
        md = h.get("metadata", {})
        out.append(_candidate(
            repository="Zenodo", accession=str(h["id"]), doi=h.get("doi"),
            title=md.get("title"), description=_strip_html(md.get("description"))[:1500],
            landing_url=h.get("links", {}).get("self_html") or f"https://zenodo.org/records/{h['id']}",
            authors=[c.get("name") for c in md.get("creators", [])],
            date_published=md.get("publication_date"),
            size=sum(f.get("size", 0) for f in h.get("files", [])) or None,
            license=(md.get("license") or {}).get("id"),
            hints={"files": [f.get("key") for f in h.get("files", [])][:20],
                   "keywords": md.get("keywords", [])}))
    return out


def empiar(query, limit=10):
    hits = _get("https://www.ebi.ac.uk/ebisearch/ws/rest/empiar",
                query=query, format="json", size=limit)["entries"]
    out = []
    for h in hits:
        acc = h["id"]
        e = _get(f"https://www.ebi.ac.uk/empiar/api/entry/{acc}/")[acc]
        imagesets = e.get("imagesets", [])
        out.append(_candidate(
            repository="EMPIAR", accession=acc, doi=e.get("entry_doi"), title=e.get("title"),
            landing_url=f"https://www.ebi.ac.uk/empiar/{acc}/",
            authors=[a["author"]["name"] for a in e.get("authors", [])],
            date_published=e.get("release_date"), size=e.get("dataset_size"),
            license="CC0-1.0",  # EMPIAR releases entries under CC0; verify on landing page
            hints={"experiment_type": e.get("experiment_type"),
                   "imagesets": [{k: s.get(k) for k in ("name", "category", "data_format",
                                                        "num_images_or_tilt_series",
                                                        "pixel_width")} for s in imagesets][:10],
                   "has_segmentations": any(s.get("segmentations") for s in imagesets),
                   "citation_dois": [c.get("doi") for c in e.get("citation", []) if c.get("doi")]}))
    return out


def bioimage_archive(query, limit=10):
    data = _get("https://www.ebi.ac.uk/biostudies/api/v1/BioImages/search",
                query=query, pageSize=limit)
    return [_candidate(
        repository="BioImageArchive", accession=h["accession"], title=h.get("title"),
        description=(h.get("content") or "")[:1500],
        landing_url=f"https://www.ebi.ac.uk/biostudies/BioImages/studies/{h['accession']}",
        date_published=h.get("release_date"), hints={"n_files": h.get("files")})
        for h in data.get("hits", [])]


def idr(query, limit=10):
    """IDR has no full-text API; list projects+screens and filter names/descriptions."""
    terms = [t.lower() for t in query.split()]
    out = []
    for kind in ("projects", "screens"):
        data = _get(f"https://idr.openmicroscopy.org/api/v0/m/{kind}/", limit=1000)["data"]
        for p in data:
            text = f"{p.get('Name', '')} {p.get('Description', '')}".lower()
            if all(t in text for t in terms):
                name = p.get("Name", "")
                out.append(_candidate(
                    repository="IDR", accession=name.split("-")[0] or None, title=name,
                    description=(p.get("Description") or "")[:1500],
                    landing_url=f"https://idr.openmicroscopy.org/search/?query=Name:{name.split('/')[0]}",
                    license="CC-BY-4.0",  # IDR default; verify per study
                    hints={"kind": kind, "omero_id": p.get("@id")}))
    return out[:limit]


SOURCES = {"zenodo": zenodo, "empiar": empiar, "bioimage_archive": bioimage_archive, "idr": idr}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", choices=sorted(SOURCES))
    ap.add_argument("query")
    ap.add_argument("--limit", type=int, default=10)
    a = ap.parse_args()
    try:
        results = SOURCES[a.source](a.query, a.limit)
    except requests.RequestException as e:
        print(json.dumps({"error": str(e), "source": a.source}), file=sys.stderr)
        sys.exit(2)
    for c in results:
        print(json.dumps(c, ensure_ascii=False))


if __name__ == "__main__":
    main()
