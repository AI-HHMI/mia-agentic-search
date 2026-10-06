"""Run a tools/miao_layout.py plan with TensorSwitch, without the MCP (same functions the MCP tools call).

    python tools/miao_layout.py <record> --root demo --tensorswitch ../tensorswitch/src --label-class X > plan.json
    pixi run --manifest-path ../tensorswitch/pyproject.toml python tools/miao_run.py plan.json [--keep-source]
    python tools/miao_run.py plan.json --finalize      # after running the steps with the MCP tools yourself

Runs the steps in order (fetch_dataset, convert, verify_output) and stops at the first one that doesn't
succeed. After verify_output passes it adds the crop to <dataset_dir>/manifest.json and deletes the
staged downloads (unless --keep-source). Prints one JSON line per step and a final summary line.
Prefer the MCP tools in an interactive session (the download-dataset skill); this runner is for batch
runs and for environments where the MCP isn't registered.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan")
    ap.add_argument("--keep-source", action="store_true", help="keep <root>/staging/<id>/ after a passing verify")
    ap.add_argument("--finalize", action="store_true",
                    help="don't run steps; check the crop's verification.json passed, then write the manifest and clean up")
    a = ap.parse_args()
    plan = json.loads(Path(a.plan).read_text())
    if plan.get("stop"):
        sys.exit(f"plan has stop reasons, not running: {plan['stop']}")
    if a.finalize:
        vfile = Path(plan["crop"]) / "verification.json"
        verdict = json.loads(vfile.read_text()).get("overall") if vfile.exists() else None
        if verdict != "pass":
            sys.exit(f"verification is {verdict or 'missing'} ({vfile}); run verify_output until it passes first")
        return finish(plan, a.keep_source, verdict)
    from tensorswitch_v2 import mcp_server as ts   # imported here: needs TensorSwitch's environment

    verdict = None
    for i, step in enumerate(plan["steps"], 1):
        result = json.loads(getattr(ts, step["tool"])(**step["args"]))
        status = result.get("status") or result.get("overall") or ("error" if "error" in result else None)
        print(json.dumps({"step": i, "tool": step["tool"], "status": status,
                          "error": result.get("error"), "message": result.get("message")}))
        if step["tool"] == "verify_output":
            verdict = result.get("overall") or result.get("status")
            if verdict != "pass":
                print(json.dumps(result, indent=2), file=sys.stderr)
                sys.exit(f"verify_output: {verdict}; output kept, downloads kept in {plan['staging']}")
        elif "error" in result or status != "success":
            print(json.dumps(result, indent=2), file=sys.stderr)
            sys.exit(f"step {i} ({step['tool']}) did not succeed")
    finish(plan, a.keep_source, verdict)


def finish(plan, keep_source, verdict):
    dataset_dir = Path(plan["dataset_dir"])
    manifest = dataset_dir / "manifest.json"
    entries = json.loads(manifest.read_text()) if manifest.exists() else {}
    entries.update(plan["manifest_entry"])
    manifest.write_text(json.dumps(entries, indent=2) + "\n")
    if not keep_source:
        shutil.rmtree(plan["staging"], ignore_errors=True)
    print(json.dumps({"done": plan["crop"], "verify": verdict, "manifest": str(manifest),
                      "source_deleted": not keep_source}))


if __name__ == "__main__":
    main()
