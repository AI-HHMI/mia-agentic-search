"""Native download agent: download a record's files and write them into the miao layout without TensorSwitch,
then check the result numerically and visually. Driven by the /download-native skill.

    python tools/native.py plan <record.yaml> --root demo-native --label-class nucleus [--name N] [--organism O]
                               [--crop-files "url1|url2" ...] --out <spec.json>
    python tools/native.py fetch <spec.json> [--crop crop-001.zarr]
    python tools/native.py inspect <spec.json> [--crop C]            # what each staged file says about itself
    python tools/native.py set <spec.json> <crop> <raw|label|key|index> axes=zyx [dataset=…] [reader=…]
                               [dtype=uint16] [orientation=apply|ignore] [transform=flip:y | select:c:0]
                               [role=raw|label] [label_class=golgi] [clear=transforms] reason="…"
    python tools/native.py split <spec.json> <crop> <label index> 1=mitochondria 2=er --reason "…"   # one folder per class
    python tools/native.py split <spec.json> <crop> <label index> c0=nucleus c1=cell --reason "…"     # per channel
    python tools/native.py convert <spec.json> [--crop C]
    python tools/native.py check <spec.json> [--crop C]              # verify + overlay
    python tools/native.py review <spec.json> <crop> --verdict pass|fail|unsure --reason "…" --saw "…"
    python tools/native.py finalize <spec.json> [--keep-source] [--summary-out F]

The spec (written by `plan`, completed by the agent with `set`) holds per crop the files, their staged
paths, the record array each one is, and what the agent settled after `inspect`: the axes of each array as
read, the HDF5 dataset, and any transform with its reason. The TIFF Orientation tag is applied by
`convert` unless `orientation=ignore` (with a reason). Every `set` is logged in the crop's `decisions`.

Output: the same miao#13 layout and format as /download-dataset (tools/native_io.py), except that every
array is written in canonical axis order (c, z, y, x), so raw and labels always share axis order.
`finalize` records a crop in manifest.json only if `verify` passed (or was unverified only on alignment)
and the agent's visual review says pass; then it deletes the crop's downloads.
"""
import argparse
import datetime
import hashlib
import json
import re
import shutil
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import USER_AGENT, load_yaml, voxel_size_found  # noqa: E402
from tools.miao_layout import (LABEL_CLASSES, ORGANISM_SHORT, assign_crops, default_name, label_attrs,  # noqa: E402
                               slug)
from tools.readiness import family  # noqa: E402

NATIVE_FORMATS = {"tiff", "ome-tiff", "hdf5", "mrc", "nifti"}
CHUNK_BYTES = 8 * 1024 ** 2


# ---------- what the native agent can take ----------
def native_reasons(r):
    """Why this record can't be downloaded by the native agent (empty: it can). No TensorSwitch involved."""
    im, data, tech, an = r.get("imaging") or {}, r.get("data") or {}, r.get("technical") or {}, r.get("annotations") or {}
    arrays = [a for a in tech.get("arrays") or [] if isinstance(a, dict)]
    out = []
    if data.get("access") != "open":
        out.append(f"access {data.get('access')}: needs open access")
    if str(im.get("dimensionality")) != "3D":
        out.append(f"dimensionality {im.get('dimensionality')}: only 3D")
    if not voxel_size_found({**im, "dimensionality": "3D"}):
        out.append("imaging.voxel_size_nm needs x, y and z")
    if len(im.get("organism") or []) != 1:
        out.append(f"imaging.organism lists {len(im.get('organism') or [])} organisms: one per dataset folder")
    if family(im.get("modality") or []) is None:
        out.append(f"modality family unclear ({im.get('modality')})")
    if not an.get("present"):
        out.append("no annotations")
    if not (tech.get("sample") or {}).get("urls"):
        out.append("no technical.sample.urls")
    if not any(a.get("role") == "label" for a in arrays):
        out.append("no label array in technical.arrays")
    if roles := {a.get("role") for a in arrays} - {"raw", "label"}:
        out.append(f"array roles {sorted(roles, key=str)}: miao#13 only has raw/ and labels/")
    fmts = {a.get("format") for a in arrays if a.get("role") in ("raw", "label")}
    if bad := fmts - NATIVE_FORMATS:
        out.append(f"formats {sorted(bad, key=str)}: the native readers take {', '.join(sorted(NATIVE_FORMATS))}")
    if bad := sorted({a.get("alignment") for a in arrays if a.get("role") == "label"} &
                     {"offset", "cropped", "scaled", "transform-provided"}):
        out.append(f"label alignment {bad}: labels aren't on the raw grid at offset 0")
    for u in (tech.get("sample") or {}).get("urls") or []:
        if not re.match(r"^(https?|ftp|s3)://", u):
            out.append(f"sample URL {u}: not http(s)/ftp/s3")
    return out


# ---------- plan ----------
def _pattern_regex(pat):
    """technical.arrays[].path_pattern -> regex on the end of a file spec ({a,b}, NNN, <name>, *, ?)."""
    pat = pat.split("::")[-1]
    out, i = "", 0
    while i < len(pat):
        ch = pat[i]
        if ch == "{":
            j = pat.index("}", i)
            out += "(" + "|".join(re.escape(x) for x in pat[i + 1:j].split(",")) + ")"
            i = j + 1
            continue
        if pat.startswith("NNN", i):
            out, i = out + r"\d+", i + 3
            continue
        if ch == "<" and ">" in pat[i:]:                    # <stack>: one path component, and the same
            j = pat.index(">", i)                           # placeholder again must be the same text
            key = re.sub(r"\W", "_", pat[i + 1:j])
            out += f"(?P={key})" if f"(?P<{key}>" in out else f"(?P<{key}>[^/]+)"
            i = j + 1
            continue
        out += {"*": "[^/]*", "?": "."}.get(ch, re.escape(ch))
        i += 1
    return re.compile(f"(^|/){out}$")


def _member(spec):
    url, _, member = spec.partition("::")
    return member or url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]


def match_arrays(urls, arrays):
    """Record array index per URL by path_pattern; None if no pattern or several match."""
    regs = [(i, _pattern_regex(a["path_pattern"])) for i, a in enumerate(arrays)
            if a.get("path_pattern") and a.get("role") in ("raw", "label")]
    out = []
    for u in urls:
        m = _member(u)
        hits = [i for i, rx in regs if rx.search(m) or rx.search(u.split("::")[0])]
        out.append(hits[0] if len(hits) == 1 else None)
    return out


def _staged(staging, spec):
    from urllib.parse import unquote
    return str(staging / "source" / unquote(_member(spec)).lstrip("/"))


GENERIC_TOKENS = {"data", "train", "training", "val", "valid", "validation", "test", "mask", "masks", "label",
                  "labels", "image", "images", "raw", "tif", "tiff", "small", "vol", "volume", "seg", "segmentation",
                  "files", "file", "zip", "gt", "ground", "truth", "full", "annotation", "annotations", "source"}


def class_hints(r, url, ra):
    """Words in the file's path that the record's own text names (title, description, annotations.format,
    short_name): candidates for its label class, e.g. data_golgi/…/x_label.tif -> golgi. Evidence, not a choice."""
    text = " ".join(str(x) for x in (r.get("title"), r.get("short_name"), r.get("description"),
                                     (r.get("annotations") or {}).get("format"), ra.get("classes"))).lower()
    out = []
    from urllib.parse import unquote
    for tok in re.split(r"[^a-z0-9]+", unquote(_member(url)).lower()):
        if len(tok) < 3 or tok in GENERIC_TOKENS or tok.isdigit() or tok in out:
            continue
        stem = tok[:-1] if tok.endswith("s") and len(tok) > 4 else tok
        if re.search(rf"(?<![\w-]){re.escape(stem)}(s|es)?(?![\w-])", text):   # a word, not part of high_c1
            out.append(stem if stem in LABEL_CLASSES else tok)
    return out


def set_label_class(e, r, ra, label_class, voxel, today):
    """Label class -> the entry's miao#13 metadata and folder key ({provenance}-{class}-{info})."""
    attrs = label_attrs(r, ra, label_class, voxel, today)
    if e.get("values"):                                   # one class split out of a multi-class label: a mask
        attrs["segmentation_type"] = "semantic"
        attrs["review_needed"] = [x for x in attrs["review_needed"] if not x.startswith("segmentation_type")]
        attrs["notes"] += f"; split out of a multi-class label: values {e['values']}"
    if e.get("channel") is not None:
        attrs["notes"] += f"; channel {e['channel']} of a multi-channel label"
    info = {"instance": "instance", "semantic": "semantic", "point": "points"}.get(attrs["segmentation_type"], "labels")
    e["label_class"] = label_class
    e["key"] = f"{attrs['provenance'] or 'unknown'}-{slug(label_class).replace('-', '_')}-{info}"
    e["attrs"] = attrs


def cmd_plan(a):
    r = load_yaml(a.record)
    im = r.get("imaging") or {}
    root = Path(a.root).resolve()
    staging = root / "staging" / r["id"]
    stop = native_reasons(r) if not a.organism else [x for x in native_reasons(r) if "organism" not in x]
    arrays = (r.get("technical") or {}).get("arrays") or []
    org = a.organism or (im.get("organism") or ["unknown"])[0]
    name = slug(a.name or default_name(r))
    dataset_dir = root / "data" / f"{family(im.get('modality') or [])}-{ORGANISM_SHORT.get(org) or slug(org)}-{name}"
    file_sets = [s.split("|") for s in a.crop_files] or [list((r.get("technical") or {}).get("sample", {}).get("urls") or [])]
    # a label class is needed only for the label arrays these crops actually use: "N=class" names record
    # array N; plain values go, in record order, to the used label arrays not named that way
    used = sorted({i for fs in file_sets for i in match_arrays(fs, arrays)
                   if i is not None and arrays[i].get("role") == "label"})
    named = {int(k): v for k, _, v in (x.partition("=") for x in a.label_class if re.match(r"^\d+=", x))}
    plain = [x for x in a.label_class if not re.match(r"^\d+=", x)]
    cls_by_index = {**dict(zip([i for i in used if i not in named], plain)), **named}
    if len(plain) > len([i for i in used if i not in named]):
        stop.append(f"{len(plain)} --label-class given for {len(used)} label array(s) used by the crops "
                    f"(record arrays {used}); use N=class to name an array")
    spec = {"record": r["id"], "record_path": str(Path(a.record)), "scope": "files" if a.crop_files else "sample-unit",
            "dataset_dir": str(dataset_dir), "staging": str(staging), "crops": [], "notes": [], "review": [],
            "stop": stop}
    if not stop:
        today = datetime.date.today().isoformat()
        voxel = {k: im["voxel_size_nm"][k] for k in "xyz"}
        spec["voxel_size_nm"] = voxel
        spec["raw_attrs"] = {"source_record": r["id"], "dataset_doi": r.get("doi"),
                             "license": (r.get("license") or {}).get("spdx"),
                             "publication": ((r.get("publications") or [{}])[0]).get("doi"), "title": r.get("title")}
        for files, crop in zip(file_sets, assign_crops(dataset_dir, r["id"], file_sets)):
            entries = []
            for u, idx in zip(files, match_arrays(files, arrays)):
                ra = arrays[idx] if idx is not None else {}
                e = {"role": ra.get("role"), "record_array": idx, "url": u, "path": _staged(staging, u),
                     "record_axes": ra.get("axes"), "record_shape": ra.get("shape"), "record_dtype": ra.get("dtype"),
                     "reader": None, "dataset": None, "axes": None, "transforms": [], "orientation": "apply"}
                if ra.get("role") == "label":
                    e["class_hints"] = class_hints(r, u, ra)
                    if cls_by_index.get(idx):
                        set_label_class(e, r, ra, cls_by_index[idx], voxel, today)
                        spec["review"] += [f"{e['key']}: {x}" for x in e["attrs"]["review_needed"]
                                           if f"{e['key']}: {x}" not in spec["review"]]
                    else:
                        e["label_class"], e["key"] = None, None
                        spec["notes"].append(f"{crop.name}: {_member(u)} (record array {idx}) needs a label class: "
                                             f"set {crop.name} <index> label_class=… reason=…; hints from the file "
                                             f"path and the record text: {e['class_hints'] or 'none'}")
                if idx is None:
                    spec["notes"].append(f"{crop.name}: {_member(u)} matches no single record array: set its role")
                entries.append(e)
            if sum(e["role"] == "raw" for e in entries) > 1:
                spec["notes"].append(f"{crop.name}: several raw files; set role=… so exactly one is raw")
            sample = Path(_member(next((e["url"] for e in entries if e["role"] == "raw"), files[0]))).stem
            spec["crops"].append({
                "crop": str(crop), "sample": "sample-unit" if not a.crop_files else sample, "files": files,
                "staged_files": sorted({e["path"] for e in entries}), "arrays": entries, "decisions": [],
                "manifest_entry": {"record": r["id"], "sample": "sample-unit" if not a.crop_files else sample,
                                   "files": files, "labels": [e["key"] for e in entries if e.get("key")],
                                   "converted": today, "converter": "native"}})
    _save(spec, a.out)
    print(json.dumps({"spec": a.out, "dataset_dir": str(dataset_dir), "crops": len(spec["crops"]), "stop": stop,
                      "notes": spec["notes"], "review": spec["review"]}, indent=2))


def _save(spec, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(spec, indent=2) + "\n")


def _load(path):
    return json.loads(Path(path).read_text())


def _crops(spec, name):
    cs = [c for c in spec["crops"] if not name or Path(c["crop"]).name == name]
    if not cs:
        sys.exit(f"no crop {name!r} in the spec ({[Path(c['crop']).name for c in spec['crops']]})")
    return cs


# ---------- fetch ----------
def _session():
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _get(s, url, **kw):
    time.sleep(1.0)                                           # <= 1 request/second per host
    r = s.get(url, timeout=120, allow_redirects=True, **kw)
    r.raise_for_status()
    return r


def fetch_one(s, spec_url, dest):
    """Download a file, or one zip member by range requests (stored or deflated), to `dest`."""
    from tools.peek_archive import zip_entries
    from tools.probe import http_url
    url, _, member = spec_url.partition("::")
    url = http_url(url)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    if member:
        from urllib.parse import unquote
        entries = {e["name"]: e for e in zip_entries(url)[1]}
        e = entries.get(member) or entries.get(unquote(member))
        if e is None:
            raise FileNotFoundError(f"{member} not in {url}")
        if dest.exists() and dest.stat().st_size == e["size"]:
            return {"file": str(dest), "bytes": e["size"], "status": "already there"}
        head = _get(s, url, headers={"Range": f"bytes={e['offset']}-{e['offset'] + 29}"}).content
        n, m = int.from_bytes(head[26:28], "little"), int.from_bytes(head[28:30], "little")
        start = e["offset"] + 30 + n + m
        if e["csize"] == 0:
            part.write_bytes(b"")
        else:
            r = _get(s, url, headers={"Range": f"bytes={start}-{start + e['csize'] - 1}"}, stream=True)
            d = zlib.decompressobj(-15) if e["method"] == 8 else None
            if e["method"] not in (0, 8):
                raise ValueError(f"zip compression method {e['method']} not supported")
            with open(part, "wb") as f:
                for blk in r.iter_content(CHUNK_BYTES):
                    f.write(d.decompress(blk) if d else blk)
                if d:
                    f.write(d.flush())
        if part.stat().st_size != e["size"]:
            raise IOError(f"{member}: got {part.stat().st_size} bytes, the zip says {e['size']}")
    else:
        r = _get(s, url, stream=True)
        size = int(r.headers.get("Content-Length") or 0)
        if dest.exists() and size and dest.stat().st_size == size:
            r.close()
            return {"file": str(dest), "bytes": size, "status": "already there"}
        with open(part, "wb") as f:
            for blk in r.iter_content(CHUNK_BYTES):
                f.write(blk)
        if size and part.stat().st_size != size:
            raise IOError(f"{url}: got {part.stat().st_size} bytes of {size}")
    part.replace(dest)
    return {"file": str(dest), "bytes": dest.stat().st_size, "status": "downloaded"}


def cmd_fetch(a):
    spec = _load(a.spec)
    s = _session()
    for c in _crops(spec, a.crop):
        for e in c["arrays"]:
            try:
                print(json.dumps({"crop": Path(c["crop"]).name, **fetch_one(s, e["url"], e["path"])}))
            except Exception as ex:  # noqa: BLE001
                print(json.dumps({"crop": Path(c["crop"]).name, "file": e["path"], "status": "error", "error": str(ex)}))
                sys.exit(1)


# ---------- inspect / set ----------
def cmd_inspect(a):
    import numpy as np
    from tools.native_io import guess_reader, info, read
    spec = _load(a.spec)
    for c in _crops(spec, a.crop):
        for i, e in enumerate(c["arrays"]):
            out = {"crop": Path(c["crop"]).name, "index": i, "role": e["role"], "key": e.get("key"),
                   "file": e["path"], "record_axes": e.get("record_axes"), "record_shape": e.get("record_shape"),
                   "record_dtype": e.get("record_dtype")}
            try:
                out["info"] = info(e["path"], e.get("reader") or guess_reader(e["path"]), e.get("dataset"))
                if "datasets" not in out["info"] or e.get("dataset"):
                    arr, _ = read(e["path"], e.get("reader"), e.get("dataset"))
                    sub = arr[tuple(slice(None, None, max(1, n // 64)) for n in arr.shape)]
                    out["stats"] = {"shape": list(arr.shape), "dtype": str(arr.dtype),
                                    "min": float(sub.min()), "max": float(sub.max())}
                    if np.issubdtype(arr.dtype, np.integer):
                        out["stats"]["distinct_values_in_subsample"] = int(len(np.unique(sub)))
                    ax = (out["info"].get("axes_in_file") or "").replace("s", "c")
                    if e["role"] == "label" and "c" in ax and len(ax) == arr.ndim and arr.shape[ax.index("c")] > 1:
                        # per channel: foreground, and how much of it lies inside each other channel (a nucleus
                        # mask sits inside its cell mask), so the channel -> class assignment can rest on the data
                        ch = [np.take(sub, k, axis=ax.index("c")) > 0 for k in range(arr.shape[ax.index("c")])]
                        out["stats"]["label_channels"] = [
                            {"channel": k, "fg_fraction": round(float(m.mean()), 4),
                             "inside": {f"c{j}": round(float((m & o).sum() / max(int(m.sum()), 1)), 3)
                                        for j, o in enumerate(ch) if j != k}} for k, m in enumerate(ch)]
                    spp = out["info"].get("samples_per_pixel") or 1
                    if spp > 1 and arr.shape[-1] == spp:      # grey stored as RGB: every sample the same?
                        out["stats"]["samples_identical"] = bool(all(np.array_equal(sub[..., 0], sub[..., k])
                                                                     for k in range(1, spp)))
                    del arr
            except Exception as ex:  # noqa: BLE001
                out["error"] = str(ex)
            print(json.dumps(out, default=str))
    print(json.dumps({"voxel_size_nm_record": spec.get("voxel_size_nm")}))


def cmd_set(a):
    spec = _load(a.spec)
    c = _crops(spec, a.crop)[0]
    sel = a.array
    if sel.isdigit():
        targets = [c["arrays"][int(sel)]]
    else:
        targets = [e for e in c["arrays"] if e.get("key") == sel or (sel in ("raw", "label") and e["role"] == sel)]
    if len(targets) != 1:
        sys.exit(f"{sel!r} selects {len(targets)} arrays; use the index from `inspect`")
    e = targets[0]
    kv = dict(x.split("=", 1) for x in a.changes if "=" in x)
    reason = kv.pop("reason", None)
    changes = {}
    for k, v in kv.items():
        if k in ("axes", "reader", "dataset", "dtype", "role"):
            e[k] = v or None
            if k == "role" and v == "label" and not e.get("key"):
                e.setdefault("label_class", None)
                e.setdefault("key", None)
        elif k == "orientation":
            if v not in ("apply", "ignore"):
                sys.exit("orientation=apply|ignore")
            if v == "ignore" and not reason:
                sys.exit("orientation=ignore needs reason=… (what says the stored orientation is right)")
            e[k] = v
        elif k == "transform":
            if not reason:
                sys.exit("a transform needs reason=… (the file metadata or documentation that calls for it)")
            parts = v.split(":")
            t = {"op": parts[0], "axis": parts[1], "reason": reason}
            if parts[0] == "select":
                t["index"] = int(parts[2])
            e["transforms"].append(t)
        elif k == "clear" and v == "transforms":
            e["transforms"] = []
        elif k == "label_class":
            if not reason:
                sys.exit("label_class needs reason=… (where the record or the file names say what is annotated)")
            if e["role"] != "label":
                sys.exit("label_class is for label arrays (set role=label first)")
            r, ra = _record_array(spec, e)
            set_label_class(e, r, ra, v, spec["voxel_size_nm"], datetime.date.today().isoformat())
        else:
            sys.exit(f"unknown setting {k!r}")
        changes[k] = v
    _log(c, e, changes, reason)
    _save(spec, a.spec)
    print(json.dumps({"crop": Path(c["crop"]).name, "array": c["arrays"].index(e), **{k: e.get(k) for k in
                      ("role", "label_class", "key", "axes", "reader", "dataset", "dtype", "orientation",
                       "values", "transforms")}}))


def _record_array(spec, e):
    r = load_yaml(spec["record_path"])
    arrays = (r.get("technical") or {}).get("arrays") or []
    return r, (arrays[e["record_array"]] if e.get("record_array") is not None else {})


def _log(c, e, changes, reason):
    c["decisions"].append({"array": c["arrays"].index(e), "changes": changes, "reason": reason,
                           "at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")})
    c["manifest_entry"]["labels"] = [x["key"] for x in c["arrays"] if x["role"] == "label" and x.get("key")]


def cmd_split(a):
    """One multi-class label file -> one label entry per class (a mask of the given values), each written
    to its own labels/ folder. The value -> class mapping must come from the record or the dataset's docs."""
    spec = _load(a.spec)
    c = _crops(spec, a.crop)[0]
    e = c["arrays"][int(a.index)]
    if e["role"] != "label":
        sys.exit(f"array {a.index} is not a label")
    if not a.reason:
        sys.exit("split needs --reason (where the value -> class mapping is documented)")
    groups = {}
    for m in a.mapping:
        val, _, cls = m.partition("=")
        if cls and re.fullmatch(r"c\d+", val):          # one channel of a multi-channel label = one class
            groups[cls] = {"channel": int(val[1:])}
            continue
        if not cls or not all(x.strip().lstrip("-").isdigit() for x in val.split(",")):
            sys.exit(f"{m!r}: use VALUE=class, VALUE,VALUE=class or cN=class (channel N)")
        groups.setdefault(cls, {"values": []})["values"].extend(int(x) for x in val.split(","))
    r, ra = _record_array(spec, e)
    pos = c["arrays"].index(e)
    new = []
    for cls, sel in groups.items():
        x = {**{k: v for k, v in e.items() if k not in ("key", "attrs", "label_class")},
             **({"values": sorted(sel["values"])} if "values" in sel else sel)}
        set_label_class(x, r, ra, cls, spec["voxel_size_nm"], datetime.date.today().isoformat())
        new.append(x)
    c["arrays"][pos:pos + 1] = new
    _log(c, new[0], {"split": groups}, a.reason)
    _save(spec, a.spec)
    print(json.dumps({"crop": Path(c["crop"]).name, "split": [{"index": pos + i, "key": x["key"], "values": x.get("values"),
                                                               "channel": x.get("channel")}
                                                              for i, x in enumerate(new)]}))


# ---------- convert / check ----------
def cmd_convert(a):
    from tools.native_io import prepare, write_crop_metadata, write_multiscale
    spec = _load(a.spec)
    for c in _crops(spec, a.crop):
        crop = Path(c["crop"])
        raws = [e for e in c["arrays"] if e["role"] == "raw"]
        labels = [e for e in c["arrays"] if e["role"] == "label"]
        if len(raws) != 1 or any(e["role"] not in ("raw", "label") for e in c["arrays"]):
            sys.exit(f"{crop.name}: needs exactly one raw and every array's role set")
        if missing := [c["arrays"].index(e) for e in labels if not e.get("key")]:
            sys.exit(f"{crop.name}: label array(s) {missing} have no label class (set … label_class=… reason=…)")
        if len({e["key"] for e in labels}) != len(labels):
            sys.exit(f"{crop.name}: two labels would share the folder name {[e['key'] for e in labels]}")
        if crop.exists():
            shutil.rmtree(crop)                     # a crop is always written whole
        report = {"crop": str(crop), "converter": "tools/native.py", "arrays": []}
        arr, rep = prepare(raws[0], "image")
        levels = write_multiscale(crop / "raw", arr, rep["axes"], spec["voxel_size_nm"], "image", spec["raw_attrs"])
        report["arrays"].append({"group": "raw", **rep, "levels": levels})
        raw_axes, raw_zyx = rep["axes"], rep["shape"][-3:]
        del arr
        for e in labels:
            lab, rep = prepare(e, "label")
            if rep["shape"][-3:] != raw_zyx:
                rep["warning"] = f"label z/y/x {rep['shape']} differs from raw {raw_zyx}"
            lv = write_multiscale(crop / "labels" / e["key"], lab, "zyx", spec["voxel_size_nm"], "label", e["attrs"])
            report["arrays"].append({"group": f"labels/{e['key']}", **rep, "levels": lv})
            del lab
        write_crop_metadata(crop, raw_axes, levels, [e["key"] for e in labels])
        report["decisions"] = c["decisions"]
        (crop / "conversion.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"crop": crop.name, "status": "success",
                          "arrays": [{k: x[k] for k in ("group", "shape", "dtype", "done")} for x in report["arrays"]]}))


def cmd_check(a):
    from tools.native_qc import overlay, verify
    spec = _load(a.spec)
    for c in _crops(spec, a.crop):
        v = verify(spec, c)
        images = overlay(spec, c)
        print(json.dumps({"crop": Path(c["crop"]).name, "overall": v["overall"],
                          "checks": {x["name"]: f"{x['status']}: {x['detail']}" for x in v["checks"]},
                          "overlays": images}, indent=1))


# ---------- review / finalize ----------
def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16]


def cmd_review(a):
    spec = _load(a.spec)
    c = _crops(spec, a.crop)[0]
    qc = Path(c["crop"]) / "qc"
    images = sorted(qc.glob("overlay-*.png"))
    conv = Path(c["crop"]) / "conversion.json"
    if not images or not conv.exists() or min(p.stat().st_mtime for p in images) < conv.stat().st_mtime:
        sys.exit("run `check` first: the overlays must be newer than the conversion")
    out = {"verdict": a.verdict, "reason": a.reason, "saw": a.saw, "reviewer": "claude (visual review of the overlays)",
           "images": {p.name: _sha(p) for p in images},
           "at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    (qc / "review.json").write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({"crop": Path(c["crop"]).name, "review": a.verdict}))


def crop_status(c):
    """(ok, reason): verification passed (or unverified only on alignment) and the visual review says pass."""
    crop = Path(c["crop"])
    vf, rf = crop / "verification.json", crop / "qc" / "review.json"
    if not vf.exists():
        return False, "not verified"
    v = json.loads(vf.read_text())
    if v["overall"] == "fail":
        return False, f"verify failed: {v['failures']}"
    if v["overall"] == "unverified" and any(not x.startswith("alignment:") for x in v["unverified"]):
        return False, f"verify unverified: {v['unverified']}"
    if not rf.exists():
        return False, "no visual review"
    rv = json.loads(rf.read_text())
    if rv["verdict"] != "pass":
        return False, f"visual review {rv['verdict']}: {rv['reason']}"
    if any(_sha(qc) != h for qc in (crop / "qc").glob("overlay-*.png") for n, h in rv["images"].items() if qc.name == n):
        return False, "overlays changed after the review"
    return True, "pass" if v["overall"] == "pass" else "alignment unverified numerically, visual review pass"


def cmd_finalize(a):
    from tools.miao_run import finish
    spec = _load(a.spec)
    summary = {"dataset_dir": spec["dataset_dir"], "crops": len(spec["crops"]), "done": [], "failed": {}}
    for c in spec["crops"]:
        ok, why = crop_status(c)
        name = Path(c["crop"]).name
        if ok:
            finish(spec, c, a.keep_source)
            summary["done"].append(name)
        else:
            summary["failed"][name] = why
    line = json.dumps(summary)
    if a.summary_out:
        Path(a.summary_out).write_text(line + "\n")
    print(line)
    sys.exit(1 if summary["failed"] else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("record")
    p.add_argument("--root", default="demo-native")
    p.add_argument("--label-class", action="append", default=[])
    p.add_argument("--name")
    p.add_argument("--organism")
    p.add_argument("--crop-files", action="append", default=[], help='"url1|url2": one crop (repeat for more)')
    p.add_argument("--out", required=True)
    for n in ("fetch", "inspect", "convert", "check"):
        q = sub.add_parser(n)
        q.add_argument("spec")
        q.add_argument("--crop")
    q = sub.add_parser("set")
    q.add_argument("spec")
    q.add_argument("crop")
    q.add_argument("array")
    q.add_argument("changes", nargs="+")
    q = sub.add_parser("split")
    q.add_argument("spec")
    q.add_argument("crop")
    q.add_argument("index", help="the label's index (from inspect)")
    q.add_argument("mapping", nargs="+", help="VALUE=class, VALUE,VALUE=class or cN=class (channel N), from the "
                                              "record, the file's channel names or the dataset's docs")
    q.add_argument("--reason", required=True)
    q = sub.add_parser("review")
    q.add_argument("spec")
    q.add_argument("crop")
    q.add_argument("--verdict", required=True, choices=["pass", "fail", "unsure"])
    q.add_argument("--reason", required=True)
    q.add_argument("--saw", required=True, help="what the overlays show, per image")
    q = sub.add_parser("finalize")
    q.add_argument("spec")
    q.add_argument("--keep-source", action="store_true")
    q.add_argument("--summary-out")
    a = ap.parse_args()
    {"plan": cmd_plan, "fetch": cmd_fetch, "inspect": cmd_inspect, "set": cmd_set, "convert": cmd_convert,
     "check": cmd_check, "split": cmd_split, "review": cmd_review, "finalize": cmd_finalize}[a.cmd](a)


if __name__ == "__main__":
    main()
