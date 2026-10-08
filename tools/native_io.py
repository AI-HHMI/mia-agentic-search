"""Readers, the OME-Zarr writer and the pyramid for the native download agent (tools/native.py). No TensorSwitch.

Reading: `read(path, reader, dataset)` returns the array as stored plus what the file says about it (axes,
TIFF Orientation, voxel size). `prepare(...)` applies what the agent settled (axes, transforms) and the
TIFF Orientation tag, and returns the array in canonical axis order: c, z, y, x for images (c only when
there is more than one channel), z, y, x for labels.

Writing (miao#13, same format as TensorSwitch's preset `miaai`): zarr v3, 128³ chunks in 512³ shards
(channel axis 1), zstd level 5, C order, dtype kept, OME-NGFF 0.5 multiscales s0, s1, … with `mean` for
images and `mode` for labels. Each level halves the axes whose voxel size is under twice the finest one
(so anisotropic z is only halved once x/y have caught up), until every spatial axis is at most 256.
"""
import json
import math
from pathlib import Path

import numpy as np

SOFTWARE = {"name": "mia-agentic-search native converter", "version": "1",
            "url": "https://github.com/AI-HHMI/mia-agentic-search"}
CHUNK, SHARD = 128, 512
STOP_SIZE = 256           # no further level once every spatial axis is at most this
MAX_LEVELS = 8
MAX_BYTES = 64 * 1024 ** 3  # one array is converted in memory (a few copies at peak): bigger ones are refused
READERS = ("tiff", "hdf5", "mrc", "nifti", "zarr")

# TIFF Orientation (274) -> how to turn the stored page into the displayed one: (transpose y/x, flip y, flip x),
# applied in that order. 1 = as stored. 5-8 swap the y and x sizes.
ORIENTATION_OPS = {1: (False, False, False), 2: (False, False, True), 3: (False, True, True),
                   4: (False, True, False), 5: (True, False, False), 6: (True, False, True),
                   7: (True, True, True), 8: (True, True, False)}


def guess_reader(path):
    p = str(path).lower()
    if p.endswith((".tif", ".tiff")):
        return "tiff"
    if p.endswith((".h5", ".hdf5", ".hdf", ".n5h5")):
        return "hdf5"
    if p.endswith((".mrc", ".rec", ".st", ".map", ".ali")):
        return "mrc"
    if p.endswith((".nii", ".nii.gz")):
        return "nifti"
    if p.rstrip("/").endswith(".zarr") or (Path(path) / "zarr.json").exists() or (Path(path) / ".zarray").exists():
        return "zarr"
    return None


def _enum(v):
    return None if v is None else str(getattr(v, "name", v))


def _tiff_info(path):
    import tifffile
    with tifffile.TiffFile(path) as t:
        s = t.series[0]
        pg = t.pages[0]
        orient = int(pg.tags["Orientation"].value) if "Orientation" in pg.tags else 1
        info = {"reader": "tiff", "shape": list(s.shape), "dtype": str(s.dtype), "axes_in_file": s.axes.lower(),
                "orientation": orient, "pages": len(t.pages), "imagej": {k: (v[:300] + " …") if isinstance(v, str) and len(v) > 300 else v
                                                   for k, v in (t.imagej_metadata or {}).items()},
                "ome": bool(t.is_ome), "photometric": _enum(pg.photometric),
                "samples_per_pixel": int(pg.samplesperpixel), "compression": _enum(pg.compression)}
        try:
            xr = pg.tags["XResolution"].value
            if xr and xr[0]:
                unit = (info["imagej"].get("unit") or "").lower()
                per = {"micron": 1000.0, "um": 1000.0, "µm": 1000.0, "\\u00b5m": 1000.0, "nm": 1.0, "nanometer": 1.0}.get(unit)
                if per:
                    xy = per * xr[1] / xr[0]
                    z = info["imagej"].get("spacing")
                    info["voxel_size_nm_in_file"] = {"x": round(xy, 3), "y": round(xy, 3),
                                                     "z": round(per * float(z), 3) if z else None}
        except Exception:  # noqa: BLE001
            pass
    return info


def _h5_datasets(path):
    import h5py
    out = []
    with h5py.File(path, "r") as f:
        f.visititems(lambda name, obj: out.append({"dataset": name, "shape": list(obj.shape), "dtype": str(obj.dtype),
                                                   "attrs": {k: _plain(v) for k, v in list(obj.attrs.items())[:20]}})
                     if isinstance(obj, h5py.Dataset) else None)
    return out


def _plain(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, np.ndarray):
        return v.tolist() if v.size <= 16 else f"array{list(v.shape)}"
    if isinstance(v, np.generic):
        return v.item()
    return v if isinstance(v, (str, int, float, bool, type(None))) else str(v)


def info(path, reader=None, dataset=None):
    """What the file says about itself, without reading the data (TIFF tags, HDF5 datasets, MRC header)."""
    reader = reader or guess_reader(path)
    if reader == "tiff":
        return _tiff_info(path)
    if reader == "hdf5":
        return {"reader": "hdf5", "datasets": _h5_datasets(path), "dataset": dataset}
    if reader == "mrc":
        import mrcfile
        with mrcfile.mmap(path, permissive=True) as m:
            vs = m.voxel_size
            return {"reader": "mrc", "shape": list(m.data.shape), "dtype": str(m.data.dtype), "axes_in_file": "zyx",
                    "voxel_size_nm_in_file": {"x": float(vs.x) / 10, "y": float(vs.y) / 10, "z": float(vs.z) / 10}}
    if reader == "nifti":
        import nibabel
        img = nibabel.load(path)
        zooms = img.header.get_zooms()
        return {"reader": "nifti", "shape": list(img.shape), "dtype": str(img.get_data_dtype()), "axes_in_file": "xyz",
                "zooms": [float(z) for z in zooms], "units": img.header.get_xyzt_units(),
                "affine": np.round(img.affine, 4).tolist()}
    if reader == "zarr":
        import zarr
        node = zarr.open(path if not dataset else f"{path}/{dataset}", mode="r")
        if hasattr(node, "shape"):
            return {"reader": "zarr", "shape": list(node.shape), "dtype": str(node.dtype),
                    "dimension_names": list(getattr(node.metadata, "dimension_names", None) or []) or None}
        return {"reader": "zarr", "arrays": sorted(k for k, _ in node.arrays())}
    raise ValueError(f"no reader for {path} (readers: {', '.join(READERS)})")


def read(path, reader=None, dataset=None):
    """(array as stored, info)."""
    reader = reader or guess_reader(path)
    meta = info(path, reader, dataset)
    if reader == "tiff":
        import tifffile
        arr = tifffile.imread(path, series=0)
    elif reader == "hdf5":
        import h5py
        if not dataset:
            raise ValueError(f"{path}: HDF5 needs a dataset name (one of {[d['dataset'] for d in meta['datasets']]})")
        with h5py.File(path, "r") as f:
            arr = f[dataset][()]
    elif reader == "mrc":
        import mrcfile
        with mrcfile.open(path, permissive=True) as m:
            arr = np.array(m.data)
    elif reader == "nifti":
        import nibabel
        arr = np.asarray(nibabel.load(path).dataobj)
    elif reader == "zarr":
        import zarr
        arr = np.asarray(zarr.open(path if not dataset else f"{path}/{dataset}", mode="r")[...])
    else:
        raise ValueError(f"no reader for {path}")
    if arr.nbytes > MAX_BYTES:
        raise ValueError(f"{path}: {arr.nbytes / 1e9:.1f} GB in memory, over the {MAX_BYTES / 1e9:.0f} GB limit")
    return arr, meta


def apply_orientation(arr, axes, orientation):
    """Turn TIFF pages as stored into the orientation the tag says (on the y and x axes of `axes`)."""
    transpose, fy, fx = ORIENTATION_OPS.get(orientation, (False, False, False))
    yi, xi = axes.index("y"), axes.index("x")
    if transpose:
        arr = np.swapaxes(arr, yi, xi)
    if fy:
        arr = np.flip(arr, yi)
    if fx:
        arr = np.flip(arr, xi)
    return arr


def prepare(spec_array, role):
    """Read one array and bring it into canonical order. Returns (array, report of what was done)."""
    axes = (spec_array.get("axes") or "").lower().replace("s", "c")
    if not axes:
        raise ValueError(f"{spec_array['path']}: axes not set (python tools/native.py set ... axes=...)")
    arr, meta = read(spec_array["path"], spec_array.get("reader"), spec_array.get("dataset"))
    done = []
    if arr.ndim != len(axes):
        raise ValueError(f"{spec_array['path']}: axes {axes!r} for an array of {arr.ndim} dimensions {list(arr.shape)}")
    if set(axes) - set("czyx") or len(set(axes)) != len(axes) or not {"y", "x"} <= set(axes):
        raise ValueError(f"axes {axes!r}: use each of c, z, y, x at most once (s = samples, taken as c); y and x needed")
    orient = meta.get("orientation", 1)
    if orient != 1 and spec_array.get("orientation", "apply") == "apply":
        arr = apply_orientation(arr, axes, orient)
        done.append(f"TIFF Orientation {orient} applied")
    for t in spec_array.get("transforms") or []:
        op, ax = t["op"], t.get("axis")
        if op == "flip":
            arr = np.flip(arr, axes.index(ax))
        elif op == "select":                       # keep one index of an axis, e.g. one channel of grey-as-RGB
            arr = np.take(arr, int(t["index"]), axis=axes.index(ax))
            axes = axes.replace(ax, "")
        else:
            raise ValueError(f"unknown transform {op!r} (flip, select)")
        done.append(f"{op} {ax}{' ' + str(t['index']) if op == 'select' else ''}: {t.get('reason')}")
    if "z" not in axes:
        arr, axes = arr[np.newaxis], "z" + axes
        done.append("added a z axis of size 1")
    target = ("c" if "c" in axes else "") + "zyx"
    if role == "label" and spec_array.get("values"):     # one class of a multi-class label (native.py split)
        arr = np.isin(arr, spec_array["values"]).astype(np.uint8)
        done.append(f"mask of values {spec_array['values']} ({spec_array.get('label_class')})")
    if role == "label":
        if "c" in axes:
            if arr.shape[axes.index("c")] != 1:
                raise ValueError(f"label with {arr.shape[axes.index('c')]} channels: one array per label set")
            arr, axes = np.take(arr, 0, axis=axes.index("c")), axes.replace("c", "")
            target = "zyx"
        if arr.dtype == bool:
            arr = arr.astype(np.uint8)
            done.append("bool -> uint8")
        if not np.issubdtype(arr.dtype, np.integer):
            cast = spec_array.get("dtype")
            if not cast:
                raise ValueError(f"label dtype {arr.dtype}: labels must be integers (set dtype=uint16 etc. if the "
                                 "values are whole numbers)")
            if not np.array_equal(arr, np.round(arr)):
                raise ValueError("label values are not whole numbers; refusing to cast")
            arr = arr.astype(cast)
            done.append(f"cast to {cast} (whole-number values)")
    elif "c" in axes and arr.shape[axes.index("c")] == 1:
        arr, axes = np.take(arr, 0, axis=axes.index("c")), axes.replace("c", "")
        target = "zyx"
    if axes != target:
        arr = np.transpose(arr, [axes.index(a) for a in target])
        done.append(f"axes {axes} -> {target}")
    return np.ascontiguousarray(arr), {"source_shape": list(meta.get("shape") or []), "axes_given": spec_array["axes"],
                                       "orientation_tag": orient, "done": done, "axes": target,
                                       "shape": list(arr.shape), "dtype": str(arr.dtype)}


# ---------- pyramid ----------
def level_factors(voxel_zyx, shape_zyx):
    """[(factors zyx, voxel zyx)] per level from s0."""
    out, v, s = [((1, 1, 1), tuple(voxel_zyx))], list(voxel_zyx), list(shape_zyx)
    while max(s) > STOP_SIZE and len(out) < MAX_LEVELS:
        finest = min(v[i] for i in range(3) if s[i] > 1)
        f = tuple(2 if s[i] > 1 and v[i] < 2 * finest else 1 for i in range(3))
        if f == (1, 1, 1):
            break
        v = [v[i] * f[i] for i in range(3)]
        s = [math.ceil(s[i] / f[i]) for i in range(3)]
        out.append((f, tuple(v)))
    return out


def _pad_even(a, factors):
    pad = [(0, (f - n % f) % f) for n, f in zip(a.shape, factors)]
    return np.pad(a, pad, mode="edge") if any(p[1] for p in pad) else a


def _blocks(a, factors):
    """(N..., k) view: the voxels of each output block in the last axis."""
    a = _pad_even(a, factors)
    shp = []
    for n, f in zip(a.shape, factors):
        shp += [n // f, f]
    b = a.reshape(shp)
    nd = a.ndim
    b = b.transpose([2 * i for i in range(nd)] + [2 * i + 1 for i in range(nd)])
    return b.reshape(b.shape[:nd] + (-1,))


def downsample(a, factors, method, slab=32):
    """One level down: `mean` (rounded for integers) or `mode` (ties go to the non-zero, then smaller, value).
    Works in slabs along the first spatial axis to bound memory. `factors` covers every axis of `a`."""
    factors = tuple(factors)
    if all(f == 1 for f in factors):
        return a.copy()
    zi = a.ndim - 3
    step = slab * factors[zi]
    parts = []
    for z0 in range(0, a.shape[zi], step):
        sl = [slice(None)] * a.ndim
        sl[zi] = slice(z0, z0 + step)
        b = _blocks(a[tuple(sl)], factors)
        if method == "mean":
            m = b.mean(axis=-1, dtype=np.float64)
            parts.append((np.rint(m) if np.issubdtype(a.dtype, np.integer) else m).astype(a.dtype))
        else:
            v = np.sort(b, axis=-1)
            counts = (v[..., :, None] == v[..., None, :]).sum(-1).astype(np.float32)
            counts -= (v == 0) * 0.5                 # a tie between background and a label keeps the label
            parts.append(np.take_along_axis(v, counts.argmax(-1)[..., None], -1)[..., 0])
    return np.concatenate(parts, axis=zi)


# ---------- writer ----------
def _axes_meta(axes):
    return [{"name": "c", "type": "channel"} if a == "c" else {"name": a, "type": "space", "unit": "nanometer"}
            for a in axes]


def _datasets(levels, axes, prefix=""):
    out = []
    v0 = levels[0][1]
    for i, (_, v) in enumerate(levels):
        scale = ([1.0] if "c" in axes else []) + [float(x) for x in v]
        t = [{"type": "scale", "scale": scale}]
        if i:
            t.append({"type": "translation", "translation": ([0.0] if "c" in axes else [])
                      + [(float(v[j]) - float(v0[j])) / 2 for j in range(3)]})
        out.append({"path": f"{prefix}s{i}", "coordinateTransformations": t})
    return out


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n")


def _write_array(path, a, axes):
    import zarr
    from zarr.codecs import ZstdCodec
    chunks = tuple(1 if ax == "c" else CHUNK for ax in axes)          # as TensorSwitch's miaai, also for
    shards = tuple(1 if ax == "c" else SHARD for ax in axes)          # axes shorter than a chunk
    z = zarr.create_array(store=str(path), shape=a.shape, dtype=a.dtype, chunks=chunks, shards=shards,
                          compressors=ZstdCodec(level=5, checksum=False), fill_value=0, dimension_names=list(axes),
                          zarr_format=3, overwrite=True)
    z[...] = a


def omero(a, axes):
    ch = a.shape[0] if "c" in axes else 1
    colors = ["808080"] if ch == 1 else ["FF0000", "00FF00", "0000FF", "FF00FF", "00FFFF", "FFFF00"]
    chans = []
    for i in range(ch):
        x = (a[i] if "c" in axes else a)[:: max(1, a.shape[-3] // 16)].ravel()
        lo, hi = (float(v) for v in np.percentile(x, [0.5, 99.5]))
        chans.append({"active": True, "coefficient": 1, "color": colors[i % len(colors)], "family": "linear",
                      "inverted": False, "label": f"Channel {i}",
                      "window": {"start": lo, "end": hi, "min": float(x.min()), "max": float(x.max())}})
    return {"channels": chans, "rdefs": {"defaultT": 0, "defaultZ": a.shape[-3] // 2, "model":
                                          "greyscale" if ch == 1 else "color"}}


def write_multiscale(group, a, axes, voxel_xyz, kind, attrs):
    """Write `a` (axes c?zyx) as an OME-NGFF multiscale group with levels s0, s1, …; returns the level list."""
    vz = (voxel_xyz["z"], voxel_xyz["y"], voxel_xyz["x"])
    levels = level_factors(vz, a.shape[-3:])
    lvl = a
    for i, (f, _) in enumerate(levels):
        if i:
            lvl = downsample(lvl, ((1,) if "c" in axes else ()) + f, "mean" if kind == "image" else "mode")
        _write_array(group / f"s{i}", lvl, axes)
    ms = {"axes": _axes_meta(axes), "datasets": _datasets(levels, axes),
          "coordinateTransformations": [{"type": "scale", "scale": [1.0] * len(axes)}]}
    ome = {"version": "0.5", "multiscales": [ms if kind == "label" else {**ms, "type": "image"}]}
    if kind == "image":
        ome["omero"] = omero(a, axes)
    else:
        ome["image-label"] = {"version": "0.5", "source": {"image": "../../"}}
    _write_json(group / "zarr.json", {"zarr_format": 3, "node_type": "group",
                                      "attributes": {"ome": ome, "_software": SOFTWARE, **attrs}})
    return [{"path": f"s{i}", "factors": list(f), "voxel_zyx": list(v)} for i, (f, v) in enumerate(levels)]


def write_crop_metadata(crop, raw_axes, levels, label_keys):
    """Root group (multiscales pointing at raw/) and the labels group."""
    ome = {"version": "0.5", "multiscales": [{
        "axes": _axes_meta(raw_axes),
        "datasets": _datasets([(tuple(l["factors"]), tuple(l["voxel_zyx"])) for l in levels], raw_axes, "raw/"),
        "type": "image", "coordinateTransformations": [{"type": "scale", "scale": [1.0] * len(raw_axes)}]}]}
    _write_json(crop / "zarr.json", {"zarr_format": 3, "node_type": "group",
                                     "attributes": {"ome": ome, "_software": SOFTWARE}})
    if label_keys:
        _write_json(crop / "labels" / "zarr.json", {"zarr_format": 3, "node_type": "group", "attributes": {
            "ome": {"version": "0.5", "labels": list(label_keys)}, "_software": SOFTWARE}})
