"""What blocks automatic conversion of a record (TensorSwitch, issues #518 / #519). Read-only, always exits 0.

    python tools/convertibility.py                         # every record under datasets/
    python tools/convertibility.py <file> [<file> ...]     # just these (the enricher runs it on its record)
    python tools/convertibility.py --summary               # counts per gap
    python tools/convertibility.py --json                  # one JSON object per record
    python tools/convertibility.py <file> --tensorswitch ../tensorswitch/src   # also run TensorSwitch's planner

Per record it prints **needs** (a converter can't proceed without it) and **helpful** (makes conversion
automatic or more reliable). Records are valid under the schema either way; this is not part of
validate.py or CI. With --tensorswitch, the planner's own status (ready / partial / blocked) and
warnings are added; it reads the record only, nothing is downloaded.
"""
import argparse
import fnmatch
import importlib.util
import json
import re
import sys
import types
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import ROOT, load_yaml  # noqa: E402
from tools.download_check import ALLOWED_HOSTS, SCHEMES  # noqa: E402

FAMILIES = {
    "em": {"FIB-SEM", "SBF-SEM", "ssTEM", "ssSEM", "TEM", "cryo-EM", "cryo-ET"},
    "lm": {"confocal", "spinning-disk", "widefield", "light-sheet", "two-photon", "super-resolution", "brightfield",
           "histology-WSI", "phase-contrast", "DIC", "fluorescence-other"},
    "xray": {"X-ray"},
    "exm": {"expansion"},
}
# formats whose headers can't be trusted for a voxel size (#518: MRC stores µm in the Å field, NIfTI has no usable unit)
UNTRUSTED_HEADER = {"hdf5", "nifti", "mrc", "png", "jpeg"}
HDF5_DATASET = re.compile(r"\s\((?P<name>[^()]+)\)\s*$")       # the convention: 'a/*.h5 (volumes/raw)'
OTHER_DATASET = re.compile(r"\s::\s*\S+\s*$")                   # older spelling: 'a.h5 :: FOV0'
PLACEHOLDER = re.compile(r"<[^<>]+>|#{2,}|(?<![A-Z])(?:N{2,}|P{2,})(?![A-Za-z])")   # <name>, ###, NNN, mvNN, PP
FILE_EXT = re.compile(r"\.[A-Za-z0-9]{1,6}(\.gz)?$")


def family(modalities):
    fams = {f for m in modalities for f, members in FAMILIES.items() if m in members}
    return fams, "other" in modalities


def voxel_complete(im):
    vs = im.get("voxel_size_nm") or {}
    return bool(vs.get("x") and vs.get("y") and (vs.get("z") or str(im.get("dimensionality", "")).startswith("2D")))


def _sample_file(url):
    """The concrete file a sample URL names (a zip member, or the URL's last path segment)."""
    if "::" in url:
        return url.split("::", 1)[1]
    url = url.split("?", 1)[0]
    if url.endswith("/"):
        return ""
    return url.removesuffix("/content")    # Zenodo API download links: .../files/<name>/content


def _host_ok(url):
    p = urlsplit(url.split("::", 1)[0])
    host = (p.netloc if p.scheme == "s3" else p.hostname or "").lower()
    if p.scheme == "s3":
        return True                                   # s3://bucket/... -> bucket.s3.amazonaws.com
    return p.scheme in SCHEMES and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def _expand_braces(pattern):
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    return [x for alt in m.group(1).split(",") for x in _expand_braces(pattern[:m.start()] + alt + pattern[m.end():])]


def _matches(path, pattern):
    pattern = HDF5_DATASET.sub("", OTHER_DATASET.sub("", pattern)).strip()
    pattern = pattern.split("::", 1)[-1]                       # 'a.zip::member' patterns
    name = path.rsplit("/", 1)[-1]
    globs = [g for part in pattern.split(", ") for g in _expand_braces(PLACEHOLDER.sub("*", part.strip()))]
    for glob in globs:
        if any(fnmatch.fnmatch(p, g) for p in (path, name) for g in (glob, "*/" + glob, glob.rsplit("/", 1)[-1])):
            return True
    return False


def check(r):
    im, an = r.get("imaging") or {}, r.get("annotations") or {}
    tech = r.get("technical") or {}
    arrays = [a for a in tech.get("arrays") or [] if isinstance(a, dict)]
    urls = (tech.get("sample") or {}).get("urls") or []
    needs, helpful = [], []

    if not tech:
        needs.append("no technical block (the files have not been described)")
    elif not arrays:
        needs.append("no technical.arrays (raw and label files are not known)")
    if not urls:
        needs.append("no technical.sample.urls (no concrete file to download)")
    for u in urls:
        f = _sample_file(u)
        if not f or not FILE_EXT.search(f.rstrip("/")):
            needs.append(f"sample URL is a landing page or folder, not a file: {u}")
        elif arrays and not any(_matches(f, a.get("path_pattern") or "") for a in arrays):
            helpful.append(f"sample file matches no array path_pattern: {f}")
        if not _host_ok(u):
            needs.append(f"sample URL host is not on TensorSwitch's fetch allowlist: {u}")
    dl = (r.get("data") or {}).get("download_url")
    if not dl:
        helpful.append("no data.download_url")
    else:
        f = _sample_file(dl)
        if not f or not FILE_EXT.search(f.rstrip("/")):
            helpful.append(f"data.download_url is a landing page or folder, not a file (whole-dataset conversion needs a direct archive or file): {dl}")
        if not _host_ok(dl):
            helpful.append(f"data.download_url host is not on TensorSwitch's fetch allowlist: {dl}")

    if not voxel_complete(im):
        fmts = {a.get("format") for a in arrays if a.get("role") in ("raw", "target")} or set((r.get("data") or {}).get("formats") or [])
        if not arrays or fmts & UNTRUSTED_HEADER or not fmts:
            needs.append("no imaging.voxel_size_nm" + (f" and {'/'.join(sorted(fmts & UNTRUSTED_HEADER))} headers can't be trusted"
                                                      if fmts & UNTRUSTED_HEADER else ""))
        else:
            helpful.append("no imaging.voxel_size_nm (only the file header can supply it)")

    orgs = im.get("organism") or []
    if not orgs:
        needs.append("imaging.organism is empty")
    elif len(orgs) > 1:
        needs.append(f"imaging.organism lists {len(orgs)} organisms; each is stored as its own dataset, "
                     "so notes must say which files belong to which")
    fams, other = family(im.get("modality") or [])
    if not fams:
        needs.append("imaging.modality names no family (electron, light, X-ray, expansion): only `other`")
    elif len(fams) > 1:
        needs.append(f"imaging.modality mixes families ({', '.join(sorted(fams))}); say which files are which")
    elif other:
        needs.append("imaging.modality has `other` next to a clear value")

    for a in arrays:
        pat = a.get("path_pattern") or ""
        if a.get("format") == "hdf5":
            if OTHER_DATASET.search(pat):
                needs.append(f"HDF5 dataset written as ' :: name', use a trailing ' (name)': {pat}")
            elif not HDF5_DATASET.search(pat):
                needs.append(f"HDF5 array without a dataset name in path_pattern (trailing ' (name)'): {pat or '?'}")
            if not a.get("axes"):
                helpful.append(f"HDF5 array without axes: {pat}")
        if PLACEHOLDER.search(pat):
            helpful.append(f"path_pattern has placeholders instead of a glob: {pat}")
        if ", " in pat:
            helpful.append(f"path_pattern is a list, not one glob: {pat}")
    if an.get("present") and an.get("source") in ("unknown", "mixed", None):
        helpful.append(f"annotations.source is {an.get('source') or 'missing'}")
    if an.get("present") and an.get("coverage") in ("unknown", "partial"):
        helpful.append(f"annotations.coverage is {an.get('coverage')}; say in notes which part of the volume is annotated")
    return list(dict.fromkeys(needs)), list(dict.fromkeys(helpful))


def _planner(src):
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--tensorswitch", metavar="SRC", help="TensorSwitch src/ folder (unified branch): add its planner's verdict")
    a = ap.parse_args()
    paths = [Path(p) for p in a.paths] or sorted((ROOT / "datasets").rglob("*.yaml"))
    rp = None
    if a.tensorswitch:
        try:
            rp = _planner(a.tensorswitch)
        except Exception as e:  # report, never fail
            print(f"note: TensorSwitch planner not loaded ({e}); skipping it", file=sys.stderr)
    counts, rows = Counter(), []
    for p in paths:
        r = load_yaml(p)
        needs, helpful = check(r)
        row = {"id": r.get("id"), "path": str(p), "dimensionality": (r.get("imaging") or {}).get("dimensionality"),
               "needs": needs, "helpful": helpful}
        if rp is not None and str(row["dimensionality"]).startswith("3D"):
            try:
                plan = rp.plan_record(r, "/OUT")
                row["tensorswitch"] = {"status": plan.get("status"), "needs": plan.get("needs", []),
                                       "warnings": plan.get("warnings", [])}
            except Exception as e:
                row["tensorswitch"] = {"status": "error", "warnings": [str(e)]}
        rows.append(row)
        counts.update(re.sub(r"[:(].*", "", g).strip() for g in needs + helpful)
    if a.json:
        for row in rows:
            print(json.dumps(row))
    elif a.summary:
        n = sum(1 for row in rows if not row["needs"])
        print(f"{len(rows)} records, {n} with no needs")
        for gap, c in counts.most_common():
            print(f"{c:5d}  {gap}")
        if rp is not None:
            print("tensorswitch:", dict(Counter(row["tensorswitch"]["status"] for row in rows if "tensorswitch" in row)))
    else:
        for row in rows:
            status = "ok" if not row["needs"] else f"{len(row['needs'])} needs"
            ts = f"  [tensorswitch: {row['tensorswitch']['status']}]" if "tensorswitch" in row else ""
            print(f"{row['id']}: {status}{ts}")
            for g in row["needs"]:
                print(f"  need     {g}")
            for g in row["helpful"]:
                print(f"  helpful  {g}")
            for w in (row.get("tensorswitch") or {}).get("warnings", []):
                print(f"  planner  {w}")


if __name__ == "__main__":
    main()
