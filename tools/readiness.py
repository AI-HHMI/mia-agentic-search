"""Download readiness: can /download-dataset convert this record into the miao layout unattended?

    python tools/readiness.py [<file> ...] [--tensorswitch SRC]   # one line per record (default: all records)
    python tools/readiness.py [<file> ...] --summary              # status counts and a tally of reasons
    python tools/readiness.py <file> --json                       # one JSON object per record

The planner comes from --tensorswitch or $TENSORSWITCH_SRC (TensorSwitch src/, unified branch).
One rule for the `download-ready` / `download-not-ready` labels (tools/labels.py, the auto-merge gate),
the download queue (tools/download_queue.py), tools/miao_layout.py and the enricher (step 5a).
What TensorSwitch's record planner checks (technical arrays, sample URLs, untrusted headers without a
voxel size, TIFF axes, HDF5 dataset names) comes from its plan; this adds what the miao layout needs:
  - data.access is open (an unattended run can't register), and the record is 3D;
  - imaging.voxel_size_nm has x, y and z (the label metadata needs it), one organism, one modality family;
  - sample URLs on TensorSwitch's fetch allowlist; array roles raw / label only (miao#13);
  - labels on the raw grid at offset 0 (alignment not offset / cropped / scaled / transformed);
  - planner status `ready`, or `partial` when a raw array converts and every dropped array is a label
    with no matching sample file or a table / points label (those crops are raw-only: a `note:` says so);
  - no array the planner describes as 2D (a single slice, not a volume).
Without the planner the answer is `unknown`, never `ready`. Read-only; nothing is downloaded.
`whole_download_ok`: the queue downloads a ready record whole when its size is confirmed by a complete
file listing and below WHOLE_DOWNLOAD_MAX_BYTES (50 GB); otherwise its sample unit.
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import types
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import CONFIRMED_SIZE_SOURCES, ROOT, WHOLE_DOWNLOAD_MAX_BYTES, load_yaml  # noqa: E402
from tools.probe import SCHEMES, allowed_hosts  # noqa: E402
from tools.labels import voxel_size_found  # noqa: E402

FAMILIES = {
    "em": {"FIB-SEM", "SBF-SEM", "ssTEM", "ssSEM", "TEM", "cryo-EM", "cryo-ET"},
    "lm": {"confocal", "spinning-disk", "widefield", "light-sheet", "two-photon", "super-resolution", "brightfield",
           "histology-WSI", "phase-contrast", "DIC", "fluorescence-other"},
    "xray": {"X-ray"},
    "exm": {"expansion"},
}
_PLANNER = {}


def family(modalities):
    """The miao folder family: of the first modality, else the only one listed; None if unclear."""
    fams = [{f for m in ms for f, members in FAMILIES.items() if m in members} for ms in (modalities[:1], modalities)]
    return next((next(iter(f)) for f in fams if len(f) == 1), None)


def _host_ok(url):
    p = urlsplit(url.split("::", 1)[0])
    host = (p.hostname or "").lower()
    return p.scheme == "s3" or (p.scheme in SCHEMES and any(host == h or host.endswith("." + h) for h in allowed_hosts()))


def _load_planner(src):
    """TensorSwitch's record_planner, loaded by file so its package (tensorstore etc.) isn't imported."""
    base = Path(src).resolve() / "tensorswitch_v2"
    for name, path in (("tensorswitch_v2", base), ("tensorswitch_v2.utils", base / "utils")):
        mod = sys.modules.setdefault(name, types.ModuleType(name))
        mod.__path__ = [str(path)]
    spec = importlib.util.spec_from_file_location("tensorswitch_v2.utils.record_planner", base / "utils" / "record_planner.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def planner(src=None):
    """TensorSwitch's record_planner from `src` or $TENSORSWITCH_SRC; None if unavailable."""
    src = src or os.environ.get("TENSORSWITCH_SRC")
    if not src:
        return None
    if src not in _PLANNER:
        try:
            _PLANNER[src] = _load_planner(src)
        except Exception as e:
            print(f"note: TensorSwitch planner not loaded from {src} ({e})", file=sys.stderr)
            _PLANNER[src] = None
    return _PLANNER[src]


def _described_2d(entry):
    """The planner flags this only in a note (no structured field)."""
    return any("described as 2D" in n for n in entry.get("notes", []))


def _optional(rp, a):
    """A dropped label the crops can do without: a table / points file, or an alternative no sample file matched."""
    fmt = a["format"]
    return a["role"] == "label" and (fmt in rp.TABLE_FORMATS or fmt == "other" or (fmt in rp.CONVERTIBLE and not a["files"]))


def plan_reasons(rp, plan):
    """(reasons, notes) from the planner's status and per-array notes (the last note says why it's dropped)."""
    arrays = plan.get("arrays", [])
    if not arrays:
        return [f"TensorSwitch planner {plan['status']}: {w}" for w in plan["warnings"][-1:]], []
    reasons = []
    raw_ok = any(a["convertible"] and a["role"] == "raw" for a in arrays)
    if not raw_ok:
        reasons.append("no raw array converts")
    dropped = [(a, f"{a['role']} array {a['index']} ({a['format']}): {a['notes'][-1] if a['notes'] else 'dropped'}")
               for a in arrays if not a["convertible"]]
    reasons += [f"TensorSwitch planner {plan['status']}: {w}" for a, w in dropped if not (raw_ok and _optional(rp, a))]
    notes = [f"note: raw-only crops; {w}" for a, w in dropped if raw_ok and _optional(rp, a)]
    flat = sorted({a["role"] for a in arrays if _described_2d(a)})
    if flat:
        reasons.append(f"{'/'.join(flat)} array described as 2D: the sample is a slice, not a volume")
    unnamed = Counter(a["convert_args"]["input_path"] for a in arrays if a["convertible"] and a["format"] == "hdf5"
                      and a["convert_args"].get("dataset_path") is None)
    if any(n > 1 for n in unnamed.values()):
        reasons.append("several arrays read the same HDF5 file without a dataset name: add ' (<dataset>)' to path_pattern")
    return reasons, notes


def readiness(r, src=None, organism=None):
    """(status, reasons): 'ready', 'not-ready' or 'unknown' (planner unavailable). Reasons starting with
    'note:' don't block. `organism`: the caller settles which organism (tools/miao_layout.py --organism)."""
    im, data, tech = r.get("imaging") or {}, r.get("data") or {}, r.get("technical") or {}
    arrays = [a for a in tech.get("arrays") or [] if isinstance(a, dict)]
    reasons = []
    if data.get("access") != "open":
        reasons.append(f"access {data.get('access')}: an unattended download needs open access")
    dim = str(im.get("dimensionality"))
    if dim != "3D":
        reasons.append(f"dimensionality {dim}: only 3D is covered by the miao layout yet")
    if not voxel_size_found({**im, "dimensionality": "3D"}):
        reasons.append("imaging.voxel_size_nm needs x, y and z")
    orgs = im.get("organism") or []
    if not organism and len(orgs) != 1:
        reasons.append(f"imaging.organism lists {len(orgs)} organisms: one per dataset folder")
    if family(im.get("modality") or []) is None:
        reasons.append(f"modality family unclear ({im.get('modality')}): one of {', '.join(FAMILIES)}")
    reasons += [f"sample URL host is not on TensorSwitch's fetch allowlist: {u}"
                for u in (tech.get("sample") or {}).get("urls") or [] if not _host_ok(u)]
    roles = {a.get("role") for a in arrays}
    if roles - {"raw", "label"}:
        reasons.append(f"array roles {sorted(roles - {'raw', 'label'}, key=str)}: miao#13 only has raw/ and labels/")
    misaligned = sorted({a.get("alignment") for a in arrays if a.get("role") == "label"
                         and a.get("alignment") in ("offset", "cropped", "scaled", "transform-provided")})
    if misaligned:
        reasons.append(f"label alignment {misaligned}: labels aren't on the raw grid at offset 0")
    if dim not in ("3D", "3D+t"):
        return "not-ready", reasons
    rp = planner(src)
    if rp is None:
        return ("not-ready" if reasons else "unknown"), reasons + ["TensorSwitch planner not available"]
    try:
        more, notes = plan_reasons(rp, rp.plan_record(r, "/OUT"))
    except Exception as e:
        more, notes = [f"TensorSwitch planner failed: {e}"], []
    reasons += more
    return ("not-ready" if reasons else "ready"), reasons or notes


def whole_download_ok(r):
    """Size confirmed by a complete file listing and below WHOLE_DOWNLOAD_MAX_BYTES."""
    size = (r.get("data") or {}).get("size_bytes")
    return (size is not None and size < WHOLE_DOWNLOAD_MAX_BYTES
            and (r.get("technical") or {}).get("size_source") in CONFIRMED_SIZE_SOURCES)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--tensorswitch", metavar="SRC")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args()
    statuses, tally = Counter(), Counter()
    for p in a.paths or sorted((ROOT / "datasets").rglob("*.yaml")):
        r = load_yaml(p)
        status, reasons = readiness(r, a.tensorswitch)
        statuses[status] += 1
        tally.update(re.sub(r"array \d+", "array", x).split(";")[0].split(": http")[0][:100] for x in reasons)
        if a.json:
            print(json.dumps({"id": r.get("id"), "status": status, "reasons": reasons}))
        elif not a.summary:
            print(f"{r.get('id')}: {status}")
            for x in reasons:
                print(f"  - {x}")
    if a.summary:
        print(", ".join(f"{n} {s}" for s, n in statuses.most_common()))
        for reason, n in tally.most_common():
            print(f"{n:5d}  {reason}")


if __name__ == "__main__":
    main()
