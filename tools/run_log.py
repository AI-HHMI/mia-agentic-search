"""Structured run log for each agent run (the source of truth for monitoring).

    python tools/run_log.py start --routine harvest-repositories
    python tools/run_log.py fetched <url> [<url> ...]
    python tools/run_log.py event added --id cremi --pr-url <PR url>
    python tools/run_log.py event duplicate|rejected|low-confidence|error --id X --reason "..."
    python tools/run_log.py query "<source>: <query>"
    python tools/run_log.py finish [--status ok|partial|failed]
    python tools/run_log.py show

The active run's path is kept in state/.current_run (git-ignored).
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import ROOT, RUNS_DIR, STATE_DIR, utcnow  # noqa: E402

CURRENT = STATE_DIR / ".current_run"
EVENTS = ["candidate", "added", "duplicate", "rejected", "low-confidence", "error"]


def _current():
    if not CURRENT.exists():
        sys.exit("no active run; call `run_log.py start` first")
    return ROOT / CURRENT.read_text().strip()


def _load(p):
    return json.loads(p.read_text())


def _save(p, d):
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--routine", required=True)
    s.add_argument("--dry-run", action="store_true")
    f = sub.add_parser("fetched")
    f.add_argument("urls", nargs="+")
    q = sub.add_parser("query")
    q.add_argument("text")
    e = sub.add_parser("event")
    e.add_argument("type", choices=EVENTS)
    e.add_argument("--id")
    e.add_argument("--reason")
    e.add_argument("--pr-url", help="for `added`: the dataset's PR")
    fin = sub.add_parser("finish")
    fin.add_argument("--status", choices=["ok", "partial", "failed"])
    sub.add_parser("show")
    a = ap.parse_args()

    if a.cmd == "start":
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        started = utcnow()
        p = RUNS_DIR / f"{a.routine}-{started.replace(':', '').replace('-', '')}.json"
        _save(p, {"routine": a.routine, "started_at": started, "finished_at": None, "status": "running",
                  "dry_run": a.dry_run, "queries": [], "fetched_urls": [], "events": [],
                  "counts": {k: 0 for k in EVENTS}, "pr_urls": []})
        CURRENT.write_text(str(p.relative_to(ROOT)))
        print(p.relative_to(ROOT))
        return

    p = _current()
    d = _load(p)
    if a.cmd == "fetched":
        d["fetched_urls"].extend(u for u in a.urls if u not in d["fetched_urls"])
    elif a.cmd == "query":
        d["queries"].append(a.text)
    elif a.cmd == "event":
        d["events"].append({"type": a.type, "id": a.id, "reason": a.reason, "pr_url": a.pr_url, "at": utcnow()})
        d["counts"][a.type] += 1
        if a.pr_url:
            d["pr_urls"].append(a.pr_url)
    elif a.cmd == "finish":
        d["finished_at"] = utcnow()
        d["status"] = a.status or ("failed" if d["counts"]["error"] and not d["counts"]["added"]
                                   else "partial" if d["counts"]["error"] else "ok")
        t0 = dt.datetime.fromisoformat(d["started_at"].replace("Z", "+00:00"))
        t1 = dt.datetime.fromisoformat(d["finished_at"].replace("Z", "+00:00"))
        d["duration_s"] = int((t1 - t0).total_seconds())
        CURRENT.unlink(missing_ok=True)
    elif a.cmd == "show":
        print(json.dumps({k: d[k] for k in ("routine", "status", "counts", "started_at")}, indent=2))
        return
    _save(p, d)
    print(json.dumps(d["counts"]))


if __name__ == "__main__":
    main()
