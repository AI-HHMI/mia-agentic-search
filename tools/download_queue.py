"""The download queue and the list of downloaded datasets (state/downloads.json, on claude/state/downloader).

    python tools/download_queue.py next [--root demo] [--max-gb 10]   # the next record to download, as JSON
    python tools/download_queue.py record <id> downloaded --plan P --summary S
    python tools/download_queue.py record <id> failed  --reason "..."  [--plan P --summary S]
    python tools/download_queue.py record <id> skipped --reason "..."
    python tools/download_queue.py list                                # what's downloaded, failed, skipped

`next` picks, among the records on main that are download-ready (tools/readiness.py, which needs
$TENSORSWITCH_SRC), the smallest one that isn't downloaded, skipped, or failed twice already. It
skips records larger than --max-gb (an unattended run can't ask) and sample units over 2 GB (the runner
has no LSF cluster), and stops if the disk under --root has less than 3x the dataset's size free. It prints the record path, its size, and the mode:
`whole` when the data is one zip whose size is confirmed by a file listing and < 50 GB
(tools/readiness.py:whole_download_ok), else `sample-unit`.

`record` writes the outcome. A `downloaded` entry holds the dataset folder, the crops from its
manifest.json, the size on disk, the scope, the date, and the record's git commit, so a later
change to the record can be told apart. Restore the list first with
`python tools/publish.py state-pull --routine downloader` and save it after with `state-push`.
"""
import argparse
import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import ROOT, STATE_DIR, iter_record_paths, load_yaml  # noqa: E402
from tools.readiness import planner, readiness, whole_download_ok  # noqa: E402

LIST = STATE_DIR / "downloads.json"
MAX_FAILURES = 2


def now():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load():
    d = json.loads(LIST.read_text()) if LIST.exists() else {}
    for k in ("downloaded", "failed", "skipped"):
        d.setdefault(k, {})
    return d


def save(d):
    LIST.parent.mkdir(parents=True, exist_ok=True)
    LIST.write_text(json.dumps({k: dict(sorted(v.items())) for k, v in d.items()}, indent=2) + "\n")


def record_commit(path):
    return subprocess.run(["git", "log", "-1", "--format=%H", "--", str(path)], cwd=ROOT,
                          capture_output=True, text=True).stdout.strip() or None


def mode_for(r):
    """`whole` when TensorSwitch's whole-dataset planner finds a zip to list and the size allows it."""
    return "whole" if planner()._zip_url(r) and whole_download_ok(r) else "sample-unit"


def cmd_next(a):
    if planner() is None:
        sys.exit("TENSORSWITCH_SRC is not set (or the planner didn't load); readiness can't be checked")
    d = load()
    done = set(d["downloaded"]) | set(d["skipped"])
    failed = {k for k, v in d["failed"].items() if len(v) >= MAX_FAILURES}
    candidates, too_big = [], []
    for p in iter_record_paths():
        r = load_yaml(p)
        if r.get("id") in done | failed or str((r.get("imaging") or {}).get("dimensionality")) != "3D":
            continue
        if readiness(r)[0] != "ready":
            continue
        mode = mode_for(r)
        size = r["data"].get("size_bytes") if mode == "whole" else r["technical"]["sample"].get("size_bytes")
        # unknown size: last in the queue, and only taken if the whole dataset is under --max-gb
        known = size if size is not None else r["data"].get("size_bytes")
        # a sample unit over 2 GB needs the LSF cluster (submit_job), which the runner doesn't use
        if (known is None or known > a.max_gb * 1024 ** 3
                or (mode == "sample-unit" and (size or 0) > planner().MCP_LIMIT_BYTES)):
            too_big.append((known or 0, r["id"]))
            continue
        candidates.append((size is None, size or 0, r["id"], str(p.relative_to(ROOT)), mode))
    if not candidates:
        print(json.dumps({"next": None, "reason": "no ready dataset left to download",
                          "over_max_gb_or_unknown": [i for _, i in sorted(too_big)]}))
        return
    unknown, size, rid, path, mode = min(candidates)
    root = Path(a.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(root).free
    if free < 3 * size:
        sys.exit(f"only {free / 1e9:.1f} GB free under {root}; {rid} needs about {3 * size / 1e9:.1f} GB")
    print(json.dumps({"next": rid, "path": path, "mode": mode, "size_bytes": None if unknown else size,
                      "failures_so_far": len(d["failed"].get(rid, [])), "queue_left": len(candidates) - 1,
                      "over_max_gb_or_unknown": len(too_big)}))


def du(path):
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())


def cmd_record(a):
    d = load()
    path = next((p for p in iter_record_paths() if p.stem == a.id), None)
    entry = {"at": now(), "record_commit": record_commit(path) if path else None}
    if a.plan:
        plan = json.loads(Path(a.plan).read_text())
        entry.update(scope=plan.get("scope"), dataset_dir=plan.get("dataset_dir"))
    if a.summary:
        entry["runner"] = json.loads(Path(a.summary).read_text() if Path(a.summary).exists() else a.summary)
    if a.status == "downloaded":
        if not a.plan:
            sys.exit("downloaded needs --plan")
        manifest = Path(entry["dataset_dir"]) / "manifest.json"
        crops = {k: v for k, v in json.loads(manifest.read_text()).items() if v.get("record") == a.id}
        if not crops:
            sys.exit(f"no crop of {a.id} in {manifest}; not recording it as downloaded")
        entry.update(crops=sorted(crops), labels=sorted({x for v in crops.values() for x in v.get("labels", [])}),
                     bytes_on_disk=sum(du(Path(entry["dataset_dir"]) / c) for c in crops))
        d["downloaded"][a.id] = entry
        d["failed"].pop(a.id, None)
    elif a.status == "failed":
        d["failed"].setdefault(a.id, []).append({**entry, "reason": a.reason})
    else:
        d["skipped"][a.id] = {**entry, "reason": a.reason}
    save(d)
    print(json.dumps({"recorded": a.id, "status": a.status}))


def cmd_list(a):
    d = load()
    print(json.dumps({"downloaded": {k: {x: v.get(x) for x in ("at", "scope", "dataset_dir", "crops")}
                                     for k, v in d["downloaded"].items()},
                      "failed": {k: [f["reason"] for f in v] for k, v in d["failed"].items()},
                      "skipped": {k: v["reason"] for k, v in d["skipped"].items()}}, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("next")
    n.add_argument("--root", default="demo")
    n.add_argument("--max-gb", type=float, default=10.0)
    r = sub.add_parser("record")
    r.add_argument("id")
    r.add_argument("status", choices=["downloaded", "failed", "skipped"])
    r.add_argument("--plan")
    r.add_argument("--summary", help="the runner's summary line (a file or the JSON itself)")
    r.add_argument("--reason")
    sub.add_parser("list")
    a = ap.parse_args()
    if a.cmd == "record" and a.status != "downloaded" and not a.reason:
        sys.exit(f"{a.status} needs --reason")
    {"next": cmd_next, "record": cmd_record, "list": cmd_list}[a.cmd](a)


if __name__ == "__main__":
    main()
