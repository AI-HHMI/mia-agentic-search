"""Download a small piece of a dataset, measure it, and delete it again.

    python tools/sample.py --id RECORD_ID --raw <spec> [--label <spec>] [--target <spec>]

A <spec> is a file URL, optionally with a suffix:
    <zip url>::<path in zip>          one member of a remote zip (read by range, not the whole zip)
    <h5 url>::<dataset path>          a central crop of one HDF5 dataset
    <zarr / n5 array url>             a central crop (one chunk-aligned region)
Large TIFFs (> --max-file-mb) are read page-wise by range (first and middle page) instead of whole.

Per array it prints shape, dtype, value range and percentiles, unique-value counts for integers,
and hints (e.g. "values in [0, 1]", "binary mask", "many integer IDs: instance labels?").
With --raw and --label it also compares their shapes (alignment). Hints are heuristics: write
them into the record only as what was observed (e.g. value_range), and say so in notes.

Budget: at most SAMPLE_MAX_BYTES (tools/common.py, 500 MB) per record per run, summed over
calls via the run log. Files go to a temporary directory that is always deleted. Never run code
or unpickle anything that comes with a dataset; this tool only parses array formats.
"""
import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import SAMPLE_MAX_BYTES, polite_get  # noqa: E402
from tools.peek_archive import read_member, remote_size, zip_entries  # noqa: E402
from tools.probe import RangeFile, http_url, sniff  # noqa: E402
from tools.run_log import log_inspection, sampled_bytes  # noqa: E402

MAX_ELEMENTS = 20_000_000   # statistics use a strided subsample above this
CROP_BYTES = 64 * 10**6     # decoded size of a crop from chunked formats


class Budget:
    def __init__(self, record_id):
        self.left = SAMPLE_MAX_BYTES - sampled_bytes(record_id)

    def take(self, n, what):
        if n > self.left:
            sys.exit(f"{what}: {n / 1e6:.1f} MB exceeds the remaining sample budget of {max(self.left, 0) / 1e6:.1f} MB "
                     "for this record; pick a smaller file or use tools/probe.py")
        self.left -= n


def download(url, dest, cap):
    r = polite_get(url, stream=True, allow_redirects=True, timeout=120)
    r.raise_for_status()
    n = 0
    with open(dest, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            n += len(chunk)
            if n > cap:
                raise RuntimeError(f"download exceeded {cap / 1e6:.0f} MB; aborted")
            f.write(chunk)
    return n


def load_file(path, name):
    import numpy as np
    head = Path(path).read_bytes()[:1024]
    kind = sniff(name, head)
    if kind == "tiff":
        import tifffile
        with tifffile.TiffFile(path) as t:
            s = t.series[0]
            return s.asarray(), {"axes": s.axes, "n_series": len(t.series)}
    if kind in ("png", "jpeg"):
        from PIL import Image
        with Image.open(path) as im:
            return np.asarray(im), {"mode": im.mode}
    if kind == "mrc":
        import mrcfile
        with mrcfile.open(path, permissive=True) as m:
            return np.array(m.data), {"voxel_size_A": [float(v) for v in m.voxel_size.tolist()]}
    if kind in ("nifti", "nifti-gz"):
        import nibabel
        img = nibabel.load(path)
        return np.asarray(img.dataobj), {"zooms": [float(z) for z in img.header.get_zooms()]}
    if kind == "hdf5":
        raise ValueError("HDF5: pass <url>::<dataset path> (list datasets with tools/probe.py)")
    raise ValueError(f"unsupported format for {name}")


def central_crop(shape, itemsize, chunks=None, max_bytes=CROP_BYTES):
    """Slices for a crop around the centre, shrinking the leading axes until it fits."""
    sel = [slice(0, n) for n in shape]
    size = [n for n in shape]
    while itemsize * _prod(size) > max_bytes:
        i = max(range(len(size)), key=lambda k: size[k] / (chunks[k] if chunks else 1))
        size[i] = max(1, size[i] // 2)
    for i, (n, s) in enumerate(zip(shape, size)):
        start = (n - s) // 2
        if chunks and chunks[i]:
            start = start // chunks[i] * chunks[i]
        sel[i] = slice(start, min(n, start + s))
    return tuple(sel)


def _prod(xs):
    p = 1
    for x in xs:
        p *= x
    return p


def load_spec(spec, budget, max_file):
    """Returns (array, info, bytes_downloaded)."""
    url, _, inner = spec.partition("::")
    hurl = http_url(url)
    path = urlsplit(hurl).path.lower().rstrip("/")
    tmp = Path(tempfile.mkdtemp(prefix="mia-sample-"))
    try:
        if ".n5" in path:
            raise ValueError("N5 is not supported here; use tools/probe.py for its metadata")
        if ".zarr" in path:
            import zarr
            arr = zarr.open_array(store=zarr.storage.FsspecStore.from_url(hurl, read_only=True), mode="r")
            sel = central_crop(arr.shape, arr.dtype.itemsize, arr.chunks)
            nbytes = arr.dtype.itemsize * _prod(s.stop - s.start for s in sel)
            budget.take(nbytes, spec)
            return arr[sel], {"crop": [[s.start, s.stop] for s in sel], "full_shape": list(arr.shape)}, nbytes
        if path.endswith(".zip") and inner:
            _, entries = zip_entries(hurl)
            entry = next((e for e in entries if e["name"] == inner), None)
            if entry is None:
                raise ValueError(f"{inner} not in zip")
            budget.take(entry["csize"], spec)
            data, complete = read_member(hurl, entry)
            dest = tmp / Path(inner).name
            dest.write_bytes(data)
            arr, info = load_file(dest, inner)
            return arr, info, entry["csize"]
        if inner:  # HDF5 dataset
            import h5py
            f = RangeFile(hurl)
            with h5py.File(f, "r") as h5:
                ds = h5[inner]
                sel = central_crop(ds.shape, ds.dtype.itemsize, ds.chunks)
                arr = ds[sel]
                attrs = {k: (v.tolist() if hasattr(v, "tolist") else str(v)) for k, v in list(ds.attrs.items())[:20]}
            budget.take(f.bytes_read, spec)
            return arr, {"crop": [[s.start, s.stop] for s in sel], "full_shape": list(ds.shape), "attrs": attrs}, f.bytes_read
        size = remote_size(hurl)
        if size and size > max_file and path.endswith((".tif", ".tiff")):
            import numpy as np
            import tifffile
            f = RangeFile(hurl, size=size)
            with tifffile.TiffFile(f) as t:
                n = len(t.pages)
                idx = sorted({0, n // 2})
                arr = np.stack([t.pages[i].asarray() for i in idx])
            budget.take(f.bytes_read, spec)
            return arr, {"pages_read": idx, "n_pages": n, "note": "page-wise read of a large TIFF"}, f.bytes_read
        if size and size > max_file:
            raise ValueError(f"file is {size / 1e6:.0f} MB (> --max-file-mb); probe its header instead")
        reserve = size or max_file  # unknown size: reserve the maximum, then refund
        budget.take(reserve, spec)
        dest = tmp / (Path(path).name or "file")
        n = download(hurl, dest, reserve)
        budget.left += reserve - n
        arr, info = load_file(dest, path)
        return arr, info, n
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def stats(arr, role):
    import numpy as np
    a = np.asarray(arr)
    flat = a.reshape(-1)
    if flat.size > MAX_ELEMENTS:
        flat = flat[:: flat.size // MAX_ELEMENTS + 1]
    out = {"shape": list(a.shape), "dtype": str(a.dtype), "hints": []}
    if flat.size == 0:
        return out
    if a.ndim >= 3 and 2 <= a.shape[-1] <= 4:       # RGB(A) samples: grey stored as colour?
        out["samples_identical"] = bool(all(np.array_equal(a[..., 0], a[..., k]) for k in range(1, a.shape[-1])))
        if out["samples_identical"]:
            out["hints"].append(f"the {a.shape[-1]} samples on the last axis are identical: one channel stored as RGB")
    if a.dtype.kind in "c":
        flat = np.abs(flat)
    finite = flat[np.isfinite(flat)] if a.dtype.kind == "f" else flat
    if finite.size < flat.size:
        out["non_finite_fraction"] = round(1 - finite.size / flat.size, 6)
    q = np.percentile(finite, [0.1, 1, 50, 99, 99.9]).tolist() if finite.size else []
    lo, hi = (finite.min().item(), finite.max().item()) if finite.size else (None, None)
    out.update({"min": lo, "max": hi, "mean": float(finite.mean()) if finite.size else None,
                "std": float(finite.std()) if finite.size else None,
                "percentiles": dict(zip(["p0.1", "p1", "p50", "p99", "p99.9"], [float(f"{v:.6g}") for v in q])),
                "zero_fraction": round(float((finite == 0).mean()), 6) if finite.size else None})
    if a.dtype.kind in "biu":
        u, c = np.unique(finite, return_counts=True)
        out["n_unique"] = int(u.size)
        if u.size <= 32:
            out["values"] = {int(k): int(v) for k, v in zip(u, c)}
        if role == "label":
            vals = set(u.tolist())
            if vals <= {0, 1} or vals <= {0, 255} or a.dtype == bool:
                out["hints"].append("binary mask")
            elif u.size > 32:
                out["hints"].append(f"{u.size} distinct integer IDs in the sample: instance labels?")
            else:
                out["hints"].append(f"{u.size} distinct values: semantic classes or few instances; check the docs")
        else:
            info = np.iinfo(a.dtype) if a.dtype.kind in "iu" else None
            if info and lo == info.min and hi == info.max and q and (q[0] == info.min or q[-1] == info.max):
                out["hints"].append("full dtype range used and saturated at an end: possibly contrast-stretched")
            if a.dtype == np.uint16 and hi is not None and hi < 4096:
                out["hints"].append("uint16 with max < 4096: 12-bit camera/detector range")
    elif a.dtype.kind == "f" and lo is not None:
        if lo >= 0 and hi <= 1:
            out["hints"].append("values in [0, 1]: rescaled / normalized")
        elif abs(out["mean"]) < 0.1 and 0.8 < out["std"] < 1.2:
            out["hints"].append("mean ≈ 0, std ≈ 1: standardized")
        if role == "label":
            if lo >= 0 and hi <= 1:
                out["hints"].append("float values in [0, 1]: probability map or soft mask?")
            elif np.all(np.mod(finite[:100000], 1) == 0):
                out["hints"].append("float array holding integer values: label IDs stored as float")
    return out


def compare(raw, label):
    rs, ls = list(raw["shape"]), list(label["shape"])
    if rs == ls:
        return {"same_shape": True, "note": "raw and label have identical shapes (same grid, unless the docs say otherwise)"}
    for big, small, who in ((ls, rs, "label"), (rs, ls, "raw")):
        if len(big) == len(small) + 1 and big[:-1] == small and big[-1] <= 4:
            return {"same_shape": False, "same_spatial_grid": True, "raw_shape": rs, "label_shape": ls,
                    "note": f"same spatial grid; {who} has an extra trailing axis of {big[-1]} (RGB/RGBA channels?)"}
    common = min(len(rs), len(ls))
    ratio = [round(r / l, 4) if l else None for r, l in zip(rs[-common:], ls[-common:])]
    return {"same_shape": False, "raw_shape": rs, "label_shape": ls, "trailing_axis_ratio_raw_over_label": ratio,
            "note": "shapes differ: channel axis, crop, or different resolution; check the docs and paper"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", required=True, help="record id (the per-record budget is tracked in the run log)")
    ap.add_argument("--raw")
    ap.add_argument("--label")
    ap.add_argument("--target", help="restoration target (e.g. high-SNR / high-resolution image)")
    ap.add_argument("--max-file-mb", type=float, default=200)
    a = ap.parse_args()
    if not (a.raw or a.label or a.target):
        ap.error("give at least one of --raw, --label, --target")
    budget = Budget(a.id)
    results = {}
    for role in ("raw", "label", "target"):
        spec = getattr(a, role)
        if not spec:
            continue
        try:
            arr, info, nbytes = load_spec(spec, budget, int(a.max_file_mb * 1e6))
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            results[role] = {"spec": spec, "error": f"{type(e).__name__}: {e}"}
            continue
        results[role] = {"spec": spec, **stats(arr, role), "file_info": info, "bytes_downloaded": nbytes}
        url, _, inner = spec.partition("::")
        log_inspection("sample", url, a.id, member=inner or None, role=role, bytes=nbytes,
                       shape=results[role]["shape"], dtype=results[role]["dtype"])
        del arr
    if "raw" in results and "label" in results and "error" not in results["raw"] and "error" not in results["label"]:
        results["alignment"] = compare(results["raw"], results["label"])
    if "raw" in results and "target" in results and "error" not in results["raw"] and "error" not in results["target"]:
        results["raw_vs_target"] = compare(results["raw"], results["target"])
    results["sample_budget_left_mb"] = round(max(budget.left, 0) / 1e6, 1)
    print(json.dumps(results, indent=1, default=str))
    sys.exit(1 if any(isinstance(v, dict) and "error" in v for v in results.values()) else 0)


if __name__ == "__main__":
    main()
