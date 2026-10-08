"""Numeric verification and overlay images for the native download agent (tools/native.py verify / overlay).

verify: is the written crop what the source says, and do the labels sit on the image?
  structure      raw/ and every label group exist, every level is listed
  voxel_size     s0 scale equals the record's voxel size
  identity:*     s0 equals the source read again with the same axes and transforms (whole array up to 1 GB,
                 else 8 slices spread through it)
  pyramid:*      each level has the shape its factors give, and a block of it equals the block recomputed
                 from the level above (mean for raw, mode for labels)
  grid:*         label z/y/x shape equals the raw z/y/x shape
  labels:*       integer dtype, number of IDs, foreground fraction plausible (0.05 % to 98 %)
  alignment:*    how well the labels fit the image as stored, against the same labels flipped on each axis,
                 rotated by 180°, transposed in y/x, and shifted by up to 10 % per axis. Two scores: `interior`
                 (difference of mean intensity inside the labels vs a 3-voxel ring around them, in standard
                 deviations: filled objects such as nuclei; the ring keeps unlabelled neighbours out of it) and `boundary` (image gradient on label boundaries vs everywhere:
                 membranes, neurites). It fails when a wrong transform or shift fits clearly better (gain >= 1.3
                 on that score, and the other score not more than 10 % worse), and is `unverified` when neither
                 score says much or a wrong transform fits about as well (the visual review then decides).
Each check is pass, fail or unverified; overall is fail if any failed, unverified if any could not decide,
else pass. Written to <crop>/verification.json (same shape as TensorSwitch's) and <crop>/qc/metrics.json.

overlay: <crop>/qc/overlay-xy.png (xy slices at 15/35/50/65/85 % of z) and <crop>/qc/overlay-ortho.png (xz and
yz through the middle, stretched to physical aspect), each with three columns: raw, raw + label outlines,
and raw + the labels flipped in y (wrong on purpose, as a reference for what misalignment looks like).
"""
import datetime
import json
from pathlib import Path

import numpy as np

from tools.native_io import downsample, level_factors, prepare

GAIN_FAIL = 1.3          # a wrong transform this much better than as-stored: misaligned
AMBIGUOUS = 0.9          # a wrong transform within 10 % of as-stored: the numbers can't decide
WEAK_INTERIOR = 0.25     # interior separation (in SD) below this and ...
WEAK_BOUNDARY = 1.15     # ... boundary ratio below this: the image doesn't tell
FG_RANGE = (0.0005, 0.98)
IDENTITY_WHOLE_BYTES = 1024 ** 3


def _now():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _group_levels(group):
    meta = json.loads((group / "zarr.json").read_text())
    ms = meta["attributes"]["ome"]["multiscales"][0]
    return meta, [a["name"] for a in ms["axes"]], ms["datasets"]


def _open(group, path):
    import zarr
    return zarr.open(str(group / path), mode="r")


def _pick_level(group, datasets, max_voxels):
    for ds in datasets:
        a = _open(group, ds["path"])
        if int(np.prod(a.shape[-3:])) <= max_voxels:
            return ds["path"], a
    return datasets[-1]["path"], _open(group, datasets[-1]["path"])


# ---------- alignment scores ----------
def _boundaries(lab):
    """Label boundary voxels, in-plane (y/x neighbours), so anisotropic z doesn't dominate."""
    b = np.zeros(lab.shape, bool)
    b[:, 1:, :] |= lab[:, 1:, :] != lab[:, :-1, :]
    b[:, :, 1:] |= lab[:, :, 1:] != lab[:, :, :-1]
    return b


def _gradient(r):
    gy = np.abs(np.diff(r, axis=1, append=r[:, -1:, :]))
    gx = np.abs(np.diff(r, axis=2, append=r[:, :, -1:]))
    return gy + gx


SHELL = 3                # voxels (in-plane, at the analysis level): the ring the labels are compared with


def _shell(m, k=SHELL):
    """The ring of voxels within k (y/x) of the labels, outside them."""
    d = m.copy()
    for _ in range(k):
        e = d.copy()
        e[:, 1:, :] |= d[:, :-1, :]
        e[:, :-1, :] |= d[:, 1:, :]
        e[:, :, 1:] |= d[:, :, :-1]
        e[:, :, :-1] |= d[:, :, 1:]
        d = e
    return d & ~m


def _scores(r, g, lab):
    m = lab > 0
    fg = m.mean()
    out = {"fg_fraction": float(fg)}
    if 0 < fg < 1:
        sd = float(r.std()) or 1.0
        # inside vs the ring just around the labels, not all the background: an outline on its structure has an
        # edge right there, while unlabelled bright neighbours (sparse labels) don't count against it
        ring = _shell(m)
        out["interior"] = float(abs(r[m].mean() - r[ring if ring.any() else ~m].mean()) / sd)
        b = _boundaries(lab)
        out["boundary"] = float(g[b].mean() / max(float(g.mean()), 1e-9)) if b.any() else None
    else:
        out["interior"], out["boundary"] = None, None
    return out


def _candidates(lab):
    c = {"flip z": lab[::-1], "flip y": lab[:, ::-1], "flip x": lab[:, :, ::-1], "rotate 180 (y+x)": lab[:, ::-1, ::-1]}
    if lab.shape[1] == lab.shape[2]:
        c["transpose y/x"] = lab.transpose(0, 2, 1)
    return c


def _best_shift(r, lab, primary):
    """Shift of the labels (z, y, x voxels at this level) that fits best, by FFT cross-correlation, within 10 %."""
    if primary == "interior":
        a, b = r - r.mean(), (lab > 0).astype(np.float32)
    else:
        a, b = _gradient(r), _boundaries(lab).astype(np.float32)
        a = a - a.mean()
    b = b - b.mean()
    corr = np.fft.irfftn(np.fft.rfftn(a) * np.conj(np.fft.rfftn(b)), s=a.shape)
    sign = 1.0 if corr[0, 0, 0] >= 0 else -1.0
    corr *= sign
    allowed = []                                   # shifts within 10 % of each axis (both directions)
    for n in a.shape:
        ok, lim = np.zeros(n, bool), max(1, int(n * 0.1))
        ok[:lim + 1] = True
        ok[n - lim:] = True
        allowed.append(ok)
    mask = allowed[0][:, None, None] & allowed[1][None, :, None] & allowed[2][None, None, :]
    c = np.where(mask, corr, -np.inf)
    k = np.unravel_index(int(np.argmax(c)), c.shape)
    shift = [int(v if v <= n // 2 else v - n) for v, n in zip(k, a.shape)]
    return shift, float(c[k] / corr[0, 0, 0]) if corr[0, 0, 0] > 0 else None


def alignment(raw_group, label_group):
    """Scores as stored and for every wrong-on-purpose transform; per raw channel, best channel reported."""
    _, raxes, rds = _group_levels(raw_group)
    _, _, lds = _group_levels(label_group)
    rpath, ra = _pick_level(raw_group, rds, 32 * 1024 ** 2)
    lab = np.asarray(_open(label_group, rpath)[...]) if (label_group / rpath).exists() else None
    if lab is None:
        return {"status": "unverified", "detail": f"no label level {rpath} to compare with raw {rpath}"}
    raw = np.asarray(ra[...]).astype(np.float32)
    chans = [raw[i] for i in range(raw.shape[0])] if "c" in raxes else [raw]
    if chans[0].shape != lab.shape:
        return {"status": "fail", "detail": f"raw {chans[0].shape} and labels {lab.shape} differ at {rpath}"}
    per = []
    for ci, r in enumerate(chans):
        g = _gradient(r)
        res = {"channel": ci, "as_stored": _scores(r, g, lab)}
        res["transforms"] = {k: _scores(r, g, v) for k, v in _candidates(lab).items()}
        per.append(res)
    fg = per[0]["as_stored"]["fg_fraction"]
    primary = "boundary" if fg > 0.6 else "interior"

    secondary = "interior" if primary == "boundary" else "boundary"

    def val(s, k=primary):
        return s.get(k) or 0.0

    def better(alt, ref):
        """(gain, counts): `alt` fits clearly better on the primary score without losing > 10 % on the other."""
        g = val(alt) / val(ref) if val(ref) > 0 else None
        keeps = val(ref, secondary) == 0 or val(alt, secondary) >= 0.9 * val(ref, secondary)
        return g, bool(g is not None and g >= GAIN_FAIL and keeps)
    best = max(per, key=lambda p: val(p["as_stored"]))
    base = val(best["as_stored"])
    alt_name, alt = max(best["transforms"].items(), key=lambda kv: val(kv[1]))
    gain, flipped = better(alt, best["as_stored"])
    # shift search on a smaller level; the shift found is then scored like the transforms
    spath, sa = _pick_level(raw_group, rds, 4 * 1024 ** 2)
    sraw = np.asarray(sa[...]).astype(np.float32)
    sraw = sraw[best["channel"]] if "c" in raxes else sraw
    slab = np.asarray(_open(label_group, spath)[...]) if (label_group / spath).exists() else None
    shift, shift_gain, shifted = None, None, False
    if slab is not None and slab.shape == sraw.shape:
        shift, _ = _best_shift(sraw, slab, primary)
        if max(abs(s) for s in shift) >= 2:
            sg = _gradient(sraw)
            shift_gain, shifted = better(_scores(sraw, sg, np.roll(slab, shift, (0, 1, 2))), _scores(sraw, sg, slab))
    weak = (best["as_stored"].get("interior") or 0) < WEAK_INTERIOR and (best["as_stored"].get("boundary") or 0) < WEAK_BOUNDARY
    out = {"level": rpath, "primary_score": primary, "channel": best["channel"], "as_stored": best["as_stored"],
           "best_wrong_transform": alt_name, "best_wrong_score": alt, "gain": gain, "shift_level": spath,
           "best_shift_zyx": shift, "shift_gain": shift_gain, "per_channel": per}
    if flipped:
        out["status"], out["detail"] = "fail", (f"labels fit better after '{alt_name}' ({primary} {val(alt):.2f} vs "
                                                f"{base:.2f} as stored, gain {gain:.2f})")
    elif shifted:
        out["status"], out["detail"] = "fail", (f"labels fit better shifted by {shift} (z, y, x voxels at {spath}; "
                                                f"gain {shift_gain:.2f})")
    elif weak:
        out["status"], out["detail"] = "unverified", (
            f"the image says little about where the labels belong (interior {best['as_stored'].get('interior')}, "
            f"boundary {best['as_stored'].get('boundary')}); the visual review decides")
    elif gain is not None and gain >= AMBIGUOUS:
        out["status"], out["detail"] = "unverified", (
            f"'{alt_name}' fits about as well as stored ({primary} {val(alt):.2f} vs {base:.2f}): the numbers can't "
            "tell them apart; the visual review decides")
    else:
        out["status"], out["detail"] = "pass", (f"best as stored: {primary} {base:.2f}; best wrong transform "
                                                f"'{alt_name}' {val(alt):.2f}; best shift {shift}")
    return out


# ---------- verify ----------
def _check(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def _identity(arr_written, group, path, prepared):
    a = _open(group, path)
    if list(a.shape) != list(prepared.shape) or str(a.dtype) != str(prepared.dtype):
        return "fail", f"shape/dtype {list(a.shape)} {a.dtype} vs source {list(prepared.shape)} {prepared.dtype}"
    if prepared.nbytes <= IDENTITY_WHOLE_BYTES:
        same = np.array_equal(np.asarray(a[...]), prepared)
        return ("pass" if same else "fail"), ("identical to the source in the whole array" if same else
                                              "differs from the source")
    zs = np.linspace(0, prepared.shape[-3] - 1, 8).astype(int)
    for z in zs:
        if not np.array_equal(np.asarray(a[..., z, :, :]), prepared[..., z, :, :]):
            return "fail", f"z slice {z} differs from the source"
    return "pass", f"identical to the source in {len(zs)} z slices"


def _pyramid(group, kind):
    _, axes, ds = _group_levels(group)
    prev = _open(group, ds[0]["path"])
    for d0, d1 in zip(ds, ds[1:]):
        cur = _open(group, d1["path"])
        s0 = d0["coordinateTransformations"][0]["scale"]
        s1 = d1["coordinateTransformations"][0]["scale"]
        f = [int(round(b / a)) for a, b in zip(s0, s1)]
        want = [-(-n // k) for n, k in zip(prev.shape, f)]
        if list(cur.shape) != want or cur.dtype != prev.dtype:
            return "fail", f"{d1['path']}: shape {list(cur.shape)} {cur.dtype}, expected {want} {prev.dtype}"
        blk = [min(n, 64 * k) for n, k in zip(prev.shape, f)]
        src = np.asarray(prev[tuple(slice(0, b) for b in blk)])
        got = np.asarray(cur[tuple(slice(0, -(-b // k)) for b, k in zip(blk, f))])
        if not np.array_equal(downsample(src, f, "mean" if kind == "image" else "mode"), got):
            return "fail", f"{d1['path']}: a block differs from the one recomputed from {d0['path']}"
        prev = cur
    return "pass", f"{len(ds)} level(s), shapes and a recomputed block consistent"


def verify(spec, c):
    crop = Path(c["crop"])
    checks, metrics = [], {}
    raw_spec = next(a for a in c["arrays"] if a["role"] == "raw")
    labels = [a for a in c["arrays"] if a["role"] == "label"]
    raw_group = crop / "raw"
    ok = (raw_group / "zarr.json").exists() and all((crop / "labels" / a["key"] / "zarr.json").exists() for a in labels)
    checks.append(_check("structure", "pass" if ok else "fail",
                         f"raw/ and labels {[a['key'] for a in labels]}" if ok else "raw/ or a label group missing"))
    if not ok:
        return _write_verification(crop, raw_spec, checks, metrics)
    _, raxes, rds = _group_levels(raw_group)
    v = spec["voxel_size_nm"]
    want = [v["z"], v["y"], v["x"]]
    got = rds[0]["coordinateTransformations"][0]["scale"][-3:]
    checks.append(_check("voxel_size", "pass" if np.allclose(got, want) else "fail",
                         f"s0 scale z/y/x {got}, record {want}"))
    for a, group, kind, name in [(raw_spec, raw_group, "image", "raw")] + [
            (x, crop / "labels" / x["key"], "label", "labels/" + x["key"]) for x in labels]:
        prepared, _ = prepare(a, a["role"])
        checks.append(_check(f"identity:{name}", *_identity(None, group, "s0", prepared)))
        del prepared
        checks.append(_check(f"pyramid:{name}", *_pyramid(group, kind)))
    rshape = list(_open(raw_group, "s0").shape[-3:])
    for x in labels:
        g = crop / "labels" / x["key"]
        la = _open(g, "s0")
        same = list(la.shape) == rshape
        checks.append(_check(f"grid:{x['key']}", "pass" if same else "fail",
                             f"labels z/y/x {list(la.shape)}, raw {rshape}"))
        lab = np.asarray(_open(g, _pick_level(g, _group_levels(g)[2], 32 * 1024 ** 2)[0])[...])
        fg = float((lab > 0).mean())
        ids = int(len(np.unique(lab))) - int((lab == 0).any())
        okfg = FG_RANGE[0] <= fg <= FG_RANGE[1]
        checks.append(_check(f"labels:{x['key']}", "pass" if okfg and np.issubdtype(lab.dtype, np.integer) else
                             "unverified", f"{lab.dtype}, {ids} label value(s), foreground {100 * fg:.2f} %"))
        metrics[x["key"]] = {"ids": ids, "fg_fraction": fg}
        if same:
            al = alignment(raw_group, g)
            metrics[x["key"]]["alignment"] = al
            checks.append(_check(f"alignment:{x['key']}", al["status"], al["detail"]))
    return _write_verification(crop, raw_spec, checks, metrics)


def _write_verification(crop, raw_spec, checks, metrics):
    failures = [c["name"] for c in checks if c["status"] == "fail"]
    unverified = [c["name"] for c in checks if c["status"] == "unverified"]
    out = {"output": str(crop), "source": raw_spec.get("path"), "checks": checks, "verified_at": _now(),
           "tool": "tools/native.py verify", "failures": failures, "unverified": unverified,
           "overall": "fail" if failures else "unverified" if unverified else "pass"}
    (crop / "verification.json").write_text(json.dumps(out, indent=2) + "\n")
    (crop / "qc").mkdir(exist_ok=True)
    (crop / "qc" / "metrics.json").write_text(json.dumps(metrics, indent=2, default=float) + "\n")
    return out


# ---------- overlay ----------
def _norm(img, lo, hi):
    return (np.clip((img.astype(np.float32) - lo) / max(hi - lo, 1e-9), 0, 1) * 255).astype(np.uint8)


def _colors(lab):
    ids = lab.astype(np.uint64)
    h = (ids * np.uint64(2654435761)) % np.uint64(2 ** 32)
    rgb = np.stack([(h >> np.uint64(s)) & np.uint64(255) for s in (0, 8, 16)], -1).astype(np.float32)
    return 80 + rgb * (175 / 255)          # bright enough on grey


def _overlay(gray, lab):
    rgb = np.repeat(gray[..., None], 3, -1).astype(np.float32)
    if lab is None:
        return rgb.astype(np.uint8)
    col = _colors(lab)
    fill = lab > 0
    rgb[fill] = 0.75 * rgb[fill] + 0.25 * col[fill]
    b = np.zeros(lab.shape, bool)
    b[1:, :] |= lab[1:, :] != lab[:-1, :]
    b[:, 1:] |= lab[:, 1:] != lab[:, :-1]
    b &= (lab > 0) | np.roll(lab > 0, 1, 0) | np.roll(lab > 0, 1, 1)
    edge_col = np.where(fill[..., None], col, np.array([255, 255, 0], np.float32))
    rgb[b] = edge_col[b]
    return rgb.astype(np.uint8)


def _panel(arr, size, aspect=1.0):
    from PIL import Image
    im = Image.fromarray(arr)
    h, w = arr.shape[:2]
    h = h * aspect
    s = size / max(h, w)
    return im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.NEAREST)


def _sheet(rows, titles, col_titles, path, size=360):
    from PIL import Image, ImageDraw
    pad, head = 6, 18
    cw = size + pad
    rh = [max(p.height for p in r) + head for r in rows]
    sheet = Image.new("RGB", (cw * len(col_titles) + pad, sum(rh) + head + pad), (24, 24, 24))
    d = ImageDraw.Draw(sheet)
    for j, t in enumerate(col_titles):
        d.text((pad + j * cw, 3), t, fill=(255, 255, 255))
    y = head
    for r, t, h in zip(rows, titles, rh):
        d.text((pad, y + 2), t, fill=(200, 200, 200))
        for j, p in enumerate(r):
            sheet.paste(p, (pad + j * cw, y + head))
        y += h
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)


def overlay(spec, c, size=360):
    crop = Path(c["crop"])
    raw_group = crop / "raw"
    _, raxes, rds = _group_levels(raw_group)
    labels = [a for a in c["arrays"] if a["role"] == "label"]
    # the level whose in-plane size is closest to (not below) the panel size
    path = rds[0]["path"]
    for ds in rds:
        if max(_open(raw_group, ds["path"]).shape[-2:]) >= size:
            path = ds["path"]
    raw = np.asarray(_open(raw_group, path)[...])
    metrics = json.loads((crop / "qc" / "metrics.json").read_text()) if (crop / "qc" / "metrics.json").exists() else {}
    out = []
    for x in labels or [None]:
        ch = 0
        if x is not None:
            ch = (metrics.get(x["key"], {}).get("alignment") or {}).get("channel", 0)
        r = raw[ch] if "c" in raxes else raw
        lo, hi = (float(v) for v in np.percentile(r[:: max(1, r.shape[0] // 16)], [1, 99.5]))
        lab = None
        if x is not None:
            g = crop / "labels" / x["key"]
            lab = np.asarray(_open(g, path)[...]) if (g / path).exists() else None
            if lab is not None and lab.shape != r.shape:
                lab = None
        scale = rds[[d["path"] for d in rds].index(path)]["coordinateTransformations"][0]["scale"][-3:]
        aspect = min(8.0, scale[0] / scale[1])
        nz, ny, nx = r.shape
        tag = x["key"] if x else "raw only"
        cols = ["raw", f"raw + {tag}", "labels flipped in y (wrong on purpose)"]
        rows, titles = [], []
        for f in (0.15, 0.35, 0.5, 0.65, 0.85):
            z = min(nz - 1, int(f * nz))
            g = _norm(r[z], lo, hi)
            l2 = lab[z] if lab is not None else None
            rows.append([_panel(np.repeat(g[..., None], 3, -1), size), _panel(_overlay(g, l2), size),
                         _panel(_overlay(g, l2[::-1] if l2 is not None else None), size)])
            titles.append(f"xy, z = {z} of {nz} ({path}, channel {ch})")
        name = "overlay-xy.png" if len(labels) <= 1 else f"overlay-xy-{x['key']}.png"
        _sheet(rows, titles, cols, crop / "qc" / name, size)
        out.append(str(crop / "qc" / name))
        rows, titles = [], []
        for title, g, l2 in [(f"xz at y = {ny // 2}", r[:, ny // 2, :], lab[:, ny // 2, :] if lab is not None else None),
                             (f"yz at x = {nx // 2}", r[:, :, nx // 2], lab[:, :, nx // 2] if lab is not None else None)]:
            gg = _norm(g, lo, hi)
            flipped = None
            if lab is not None:
                flipped = (lab[:, ::-1, :][:, ny // 2, :] if title.startswith("xz") else lab[:, ::-1, nx // 2])
            rows.append([_panel(np.repeat(gg[..., None], 3, -1), size, aspect), _panel(_overlay(gg, l2), size, aspect),
                         _panel(_overlay(gg, flipped), size, aspect)])
            titles.append(f"{title} (z stretched x{aspect:.1f} to physical aspect)")
        name = "overlay-ortho.png" if len(labels) <= 1 else f"overlay-ortho-{x['key']}.png"
        _sheet(rows, titles, cols, crop / "qc" / name, size)
        out.append(str(crop / "qc" / name))
    return out


def level_plan(voxel_xyz, shape_zyx):
    """For the plan's report: the levels a crop of this shape gets."""
    return [{"factors": list(f), "voxel_zyx": list(v)} for f, v in
            level_factors((voxel_xyz["z"], voxel_xyz["y"], voxel_xyz["x"]), shape_zyx)]
