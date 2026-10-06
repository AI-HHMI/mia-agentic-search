"""Run a tools/miao_layout.py plan with TensorSwitch, one crop at a time (same functions the MCP tools call).

    python tools/miao_layout.py <record> --root demo --tensorswitch ../tensorswitch/src --label-class X [--whole] > plan.json
    pixi run --manifest-path ../tensorswitch/pyproject.toml python tools/miao_run.py plan.json [--keep-source]
    python tools/miao_run.py plan.json --finalize      # after running a crop's steps with the MCP tools yourself

For each crop in turn: fetch its files, convert, verify_output, then record it in
<dataset_dir>/manifest.json, fill each label's bbox.size from the converted array, and delete that
crop's downloads (unless --keep-source). Staging therefore never holds more than one crop.

Resumable: a crop already in the manifest whose verification.json says `pass` is skipped, so after an
interruption just run the same command again. A crop that fails (a step errors, or verify_output isn't
`pass`) keeps its output and downloads for inspection; the run goes on with the next crop and exits 1
at the end, listing the failures. Prints one JSON line per step and a summary line.
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def verdict_of(crop):
    vfile = Path(crop) / "verification.json"
    return json.loads(vfile.read_text()).get("overall") if vfile.exists() else None


def is_done(plan, c):
    manifest = Path(plan["dataset_dir"]) / "manifest.json"
    entries = json.loads(manifest.read_text()) if manifest.exists() else {}
    entry = entries.get(Path(c["crop"]).name)
    return bool(entry and sorted(entry.get("files", [])) == sorted(c["files"]) and verdict_of(c["crop"]) == "pass")


def fill_bbox_size(crop):
    """bbox.size (x, y, z) of each label from its converted s0 array; drops the matching review item."""
    for zj in (Path(crop) / "labels").glob("*/zarr.json"):
        meta = json.loads(zj.read_text())
        attrs = meta.get("attributes", {})
        bbox = attrs.get("bbox")
        if not isinstance(bbox, dict) or bbox.get("size"):
            continue
        ms = attrs["ome"]["multiscales"][0]
        names = [ax["name"] for ax in ms["axes"]]
        shape = json.loads((zj.parent / ms["datasets"][0]["path"] / "zarr.json").read_text())["shape"]
        if all(a in names for a in "xyz"):
            bbox["size"] = [shape[names.index(a)] for a in "xyz"]
            attrs["review_needed"] = [x for x in attrs.get("review_needed", []) if not x.startswith("bbox.size")]
            zj.write_text(json.dumps(meta, indent=2) + "\n")


def finish(plan, c, keep_source):
    fill_bbox_size(c["crop"])
    manifest = Path(plan["dataset_dir"]) / "manifest.json"
    entries = json.loads(manifest.read_text()) if manifest.exists() else {}
    entries[Path(c["crop"]).name] = c["manifest_entry"]
    manifest.write_text(json.dumps(dict(sorted(entries.items())), indent=2) + "\n")
    if not keep_source:
        for f in c["staged_files"]:
            Path(f).unlink(missing_ok=True)
        staging = Path(plan["staging"])
        for d in sorted((p for p in staging.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
            if not any(d.iterdir()):
                d.rmdir()
        if staging.exists() and not any(staging.iterdir()):
            staging.rmdir()


def run_crop(ts, plan, c):
    """Run one crop's steps; returns None on success or the reason it failed."""
    for i, step in enumerate(c["steps"], 1):
        result = json.loads(getattr(ts, step["tool"])(**step["args"]))
        status = result.get("overall") if step["tool"] == "verify_output" else result.get("status")
        print(json.dumps({"crop": Path(c["crop"]).name, "step": i, "tool": step["tool"], "status": status,
                          "error": result.get("error"), "message": result.get("message")}), flush=True)
        if step["tool"] == "verify_output":
            if status != "pass":
                failed = [x for x in result.get("checks", []) if x.get("status") not in ("pass", None)]
                return f"verify_output {status}: {[x.get('name') for x in failed] or result}"
        elif "error" in result or status != "success":
            return f"step {i} ({step['tool']}): {result.get('error') or status}: {result.get('message')}"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan")
    ap.add_argument("--keep-source", action="store_true", help="keep each crop's downloads after it passes")
    ap.add_argument("--summary-out", help="also write the summary line to this file")
    ap.add_argument("--finalize", action="store_true",
                    help="don't run steps; record every crop whose verification.json says pass, and clean up")
    a = ap.parse_args()
    plan = json.loads(Path(a.plan).read_text())
    if plan.get("stop"):
        sys.exit(f"plan has stop reasons, not running: {plan['stop']}")
    done, skipped, failed = [], [], {}

    if a.finalize:
        for c in plan["crops"]:
            if is_done(plan, c):
                skipped.append(Path(c["crop"]).name)
            elif verdict_of(c["crop"]) == "pass":
                finish(plan, c, a.keep_source)
                done.append(Path(c["crop"]).name)
            else:
                failed[Path(c["crop"]).name] = f"verification {verdict_of(c['crop']) or 'missing'}"
    else:
        from tensorswitch_v2 import mcp_server as ts   # imported here: needs TensorSwitch's environment
        for c in plan["crops"]:
            name = Path(c["crop"]).name
            if is_done(plan, c):
                skipped.append(name)
                continue
            if any(s["tool"] == "submit_job" for s in c["steps"]):
                failed[name] = "needs the LSF cluster (over 2 GB): run its steps with the MCP tools (submit_job)"
                continue
            if Path(c["crop"]).exists():          # a half-written crop from an interrupted run: start it over
                shutil.rmtree(c["crop"])
            reason = run_crop(ts, plan, c)
            if reason:
                failed[name] = reason
            else:
                finish(plan, c, a.keep_source)
                done.append(name)
    summary = json.dumps({"dataset_dir": plan["dataset_dir"], "crops": len(plan["crops"]), "done": done,
                          "already_done": skipped, "failed": failed})
    print(summary)
    if a.summary_out:
        Path(a.summary_out).write_text(summary + "\n")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
