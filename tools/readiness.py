"""Download readiness: can /download-dataset convert this record into the miao layout unattended?

    python tools/readiness.py <file> [<file> ...] [--tensorswitch SRC]     # one line per record
    TENSORSWITCH_SRC=../tensorswitch/src python tools/readiness.py <file>  # same, planner from the env

One rule, used by the auto-merge gate (the `download-ready` label, via tools/labels.py) and by the
download queue (tools/download_queue.py). A record is ready when all of these hold:
  1. it is 3D (3D+t isn't covered by the miao layout yet);
  2. tools/convertibility.py lists no needs;
  3. imaging.voxel_size_nm has x, y and z (the label metadata needs it, whatever the format);
  4. every technical.arrays role is raw or label (miao#13 has no place for restoration targets);
  5. the organism has a short folder name (tools/miao_layout.py:ORGANISM_SHORT);
  6. there is a raw array, and no label is offset, cropped or scaled against it (the label metadata
     puts every label at offset 0 on the raw grid);
  7. TensorSwitch's record planner says `ready`, and doesn't flag a raw or label array of a 3D record
     as 2D (single slices are not volumes).
Check 2 includes the TIFF axes rule (tools/convertibility.py:tiff_axes_needs): every TIFF array states
its axes, and, until TensorSwitch's convert accepts `input_axes`, they end in `yx` (no samples axis).
Check 7 needs the TensorSwitch source (--tensorswitch or $TENSORSWITCH_SRC). Without it the answer
is `unknown`, never `ready`. Read-only; nothing is downloaded.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import load_yaml  # noqa: E402
from tools.convertibility import _planner, check, input_axes_supported  # noqa: E402
from tools.miao_layout import ORGANISM_SHORT  # noqa: E402

_PLANNER = {}


def planner(src=None):
    """TensorSwitch's record_planner from `src` or $TENSORSWITCH_SRC; None if unavailable."""
    src = src or os.environ.get("TENSORSWITCH_SRC")
    if not src:
        return None
    if src not in _PLANNER:
        try:
            _PLANNER[src] = _planner(src)
        except Exception as e:
            print(f"note: TensorSwitch planner not loaded from {src} ({e})", file=sys.stderr)
            _PLANNER[src] = None
    return _PLANNER[src]


def readiness(r, src=None):
    """(status, reasons): status is 'ready', 'not-ready', or 'unknown' (planner unavailable)."""
    im = r.get("imaging") or {}
    reasons = []
    dim = str(im.get("dimensionality"))
    if dim != "3D":
        reasons.append(f"dimensionality {dim}: only 3D is covered by the miao layout yet")
    reasons += check(r, input_axes_supported(src or os.environ.get("TENSORSWITCH_SRC")))[0]
    vs = im.get("voxel_size_nm") or {}
    if not (vs.get("x") and vs.get("y") and vs.get("z")):
        reasons.append("imaging.voxel_size_nm needs x, y and z")
    roles = {a.get("role") for a in (r.get("technical") or {}).get("arrays") or [] if isinstance(a, dict)}
    if roles - {"raw", "label"}:
        reasons.append(f"array roles {sorted(roles - {'raw', 'label'})}: miao#13 only has raw/ and labels/")
    arrays = [a for a in (r.get("technical") or {}).get("arrays") or [] if isinstance(a, dict)]
    if arrays and "raw" not in roles:
        reasons.append("no raw array (labels only; the images are elsewhere)")
    misaligned = sorted({a.get("alignment") for a in arrays if a.get("role") == "label"
                         and a.get("alignment") in ("offset", "cropped", "scaled", "transform-provided")})
    if misaligned:
        reasons.append(f"label alignment {misaligned}: labels aren't on the raw grid at offset 0")
    orgs = im.get("organism") or []
    if len(orgs) == 1 and orgs[0] not in ORGANISM_SHORT:
        reasons.append(f"no folder name for organism {orgs[0]!r} (tools/miao_layout.py:ORGANISM_SHORT)")
    rp = planner(src)
    if rp is None:
        return ("not-ready" if reasons else "unknown"), reasons + ["TensorSwitch planner not available"]
    try:
        plan = rp.plan_record(r, "/OUT")
        if plan.get("status") != "ready":
            reasons.append(f"TensorSwitch planner: {plan.get('status')}"
                           + (f" ({plan['warnings'][0]})" if plan.get("warnings") else ""))
        flat = [x["role"] for x in plan.get("arrays", []) if any("described as 2D" in n for n in x.get("notes", []))]
        if flat:
            reasons.append(f"{'/'.join(sorted(set(flat)))} array described as 2D: the sample is a slice, not a volume")
    except Exception as e:
        reasons.append(f"TensorSwitch planner failed: {e}")
    return ("not-ready" if reasons else "ready"), reasons


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--tensorswitch", metavar="SRC")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    for p in a.paths:
        r = load_yaml(p)
        status, reasons = readiness(r, a.tensorswitch)
        if a.json:
            print(json.dumps({"id": r.get("id"), "status": status, "reasons": reasons}))
        else:
            print(f"{r.get('id')}: {status}")
            for x in reasons:
                print(f"  - {x}")


if __name__ == "__main__":
    main()
