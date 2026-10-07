"""Thumbnails of the datasets downloaded into demo/data/ (the miao layout), for the dashboard gallery.

    python tools/demo_gallery.py [--data demo/data]   # prints the gallery entries (without images)

One entry per dataset folder: a JPEG thumbnail of the middle slice of crop-001's raw array
(label instances drawn as colored outlines when the crop has labels), the number of crops, the
catalog record it came from and a Fileglancer link to the folder. demo/ is git-ignored, so the
gallery is empty wherever demo/data/ doesn't exist (e.g. the GitHub Pages build).
"""
import argparse
import base64
import io
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import ROOT  # noqa: E402

FILEGLANCER = "https://fileglancer.int.janelia.org/browse/"
THUMB_PX = 256
CHANNEL_AXES = {"c", "s"}


def fileglancer_url(path: Path) -> str:
    """/groups/<lab>/home/<user>/... -> <FILEGLANCER>groups_<lab>_home/<user>/..."""
    parts = path.resolve().parts[1:]
    if len(parts) >= 3 and parts[0] == "groups" and parts[2] == "home":
        return FILEGLANCER + "/".join([f"groups_{parts[1]}_home", *parts[3:]])
    return FILEGLANCER + "/".join(parts)


def _axes(group):
    return [a["name"] for a in group.attrs["ome"]["multiscales"][0]["axes"]]


def _slice2d(group, keep_channels: bool):
    """Middle slice of the full-resolution array: (y, x) or (y, x, 3)."""
    arr = group[group.attrs["ome"]["multiscales"][0]["datasets"][0]["path"]]
    axes = _axes(group)
    if len(axes) != arr.ndim:
        axes = ["?"] * (arr.ndim - 2) + ["y", "x"]
    idx, chan = [], None
    for name, n in zip(axes, arr.shape):
        if name in ("y", "x"):
            idx.append(slice(None))
        elif keep_channels and name in CHANNEL_AXES and chan is None:
            chan = name
            idx.append(slice(0, min(n, 3)))
        else:
            idx.append(n // 2)
    img = np.asarray(arr[tuple(idx)], dtype=np.float32)
    if chan is not None:
        img = np.moveaxis(img, 0, -1)
        if img.shape[-1] == 2:
            img = np.concatenate([img, np.zeros_like(img[..., :1])], axis=-1)
        elif img.shape[-1] == 1:
            img = img[..., 0]
    return img


def _normalize(img):
    lo, hi = np.percentile(img, [1, 99.5])
    return np.clip((img - lo) / max(hi - lo, 1e-6), 0, 1)


def _resize(img, shape, nearest=False):
    from PIL import Image
    h, w = shape
    if nearest:
        ys = (np.arange(h) * img.shape[0] / h).astype(int)
        xs = (np.arange(w) * img.shape[1] / w).astype(int)
        return img[ys][:, xs]
    chans = [img] if img.ndim == 2 else [img[..., i] for i in range(img.shape[-1])]
    out = [np.asarray(Image.fromarray(c.astype(np.float32), mode="F").resize((w, h), Image.BILINEAR)) for c in chans]
    return out[0] if img.ndim == 2 else np.stack(out, -1)


def thumbnail(crop: Path):
    """JPEG bytes and a short description of the crop's raw array."""
    import zarr
    from PIL import Image
    g = zarr.open_group(str(crop), mode="r")
    raw = g["raw"]
    s0 = raw[raw.attrs["ome"]["multiscales"][0]["datasets"][0]["path"]]
    img = _normalize(_slice2d(raw, keep_channels=True))
    scale = THUMB_PX / max(img.shape[:2])
    shape = (max(1, round(img.shape[0] * scale)), max(1, round(img.shape[1] * scale)))
    img = _resize(img, shape)
    rgb = np.repeat(img[..., None], 3, -1) if img.ndim == 2 else img
    labels = list(g["labels"].group_keys()) if "labels" in g else []
    if labels:
        lab = _resize(_slice2d(g["labels"][labels[0]], keep_channels=False).astype(np.int64), shape, nearest=True)
        edge = np.zeros(lab.shape, bool)
        edge[:-1] |= lab[:-1] != lab[1:]
        edge[:, :-1] |= lab[:, :-1] != lab[:, 1:]
        edge &= lab > 0
        ids = lab[edge]
        hue = (ids * 0.618033988749895) % 1.0
        colors = np.stack([np.abs(hue * 6 - 3) - 1, 2 - np.abs(hue * 6 - 2), 2 - np.abs(hue * 6 - 4)], -1).clip(0, 1)
        rgb = rgb.copy()
        rgb[edge] = 0.25 * rgb[edge] + 0.75 * colors
    buf = io.BytesIO()
    Image.fromarray((rgb * 255).astype(np.uint8)).save(buf, "JPEG", quality=82)
    return buf.getvalue(), {"axes": "".join(_axes(raw)), "shape": list(s0.shape), "dtype": str(s0.dtype), "labels": labels}


def _record_id(folder: Path, crop: Path):
    manifest = folder / "manifest.json"
    if manifest.exists():
        entry = json.loads(manifest.read_text()).get(crop.name) or {}
        if entry.get("record"):
            return entry["record"]
    ver = crop / "verification.json"
    if ver.exists():
        src = Path(json.loads(ver.read_text()).get("source") or "")
        if "staging" in src.parts:
            return src.parts[src.parts.index("staging") + 1]
    return None


def gallery(data_dir: Path = ROOT / "demo" / "data", images: bool = True):
    entries = []
    if not data_dir.is_dir():
        return entries
    for folder in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        crops = sorted(folder.glob("crop-*.zarr"))
        if not crops:
            continue
        entry = {"name": folder.name, "crops": len(crops), "record": _record_id(folder, crops[0]),
                 "url": fileglancer_url(folder)}
        try:
            jpeg, info = thumbnail(crops[0])
            entry.update(info)
            if images:
                entry["thumb"] = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        except Exception as e:  # a crop still being written, or an unreadable array: list it without a picture
            print(f"note: no thumbnail for {folder.name} ({e})", file=sys.stderr)
        entries.append(entry)
    return entries


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "demo" / "data"))
    a = ap.parse_args()
    print(json.dumps(gallery(Path(a.data), images=False), indent=1))
