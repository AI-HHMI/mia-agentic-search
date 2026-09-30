"""Read array metadata (shape, axes, dtype, compression, chunks, voxel size) from file headers,
using HTTP range requests: usually a few KB per file, never the whole file.

    python tools/probe.py <url> [--id RECORD_ID]
    python tools/probe.py <zip url> --member <path in zip> [--id ID]
    python tools/probe.py <zip url> --glob '*_gt.tif' [--n 3] [--id ID]

Formats: TIFF / OME-TIFF / ImageJ TIFF / BigTIFF / SVS, MRC (.mrc .rec .st .map .mrcs), HDF5,
Zarr v2/v3 and OME-Zarr (incl. its labels/), N5, neuroglancer precomputed (`info`), PNG, JPEG,
NIfTI (.nii, .nii.gz). Members of a remote .zip are read in place (stored members fully by range,
deflated members from their first --max-bytes). CZI / ND2 / LIF need tools/sample.py.

Prints one JSON object per file. Header values are facts about the file; `voxel_size_nm` is only
reported when the header states it with a unit. Each probe is recorded in the active run log.
"""
import argparse
import fnmatch
import io
import json
import re
import struct
import sys
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import JUNK_PATH, polite_get  # noqa: E402
from tools.peek_archive import read_member, remote_size, zip_entries  # noqa: E402
from tools.run_log import log_inspection  # noqa: E402

BLOCK = 256 * 1024
UNIT_NM = {"nm": 1, "nanometer": 1, "µm": 1000, "um": 1000, "micron": 1000, "micrometer": 1000, "\\u00B5m": 1000,
           "mm": 1e6, "millimeter": 1e6, "å": 0.1, "a": 0.1, "angstrom": 0.1, "m": 1e9, "meter": 1e9}


def http_url(url):
    """s3:// → virtual-host https; ftp:// → https on the same host (EBI and most mirrors serve both)."""
    u = urlsplit(url)
    if u.scheme == "s3":
        return f"https://{u.netloc}.s3.amazonaws.com{u.path}"
    if u.scheme == "ftp":
        return f"https://{u.netloc}{u.path}"
    return url


class RangeFile(io.RawIOBase):
    """Seekable read-only file over HTTP range requests (block-cached), optionally a slice of a bigger file."""

    def __init__(self, url, base=0, size=None):
        self.url, self.base, self.pos = url, base, 0
        self.size = size if size is not None else remote_size(url) - base
        self.blocks, self.bytes_read, self.requests = {}, 0, 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def tell(self):
        return self.pos

    def _block(self, i):
        if i not in self.blocks:
            start = self.base + i * BLOCK
            end = min(self.base + self.size, start + BLOCK) - 1
            r = polite_get(self.url, headers={"Range": f"bytes={start}-{end}"})
            if r.status_code != 206:
                raise OSError(f"range request refused (HTTP {r.status_code})")
            self.blocks[i] = r.content
            self.bytes_read += len(r.content)
            self.requests += 1
        return self.blocks[i]

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        out = bytearray()
        while len(out) < n:
            i, o = divmod(self.pos + len(out), BLOCK)
            out += self._block(i)[o:o + n - len(out)]
        self.pos += n
        return bytes(out)

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)


class BufferFile(io.BytesIO):
    """In-memory prefix of a file (e.g. a deflated zip member); reads past it raise."""

    def __init__(self, data, complete):
        super().__init__(data)
        self.complete, self.bytes_read, self.requests = complete, len(data), 0
        self.size = len(data)

    def read(self, n=-1):
        if not self.complete and n is not None and n >= 0 and self.tell() + n > self.size:
            raise EOFError("header lies beyond the part of the zip member that was read; use tools/sample.py")
        return super().read(n)


# ---------- TIFF ----------
TIFF_COMPRESSION = {1: "none", 2: "other", 5: "lzw", 6: "jpeg", 7: "jpeg", 8: "deflate", 32946: "deflate",
                    32773: "packbits", 34712: "jpeg2000", 33003: "jpeg2000", 33005: "jpeg2000", 34925: "lzma",
                    50000: "zstd", 34926: "zstd", 50001: "webp", 34927: "webp", 50002: "jpegxl", 52546: "jpegxl"}
LOSSY = {"jpeg": True, "webp": None, "jpeg2000": None, "jpegxl": None}
TIFF_TYPES = {1: "B", 2: "s", 3: "H", 4: "I", 5: "II", 6: "b", 7: "B", 8: "h", 9: "i", 10: "ii", 11: "f", 12: "d",
              16: "Q", 17: "q", 18: "Q"}


def _dtype(kind, bits):
    if bits == 1:
        return "bool"
    prefix = {1: "uint", 2: "int", 3: "float"}.get(kind, "uint")
    return f"{prefix}{bits}" if bits in (8, 16, 32, 64) or (prefix == "float" and bits in (16, 32, 64)) else "unknown"


def _read_ifd(f, off, endian, big):
    f.seek(off)
    n = struct.unpack(endian + ("Q" if big else "H"), f.read(8 if big else 2))[0]
    entry = 20 if big else 12
    raw = f.read(n * entry + (8 if big else 4))
    tags = {}
    for k in range(n):
        e = raw[k * entry:(k + 1) * entry]
        if big:
            tag, typ, cnt = struct.unpack(endian + "HHQ", e[:12])
            val = e[12:20]
        else:
            tag, typ, cnt = struct.unpack(endian + "HHI", e[:8])
            val = e[8:12]
        fmt = TIFF_TYPES.get(typ, "B")
        size = struct.calcsize(fmt) * cnt
        if tag in (273, 279, 324, 325):  # strip/tile offsets and byte counts: not needed
            continue
        if size > len(val):
            ptr = struct.unpack(endian + ("Q" if big else "I"), val)[0]
            here = f.tell()
            f.seek(ptr)
            val = f.read(min(size, 1 << 20))
            f.seek(here)
        if typ == 2:
            tags[tag] = val[:cnt].split(b"\0", 1)[0].decode("utf-8", "replace")
        else:
            vals = struct.unpack(endian + fmt * cnt, val[:size]) if size <= len(val) else ()
            tags[tag] = vals
    nxt = struct.unpack(endian + ("Q" if big else "I"), raw[n * entry:n * entry + (8 if big else 4)])[0]
    return tags, nxt


def _first(tags, tag, default=None):
    v = tags.get(tag)
    return v[0] if isinstance(v, tuple) and v else default


def probe_tiff(f, max_pages=100000, request_budget=15):
    head = f.read(16)
    endian = "<" if head[:2] == b"II" else ">"
    big = struct.unpack(endian + "H", head[2:4])[0] == 43
    off = struct.unpack(endian + "Q", head[8:16])[0] if big else struct.unpack(endian + "I", head[4:8])[0]
    tags, nxt = _read_ifd(f, off, endian, big)
    w, h = _first(tags, 256), _first(tags, 257)
    spp = _first(tags, 277, 1)
    bits = (tags.get(258) or (1,))[0]
    kind = _first(tags, 339, 1)
    comp = TIFF_COMPRESSION.get(_first(tags, 259, 1), "other")
    desc = tags.get(270, "") if isinstance(tags.get(270), str) else ""
    res = {"format": "tiff", "dtype": _dtype(kind, bits), "compression": comp, "lossy": LOSSY.get(comp, False),
           "page_shape": [h, w] + ([spp] if spp > 1 else []), "photometric": _first(tags, 262),
           "tiled": 322 in tags, "chunks": [_first(tags, 323), _first(tags, 322)] if 322 in tags else None,
           "bigtiff": big, "notes": []}
    if _first(tags, 262) == 3:
        res["notes"].append("palette (colour-mapped) image: values are indices, often label IDs")

    # page count: walk the IFD chain while it is cheap (IFDs are often contiguous, i.e. in cached blocks)
    pages, req0 = 1, getattr(f, "requests", 0)
    try:
        while nxt and pages < max_pages and getattr(f, "requests", 0) - req0 < request_budget:
            f.seek(nxt)
            n = struct.unpack(endian + ("Q" if big else "H"), f.read(8 if big else 2))[0]
            f.seek(nxt + (8 if big else 2) + n * (20 if big else 12))
            nxt = struct.unpack(endian + ("Q" if big else "I"), f.read(8 if big else 4))[0]
            pages += 1
    except EOFError:
        pass
    res["pages_counted"] = pages
    res["pages_complete"] = not nxt

    # ImageJ / OME metadata give the real axes
    if desc.startswith("ImageJ="):
        ij = dict(re.findall(r"^(\w+)=(.*)$", desc, re.M))
        dims = [("t", "frames"), ("z", "slices"), ("c", "channels")]
        shape, axes = [], ""
        for ax, key in dims:
            if int(ij.get(key, 1)) > 1:
                shape.append(int(ij[key]))
                axes += ax
        res["shape"], res["axes"] = shape + [h, w] + ([spp] if spp > 1 else []), axes + "yx" + ("s" if spp > 1 else "")
        res["imagej"] = {k: ij[k] for k in ("images", "channels", "slices", "frames", "spacing", "unit", "min", "max") if k in ij}
        unit = (ij.get("unit") or "").replace("\\u00B5", "µ").lower()
        xres = tags.get(282)
        if unit in UNIT_NM and xres and len(xres) == 2 and xres[0]:
            px = xres[1] / xres[0] * UNIT_NM[unit]
            z = float(ij["spacing"]) * UNIT_NM[unit] if "spacing" in ij else None
            res["voxel_size_nm"] = {"x": round(px, 4), "y": round(px, 4), "z": round(z, 4) if z else None}
    elif "<OME" in desc:
        try:
            root = ET.fromstring(desc)
            px = next(el for el in root.iter() if el.tag.endswith("Pixels"))
            a = px.attrib
            order = a.get("DimensionOrder", "XYZCT")[::-1].lower()  # slowest first
            sizes = {ax: int(a.get(f"Size{ax.upper()}", 1)) for ax in "xyzct"}
            axes = "".join(ax for ax in order if sizes[ax] > 1 or ax in "yx")
            res["format"] = "ome-tiff"
            res["shape"], res["axes"] = [sizes[ax] for ax in axes], axes
            res["ome_type"] = a.get("Type")
            vs = {}
            for ax in "xyz":
                v, u = a.get(f"PhysicalSize{ax.upper()}"), a.get(f"PhysicalSize{ax.upper()}Unit", "µm")
                if v and u.lower() in UNIT_NM:
                    vs[ax] = round(float(v) * UNIT_NM[u.lower()], 4)
            if vs:
                res["voxel_size_nm"] = {"x": vs.get("x"), "y": vs.get("y"), "z": vs.get("z")}
            n_series = sum(1 for el in root.iter() if el.tag.endswith("}Image"))
            if n_series > 1:
                res["notes"].append(f"OME-XML describes {n_series} images (series); shape is the first")
        except (ET.ParseError, StopIteration) as e:
            res["notes"].append(f"OME-XML not parsed: {e}")
    elif desc.startswith("{") and '"shape"' in desc:  # tifffile's "shaped" metadata
        try:
            res["shape"] = json.loads(desc)["shape"]
            res["axes"] = "yx" if res["shape"] == [h, w] else None
            if res["axes"] is None:
                res["notes"].append("shape from tifffile metadata; axis order not stated in the file")
        except (ValueError, KeyError):
            pass
    if "shape" not in res:
        res["shape"] = ([pages] if pages > 1 else []) + [h, w] + ([spp] if spp > 1 else [])
        res["axes"] = None if pages > 1 else "yx" + ("s" if spp > 1 else "")
        res["notes"].append("plain multi-page TIFF: page axis could be z, t or c (not stated in the file)" if pages > 1
                            else "single page")
        if not res["pages_complete"]:
            res["notes"].append(f"stopped counting pages at {pages}; shape[0] is a lower bound")
    if res.get("voxel_size_nm"):
        res["notes"].append("voxel size is the calibration stored in the file; check it is plausible "
                            "(uncalibrated files often say 1 px = 1 unit)")
    if desc and not desc.startswith(("ImageJ=", "{")) and "<OME" not in desc:
        res["description"] = desc[:300]
    return res


# ---------- MRC ----------
MRC_MODES = {0: "int8", 1: "int16", 2: "float32", 3: "complex32", 4: "complex64", 6: "uint16", 12: "float16", 101: "uint4"}


def probe_mrc(f):
    h = f.read(1024)
    stamp = h[212:214]
    endian = ">" if stamp[:1] == b"\x11" else "<"
    nx, ny, nz, mode = struct.unpack(endian + "4i", h[:16])
    mx, my, mz = struct.unpack(endian + "3i", h[28:40])
    xl, yl, zl = struct.unpack(endian + "3f", h[40:52])
    dmin, dmax, dmean = struct.unpack(endian + "3f", h[76:88])
    ispg = struct.unpack(endian + "i", h[88:92])[0]
    nsym = struct.unpack(endian + "i", h[92:96])[0]
    res = {"format": "mrc", "shape": [nz, ny, nx] if nz > 1 else [ny, nx], "axes": ("z" if nz > 1 else "") + "yx",
           "dtype": MRC_MODES.get(mode, f"mode{mode}"), "compression": "none", "lossy": False,
           "header_min_max_mean": [dmin, dmax, dmean], "extended_header_bytes": nsym, "notes": []}
    if mode == 0:
        res["notes"].append("mode 0 is int8 by the MRC2014 standard; some writers store uint8")
    if ispg in (0, 1) and nz > 1 and ispg == 0:
        res["notes"].append("space group 0: image stack (z may be a stack of 2D images, not a volume)")
    if all(v > 0 for v in (mx, my, xl, yl)):
        res["voxel_size_nm"] = {"x": round(xl / mx / 10, 4), "y": round(yl / my / 10, 4),
                                "z": round(zl / mz / 10, 4) if nz > 1 and mz > 0 and zl > 0 else None}
        res["notes"].append("voxel size from cell dimensions / sampling (Å → nm); 1 Å often means unset")
    return res


# ---------- HDF5 ----------
H5_FILTERS = {1: "gzip", 2: "shuffle", 3: "fletcher32", 4: "szip", 32000: "lzf", 32001: "blosc", 32004: "lz4",
              32008: "bitshuffle", 32015: "zstd", 307: "bz2"}


def probe_hdf5(f, max_datasets=40):
    import h5py
    res = {"format": "hdf5", "datasets": [], "notes": []}
    with h5py.File(f, "r") as h5:
        def visit(name, obj):
            if isinstance(obj, h5py.Dataset) and len(res["datasets"]) < max_datasets:
                plist = obj.id.get_create_plist()
                filters = [H5_FILTERS.get(plist.get_filter(i)[0], str(plist.get_filter(i)[0]))
                           for i in range(plist.get_nfilters())]
                attrs = {k: (v.tolist() if hasattr(v, "tolist") else str(v)) for k, v in obj.attrs.items()
                         if re.search(r"res|voxel|spacing|element_size|scale|offset|unit|axis|axes|dim", k, re.I)}
                res["datasets"].append({"name": name, "shape": list(obj.shape), "dtype": str(obj.dtype),
                                        "chunks": list(obj.chunks) if obj.chunks else None,
                                        "compression": "+".join(filters) or "none", "attrs": attrs})
        h5.visititems(visit)
        root_attrs = {k: (v.tolist() if hasattr(v, "tolist") else str(v)) for k, v in h5.attrs.items()}
        if root_attrs:
            res["root_attrs"] = {k: root_attrs[k] for k in list(root_attrs)[:20]}
    if len(res["datasets"]) >= max_datasets:
        res["notes"].append(f"only the first {max_datasets} datasets are listed")
    return res


# ---------- Zarr / OME-Zarr / N5 / precomputed ----------
def _json(url):
    r = polite_get(url)
    return r.json() if r.status_code == 200 else None


ZARR_DTYPES = {"|u1": "uint8", "<u2": "uint16", "<u4": "uint32", "<u8": "uint64", "|i1": "int8", "<i2": "int16",
               "<i4": "int32", "<i8": "int64", "<f2": "float16", "<f4": "float32", "<f8": "float64", "|b1": "bool",
               ">u2": "uint16", ">u4": "uint32", ">i2": "int16", ">f4": "float32", ">f8": "float64"}


def _zarr_array(base):
    meta = _json(base + "/.zarray")
    if meta:
        comp = (meta.get("compressor") or {}).get("id", "none")
        if comp == "blosc":
            comp = f"blosc-{(meta['compressor'] or {}).get('cname', '?')}"
        return {"shape": meta["shape"], "chunks": meta["chunks"], "dtype": ZARR_DTYPES.get(meta["dtype"], meta["dtype"]),
                "compression": comp, "zarr_format": 2, "dimension_separator": meta.get("dimension_separator", ".")}
    meta = _json(base + "/zarr.json")
    if meta and meta.get("node_type") == "array":
        codecs = [c.get("name") for c in meta.get("codecs", [])]
        inner = [c.get("name") for c in meta.get("codecs", []) for c in (c.get("configuration") or {}).get("codecs", [])]
        chunks = (meta.get("chunk_grid") or {}).get("configuration", {}).get("chunk_shape")
        return {"shape": meta["shape"], "chunks": chunks, "dtype": meta.get("data_type"),
                "compression": "+".join(n for n in codecs + inner if n not in ("bytes", "transpose", "sharding_indexed")) or "none",
                "zarr_format": 3, "sharded": "sharding_indexed" in codecs}
    return None


def _ome_scale(attrs):
    ms = (attrs.get("multiscales") or [None])[0]
    if not ms:
        return None
    axes = ms.get("axes") or []
    names = "".join((a if isinstance(a, str) else a.get("name", "?"))[0] for a in axes) or None
    units = [None if isinstance(a, str) else a.get("unit") for a in axes]
    ds = ms["datasets"][0]
    scale = next((t["scale"] for t in ds.get("coordinateTransformations", []) if t.get("type") == "scale"), None)
    trans = next((t["translation"] for t in ds.get("coordinateTransformations", []) if t.get("type") == "translation"), None)
    vs = None
    if scale and names and units:
        vs = {}
        for ax, s, u in zip(names, scale, units):
            if ax in "xyz" and u and u.lower() in UNIT_NM:
                vs[ax] = round(s * UNIT_NM[u.lower()], 4)
        vs = {"x": vs.get("x"), "y": vs.get("y"), "z": vs.get("z")} if vs else None
    return {"path": ds["path"], "axes": names, "units": units, "scale": scale, "translation": trans,
            "n_levels": len(ms["datasets"]), "voxel_size_nm": vs}


def probe_zarr(url):
    base = http_url(url).rstrip("/")
    res = {"format": "zarr", "notes": []}
    arr = _zarr_array(base)
    if arr:
        res.update(arr)
        return res
    attrs = _json(base + "/.zattrs") or (_json(base + "/zarr.json") or {}).get("attributes", {})
    attrs = attrs.get("ome", attrs)  # OME-Zarr 0.5 nests under "ome"
    ms = _ome_scale(attrs)
    if not ms:
        res["notes"].append("group without multiscales metadata; pass the URL of an array inside it")
        res["attributes_keys"] = list(attrs)[:20]
        return res
    res["format"] = "ome-zarr"
    arr = _zarr_array(f"{base}/{ms['path']}") or {}
    res.update(arr)
    res.update({"axes": ms["axes"], "levels": ms["n_levels"], "scale": ms["scale"], "units": ms["units"],
                "translation": ms["translation"]})
    if ms["voxel_size_nm"]:
        res["voxel_size_nm"] = ms["voxel_size_nm"]
    labels = (_json(base + "/labels/.zattrs") or (_json(base + "/labels/zarr.json") or {}).get("attributes", {}) or {})
    labels = labels.get("ome", labels).get("labels", [])
    res["labels"] = []
    for name in labels[:10]:
        la = _json(f"{base}/labels/{name}/.zattrs") or (_json(f"{base}/labels/{name}/zarr.json") or {}).get("attributes", {})
        lms = _ome_scale((la or {}).get("ome", la or {}))
        if lms:
            larr = _zarr_array(f"{base}/labels/{name}/{lms['path']}") or {}
            res["labels"].append({"name": name, **larr, "axes": lms["axes"], "scale": lms["scale"],
                                  "translation": lms["translation"],
                                  "same_grid_as_image": larr.get("shape") == res.get("shape") and lms["scale"] == ms["scale"]})
    return res


N5_DTYPES = {"uint8", "uint16", "uint32", "uint64", "int8", "int16", "int32", "int64", "float32", "float64"}


def probe_n5(url):
    base = http_url(url).rstrip("/")
    res = {"format": "n5", "notes": ["N5 lists dimensions fastest-first (x, y, z); shape below is reversed to z, y, x"]}
    attrs = _json(base + "/attributes.json") or {}
    if "dimensions" not in attrs:
        res["group_attributes"] = {k: attrs[k] for k in list(attrs)[:20]}
        for sub in ("s0", "setup0/timepoint0/s0"):
            a = _json(f"{base}/{sub}/attributes.json")
            if a and "dimensions" in a:
                res["array_path"], attrs = sub, a
                break
        else:
            res["notes"].append("no array found at s0; pass the URL of an array")
            return res
    comp = attrs.get("compression", {})
    res.update({"shape": attrs["dimensions"][::-1], "chunks": attrs.get("blockSize", [])[::-1],
                "dtype": attrs.get("dataType"), "compression": comp.get("type", "none") if isinstance(comp, dict) else comp})
    return res


def probe_precomputed(url):
    base = http_url(url.removeprefix("precomputed://")).rstrip("/")
    info = _json(base + "/info")
    if not info:
        return {"format": "precomputed", "notes": ["no info file found"]}
    s0 = info["scales"][0]
    enc = s0.get("encoding")
    return {"format": "precomputed", "layer_type": info.get("type"), "dtype": info.get("data_type"),
            "channels": info.get("num_channels"), "shape": s0["size"][::-1] + ([info["num_channels"]] if info.get("num_channels", 1) > 1 else []),
            "axes": "zyx" + ("c" if info.get("num_channels", 1) > 1 else ""), "chunks": s0.get("chunk_sizes", [[None]])[0][::-1],
            "compression": enc, "lossy": True if enc == "jpeg" else False if enc in ("raw", "compressed_segmentation", "png", "compresso") else None,
            "voxel_size_nm": {"x": s0["resolution"][0], "y": s0["resolution"][1], "z": s0["resolution"][2]},
            "voxel_offset": s0.get("voxel_offset"), "levels": len(info["scales"]),
            "notes": ["precomputed resolution is in nm by the neuroglancer spec"]}


# ---------- PNG / JPEG / NIfTI ----------
def probe_png(f):
    h = f.read(33)
    w, ht, depth, ctype = struct.unpack(">IIBB", h[16:26])
    chans = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype, 1)
    res = {"format": "png", "shape": [ht, w] + ([chans] if chans > 1 else []), "axes": "yx" + ("s" if chans > 1 else ""),
           "dtype": "uint16" if depth == 16 else "bool" if depth == 1 else "uint8", "compression": "deflate", "lossy": False,
           "color_type": {0: "gray", 2: "rgb", 3: "palette", 4: "gray+alpha", 6: "rgba"}.get(ctype), "notes": []}
    if ctype == 3:
        res["notes"].append("palette PNG: values are indices, often label IDs")
    return res


def probe_jpeg(f):
    data = f.read(256 * 1024)
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            prec, ht, w, comps = struct.unpack(">BHHB", data[i + 4:i + 10])
            return {"format": "jpeg", "shape": [ht, w] + ([comps] if comps > 1 else []), "axes": "yx" + ("s" if comps > 1 else ""),
                    "dtype": "uint8" if prec == 8 else f"uint{prec}", "compression": "jpeg", "lossy": marker != 0xC3, "notes": []}
        i += 2 + seglen
    return {"format": "jpeg", "notes": ["no SOF marker in the first 256 KB"]}


NII_DTYPES = {2: "uint8", 4: "int16", 8: "int32", 16: "float32", 64: "float64", 256: "int8", 512: "uint16", 768: "uint32"}


def probe_nifti(f, gz):
    raw = f.read(64 * 1024 if gz else 352)
    h = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw) if gz else raw
    endian = "<" if struct.unpack("<i", h[:4])[0] == 348 else ">"
    dim = struct.unpack(endian + "8h", h[40:56])
    dtype = struct.unpack(endian + "h", h[70:72])[0]
    pix = struct.unpack(endian + "8f", h[76:108])
    units = h[123] & 0x07
    n = dim[0]
    shape = list(dim[1:1 + n])[::-1]
    mult = {1: 1e9, 2: 1e6, 3: 1e3}.get(units)
    res = {"format": "nifti", "shape": shape, "axes": "tzyx"[4 - min(n, 4):] if n <= 4 else None,
           "dtype": NII_DTYPES.get(dtype, f"code{dtype}"), "compression": "gzip" if gz else "none", "lossy": False, "notes": []}
    if mult:
        res["voxel_size_nm"] = {"x": round(pix[1] * mult, 4), "y": round(pix[2] * mult, 4), "z": round(pix[3] * mult, 4) if n >= 3 else None}
    return res


# ---------- dispatch ----------
def sniff(name, head):
    n = name.lower()
    if head[:4] in (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+"):
        return "tiff"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:2] == b"\xff\xd8":
        return "jpeg"
    if head[:8] == b"\x89HDF\r\n\x1a\n" or n.endswith((".h5", ".hdf5", ".hdf")):
        return "hdf5"
    if n.endswith((".mrc", ".rec", ".st", ".map", ".mrcs", ".ali", ".preali")) or head[208:212] == b"MAP ":
        return "mrc"
    if n.endswith(".nii.gz"):
        return "nifti-gz"
    if n.endswith(".nii") or head[344:348] in (b"n+1\0", b"ni1\0"):
        return "nifti"
    return None


def probe_file(f, name):
    f.seek(0)
    head = f.read(1024)
    kind = sniff(name, head)
    f.seek(0)
    if kind == "tiff":
        return probe_tiff(f)
    if kind == "png":
        return probe_png(f)
    if kind == "jpeg":
        return probe_jpeg(f)
    if kind == "hdf5":
        return probe_hdf5(f)
    if kind == "mrc":
        return probe_mrc(f)
    if kind in ("nifti", "nifti-gz"):
        return probe_nifti(f, kind == "nifti-gz")
    if head.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        return {"format": "other", "notes": ["URL returned an HTML page (a directory or landing page?); "
                                             "find file URLs with tools/listing.py"]}
    return {"format": "other", "notes": [f"format not recognised from name/header ({head[:8]!r}); "
                                         "CZI/ND2/LIF/DICOM need tools/sample.py"]}


def probe_url(url):
    u = http_url(url)
    path = urlsplit(u).path.lower().rstrip("/")
    if url.startswith("precomputed://") or path.endswith("/info"):
        return probe_precomputed(url.removesuffix("/info")), 0
    if path.endswith(".zarr") or re.search(r"\.zarr/", path + "/") or path.endswith((".zarray", "zarr.json")):
        return probe_zarr(re.sub(r"/(\.zarray|zarr\.json)$", "", u)), 0
    if path.endswith(".n5") or ".n5/" in path:
        return probe_n5(u), 0
    f = RangeFile(u)
    return probe_file(f, path), f.bytes_read


def probe_zip_member(url, entry, max_bytes):
    local = RangeFile(url, entry["offset"], 30)
    ln, le = struct.unpack("<HH", local.read(30)[26:30])
    start = entry["offset"] + 30 + ln + le
    if entry["method"] == 0:
        f = RangeFile(url, start, entry["csize"])
        res = probe_file(f, entry["name"])
        return res, f.bytes_read
    data, complete = read_member(url, entry, max_bytes)
    f = BufferFile(data, complete)
    f.size = entry["size"]  # for page-count extrapolation
    try:
        res = probe_file(f, entry["name"])
    except (EOFError, struct.error) as e:
        res = {"format": "unknown", "notes": [f"deflated member: {e}"]}
    return res, len(data)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--id", help="record id, stored with the run log entry")
    ap.add_argument("--member", help="path of one file inside the zip at <url>")
    ap.add_argument("--glob", help="probe the first --n members of the zip matching this pattern")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--max-bytes", type=int, default=8 * 10**6, help="for deflated zip members: bytes to inflate")
    a = ap.parse_args()

    targets = []
    if a.member or a.glob:
        _, entries = zip_entries(http_url(a.url))
        if a.member:
            targets = [e for e in entries if e["name"] == a.member]
            if not targets:
                sys.exit(f"{a.member} not in zip; list it with tools/peek_archive.py")
        else:
            targets = [e for e in entries if fnmatch.fnmatch(e["name"], a.glob) and not JUNK_PATH.search(e["name"])][:a.n]
            if not targets:
                sys.exit(f"no zip member matches {a.glob}")
    failed = False
    for entry in targets or [None]:
        try:
            res, nbytes = probe_zip_member(http_url(a.url), entry, a.max_bytes) if entry else probe_url(a.url)
        except Exception as e:  # noqa: BLE001
            res, nbytes, failed = {"format": "unknown", "error": f"{type(e).__name__}: {e}"}, 0, True
        out = {"url": a.url, **({"member": entry["name"], "member_size": entry["size"]} if entry else {}), **res,
               "bytes_read": nbytes}
        print(json.dumps(out, default=str))
        if "error" not in res:
            log_inspection("header", a.url, a.id, member=entry["name"] if entry else None, bytes=nbytes,
                           format=res.get("format"), shape=res.get("shape"), dtype=res.get("dtype"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
