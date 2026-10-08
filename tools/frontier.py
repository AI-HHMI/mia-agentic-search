"""Rotating search frontier so each run covers new ground (state/frontier/<routine>.yaml).

    python tools/frontier.py next --routine harvest-repositories
    python tools/frontier.py touch --routine harvest-repositories --key "zenodo: FIB-SEM"
    python tools/frontier.py add --routine harvest-websearch --key "..."   # agent-proposed follow-up

`next` prints the least recently searched item (never-searched first), skipping queries already run
in the active run log; an empty list means the frontier is exhausted.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import FRONTIER_DIR, ROOT, STATE_DIR, dump_yaml, load_yaml, utcnow  # noqa: E402

MAX_ITEMS = 300


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["next", "touch", "add"])
    ap.add_argument("--routine", required=True)
    ap.add_argument("--key")
    a = ap.parse_args()

    path = FRONTIER_DIR / f"{a.routine}.yaml"
    items = (load_yaml(path) if path.exists() else None) or []

    if a.cmd == "next":
        items.sort(key=lambda i: i.get("last_searched") or "")
        done = set()
        current = STATE_DIR / ".current_run"
        if current.exists():
            done = set(json.loads((ROOT / current.read_text().strip()).read_text()).get("queries", []))
        print(json.dumps([i["key"] for i in items if i["key"] not in done][:1]))
        return
    if not a.key:
        ap.error("--key required")
    existing = next((i for i in items if i["key"] == a.key), None)
    if a.cmd == "touch":
        if existing is None:
            existing = {"key": a.key, "last_searched": None}
            items.append(existing)
        existing["last_searched"] = utcnow()
    elif a.cmd == "add" and existing is None:
        if len(items) >= MAX_ITEMS:
            sys.exit("frontier full; not adding")
        items.append({"key": a.key, "last_searched": None, "added_by_agent": True})
    FRONTIER_DIR.mkdir(parents=True, exist_ok=True)
    dump_yaml(items, path)


if __name__ == "__main__":
    main()
