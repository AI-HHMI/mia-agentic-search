"""Structured run log for each agent run (the source of truth for monitoring).

    python tools/run_log.py start --routine harvest-repositories
    python tools/run_log.py fetched <url> [<url> ...]
    python tools/run_log.py event added --id cremi --pr-url <PR url>
    python tools/run_log.py event duplicate|rejected|low-confidence|error --id X --reason "..."
    python tools/run_log.py query "<source>: <query>"
    python tools/run_log.py continue       # exit 0 = keep searching, 1 = stop (prints why)
    python tools/run_log.py continue --time-only   # enricher: stop only at the cutoff
    python tools/run_log.py event enriched|skipped --id X --pr-url <PR url> [--reason "..."]
    python tools/run_log.py finish [--status ok|partial|failed] [--stop-reason R]
    python tools/run_log.py show
    python tools/run_log.py inspected --kind sample --url U --id X --bytes N [--note "..."]   # ad hoc scripts
    python tools/run_log.py recent-errors --routine enricher [--runs 5] [--min 2]            # ids to skip

Runs search until at least one new dataset is published or SEARCH_CUTOFF_MIN is reached;
the whole run must end within RUN_BUDGET_MIN (tools/common.py).

The active run's path is kept in state/.current_run (git-ignored).
tools/listing.py, probe.py and sample.py append what they read to the run's `inspected` list
(and its URLs to `fetched_urls`) via log_inspection(); the validator checks records against it.
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import ROOT, RUN_BUDGET_MIN, RUNS_DIR, SEARCH_CUTOFF_MIN, STATE_DIR, utcnow  # noqa: E402

CURRENT = STATE_DIR / ".current_run"
EVENTS = ["candidate", "added", "duplicate", "rejected", "low-confidence", "error", "enriched", "skipped"]


def _current():
    if not CURRENT.exists():
        sys.exit("no active run; call `run_log.py start` first")
    return ROOT / CURRENT.read_text().strip()


def _load(p):
    return json.loads(p.read_text())


def _elapsed_min(d):
    t0 = dt.datetime.fromisoformat(d["started_at"].replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - t0).total_seconds() / 60


def _save(p, d):
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")


def active_run():
    """Path of the active run log, or None."""
    return ROOT / CURRENT.read_text().strip() if CURRENT.exists() else None


def log_inspection(kind, url, record_id=None, **info):
    """Record a listing / header / sample read in the active run log (no-op without one)."""
    p = active_run()
    if p is None or not p.exists():
        print(f"note: no active run log; {kind} of {url} not recorded", file=sys.stderr)
        return
    d = _load(p)
    if url not in d["fetched_urls"]:
        d["fetched_urls"].append(url)
    d.setdefault("inspected", []).append({"kind": kind, "url": url, "id": record_id, "at": utcnow(), **info})
    _save(p, d)


def sampled_bytes(record_id):
    """Bytes already downloaded by tools/sample.py for this record in the active run."""
    p = active_run()
    if p is None or not p.exists():
        return 0
    return sum(e.get("bytes", 0) for e in _load(p).get("inspected", [])
               if e.get("kind") == "sample" and e.get("id") == record_id)


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
    fin.add_argument("--stop-reason", choices=["found", "time-limit", "frontier-exhausted", "queue-empty", "error"],
                     help="default: found if a dataset was added, else time-limit if past the cutoff")
    c = sub.add_parser("continue")
    c.add_argument("--time-only", action="store_true", help="ignore found datasets; stop only at the search cutoff")
    sub.add_parser("show")
    ins = sub.add_parser("inspected", help="log a read done by your own script (tools/*.py log themselves)")
    ins.add_argument("--kind", required=True, choices=["listing", "header", "sample"])
    ins.add_argument("--url", required=True)
    ins.add_argument("--id", required=True)
    ins.add_argument("--bytes", type=int, default=0)
    ins.add_argument("--note")
    rec = sub.add_parser("recent-errors", help="record ids with errors in several recent runs of a routine")
    rec.add_argument("--routine", required=True)
    rec.add_argument("--runs", type=int, default=5)
    rec.add_argument("--min", type=int, default=2)
    a = ap.parse_args()

    if a.cmd == "recent-errors":
        logs = sorted(RUNS_DIR.glob(f"{a.routine}-*.json"))[-a.runs:]
        hits = {}
        for lp in logs:
            for i in {e["id"] for e in _load(lp).get("events", []) if e.get("type") == "error" and e.get("id")}:
                hits[i] = hits.get(i, 0) + 1
        print(json.dumps(sorted(i for i, n in hits.items() if n >= a.min)))
        return
    if a.cmd == "inspected":
        log_inspection(a.kind, a.url, a.id, bytes=a.bytes, note=a.note, manual=True)
        return

    if a.cmd == "start":
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        started = utcnow()
        p = RUNS_DIR / f"{a.routine}-{started.replace(':', '').replace('-', '')}.json"
        _save(p, {"routine": a.routine, "started_at": started, "finished_at": None, "status": "running",
                  "dry_run": a.dry_run, "queries": [], "fetched_urls": [], "events": [],
                  "counts": {k: 0 for k in EVENTS}, "pr_urls": [], "inspected": []})
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
        d["counts"][a.type] = d["counts"].get(a.type, 0) + 1
        if a.pr_url:
            d["pr_urls"].append(a.pr_url)
    elif a.cmd == "continue":
        mins = _elapsed_min(d)
        found = d["counts"]["added"] > 0 and not a.time_only
        info = {"elapsed_min": round(mins, 1), "search_cutoff_min": SEARCH_CUTOFF_MIN,
                "budget_min": RUN_BUDGET_MIN, "new_datasets": d["counts"]["added"]}
        if found:
            print(json.dumps({**info, "decision": "STOP: found a new dataset; finish the current query, then publish and save state"}))
            sys.exit(1)
        if mins >= SEARCH_CUTOFF_MIN:
            print(json.dumps({**info, "decision": "STOP: search time is up; publish anything valid and save state now"}))
            sys.exit(1)
        why = "keep working" if a.time_only else "no new dataset yet"
        print(json.dumps({**info, "decision": f"CONTINUE: {why}; {SEARCH_CUTOFF_MIN - mins:.0f} min of search left"}))
        return
    elif a.cmd == "finish":
        d["finished_at"] = utcnow()
        d["stop_reason"] = a.stop_reason or ("found" if d["counts"]["added"] else
                                             "time-limit" if _elapsed_min(d) >= SEARCH_CUTOFF_MIN else "frontier-exhausted")
        done = d["counts"].get("added", 0) + d["counts"].get("enriched", 0)
        d["status"] = a.status or ("failed" if d["counts"]["error"] and not done
                                   else "partial" if d["counts"]["error"] else "ok")
        t0 = dt.datetime.fromisoformat(d["started_at"].replace("Z", "+00:00"))
        t1 = dt.datetime.fromisoformat(d["finished_at"].replace("Z", "+00:00"))
        d["duration_s"] = int((t1 - t0).total_seconds())
        CURRENT.unlink(missing_ok=True)
    elif a.cmd == "show":
        print(json.dumps({**{k: d.get(k) for k in ("routine", "status", "stop_reason", "counts", "started_at")},
                          "elapsed_min": round(_elapsed_min(d), 1), "queries_run": len(d["queries"])}, indent=2))
        return
    _save(p, d)
    print(json.dumps(d["counts"]))


if __name__ == "__main__":
    main()
