"""Estimate a dataset's size from its arrays: files per array x voxels per file x bytes per voxel.

    python tools/listing.py <url> [<url> ...] --expand-zips --json > /tmp/files.jsonl   # every file, one per line
    python tools/estimate_size.py <record.yaml> --files /tmp/files.jsonl
    python tools/estimate_size.py <record.yaml> --count 0=4 --count 1=4               # counts from the docs

For when the size can't be listed exactly (no file sizes, a listing that doesn't cover everything) and no
page or paper states it. Per raw / label / target array in technical.arrays:

    bytes = files x prod(shape) x bytes per voxel (dtype)

- files: --count INDEX=N (the array's index in technical.arrays, from the docs or a listing you counted),
  else the --files listing entries matching the array's path_pattern. Chunked stores (zarr, ome-zarr,
  n5, precomputed) list chunks, not volumes, so they need --count; a raw store counts 1 when data.n_items
  is 1 (labels need --count: they are often several crops). Their shape is level s0, so pyramid levels
  aren't counted (a 2x pyramid adds about a seventh).
- shape: the recorded shape of one file (the sample's, when shape_varies is true: then the total is
  only as good as that file is typical, and the output says so). Tables (csv / json) are left out.
Prints one JSON object: per-array breakdown, total_bytes, and a technical.notes line. It changes nothing;
data.size_bytes takes total_bytes with technical.size_source `estimated`, and only when no listing,
page or paper gives the size. The result is the uncompressed size; compressed files download smaller.
"""
import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import JUNK_PATH, load_yaml  # noqa: E402
from tools.convertibility import _matches  # noqa: E402

BYTES = {"bool": 1, "uint8": 1, "int8": 1, "uint16": 2, "int16": 2, "float16": 2, "uint32": 4, "int32": 4,
         "float32": 4, "uint64": 8, "int64": 8, "float64": 8, "complex64": 8}
CHUNKED = {"zarr", "ome-zarr", "n5", "precomputed"}
TABLES = {"csv", "json"}
BETTER_SOURCES = {"file-listing", "page-stated", "paper"}


def human(n):
    for unit, size in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"


def estimate(r, files=None, counts=None):
    """(result dict). result['total_bytes'] is None when any array can't be estimated (reasons in 'missing')."""
    counts = counts or {}
    arrays = (r.get("technical") or {}).get("arrays") or []
    n_items = (r.get("data") or {}).get("n_items")
    paths = [f for f in (files or []) if not JUNK_PATH.search(f)]
    rows, missing, notes = [], [], []
    for i, a in enumerate(arrays):
        if not isinstance(a, dict) or a.get("role") not in ("raw", "label", "target"):
            continue
        fmt, pat = a.get("format"), a.get("path_pattern") or "?"
        if fmt in TABLES:
            notes.append(f"array {i} ({fmt} table) left out")
            continue
        shape, nbytes = a.get("shape"), BYTES.get(a.get("dtype"))
        if not shape or not all(isinstance(s, int) and s > 0 for s in shape):
            missing.append(f"array {i} ({a.get('role')}): no shape")
            continue
        if nbytes is None:
            missing.append(f"array {i} ({a.get('role')}): dtype {a.get('dtype')}")
            continue
        if i in counts:
            n, how = counts[i], "--count"
        elif fmt in CHUNKED:
            if n_items == 1 and a.get("role") == "raw":
                n, how = 1, "one volume (data.n_items 1)"
            else:
                missing.append(f"array {i} ({a.get('role')}, {fmt}): chunked store, pass --count {i}=<volumes>")
                continue
        elif paths:
            n, how = sum(1 for p in paths if _matches(p, pat)), "files in the listing matching path_pattern"
            if n == 0:
                missing.append(f"array {i} ({a.get('role')}): no listed file matches {pat}")
                continue
        else:
            missing.append(f"array {i} ({a.get('role')}): no --files listing or --count")
            continue
        total = n * math.prod(shape) * nbytes
        rows.append({"index": i, "role": a.get("role"), "files": n, "count_from": how, "shape": shape,
                     "dtype": a.get("dtype"), "bytes": total, "shape_varies": bool(a.get("shape_varies"))})
        if a.get("shape_varies"):
            notes.append(f"array {i}: shape varies, the sample's shape stands for every file")
        if fmt in CHUNKED:
            notes.append(f"array {i}: level s0 only; pyramid levels not counted")
    total = sum(x["bytes"] for x in rows) if rows and not missing else None
    line = None
    if total is not None:
        parts = " + ".join(f"{x['role']} {x['files']} x {'x'.join(map(str, x['shape']))} {x['dtype']}"
                           for x in rows)
        line = (f"size estimated (uncompressed): {parts} = {total:,} B ({human(total)})"
                + ("; shapes vary, so approximate" if any(x["shape_varies"] for x in rows) else "") + ".")
    return {"id": r.get("id"), "total_bytes": total, "human": human(total) if total is not None else None,
            "arrays": rows, "missing": missing, "notes": notes, "technical_notes_line": line}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("record")
    ap.add_argument("--files", help="tools/listing.py --json output (one JSON object per file)")
    ap.add_argument("--count", action="append", default=[], metavar="INDEX=N",
                    help="files (volumes) of technical.arrays[INDEX], from the docs or a listing you counted")
    a = ap.parse_args()
    r = load_yaml(a.record)
    files, summary = None, {}
    if a.files:
        lines = [json.loads(ln) for ln in Path(a.files).read_text().splitlines() if ln.startswith("{")]
        files = [x["path"] for x in lines if "path" in x]
        summary = next((x for x in lines if "size_source" in x), {})
    counts = {}
    for c in a.count:
        i, _, n = c.partition("=")
        counts[int(i)] = int(n)
    out = estimate(r, files, counts)
    if summary.get("size_source") == "file-listing":
        out["hint"] = (f"this listing has exact sizes for all {summary.get('n_files')} files "
                       f"({summary.get('total_bytes'):,} B): if it covers the whole dataset, that is a confirmed size "
                       f"(size_source file-listing, data.size_bytes = total_bytes), better than this estimate")
    src = (r.get("technical") or {}).get("size_source")
    if (r.get("data") or {}).get("size_bytes") is not None and src in BETTER_SOURCES:
        out["warning"] = f"the record already has a size from {src}; keep it, don't replace it with this estimate"
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
