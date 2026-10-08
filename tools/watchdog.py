"""Dead-man's switch: alert Slack when a routine stops running or keeps failing.

    python tools/watchdog.py [--dry-run]

Reads run logs from main + the state branch (collect_runs.py) and writes the result to state/watchdog.json
(pushed by watchdog.yml with `publish.py state-push --routine watchdog`; the maintainer's digest quotes it).
Posts to the Slack incoming webhook in $SLACK_WEBHOOK_URL only when the set of alerts changed since the
last run on the state branch. Always exits 0. watchdog.yml skips the run while the repo variable PAUSED is true.
"""
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.collect_runs import collect  # noqa: E402
from tools.common import STATE_BRANCH, STATE_DIR, git  # noqa: E402

# Expected cadence in hours; keep in sync with the routine schedules and site_template.html.
CADENCE_H = {"harvest-repositories": 1, "harvest-literature": 1, "harvest-websearch": 1, "maintainer": 24, "enricher": 1}
FINDS_DATASETS = ("harvest-repositories", "harvest-literature", "harvest-websearch")
STALE_FACTOR = 2
RUN_BUDGET_H = 2  # a run may take up to 2 h, and its log is only pushed when it ends
FAILED_STREAK = 3
RESULT = STATE_DIR / "watchdog.json"


def check(runs, now):
    """{"<routine>:<kind>": message}; the keys (not the messages, which carry ages) decide whether to post."""
    alerts = {}
    for routine, cadence in CADENCE_H.items():
        mine = [r for r in runs if r["routine"] == routine]
        if not mine:
            alerts[f"{routine}:none"] = f":warning: *{routine}* has no run logs yet"
            continue
        last = dt.datetime.fromisoformat(mine[0]["started_at"].replace("Z", "+00:00"))
        age_h = (now - last).total_seconds() / 3600
        if age_h > STALE_FACTOR * cadence + RUN_BUDGET_H:
            alerts[f"{routine}:stale"] = (f":rotating_light: *{routine}* is stale: last run started {age_h:.1f} h ago "
                                          f"(expected every {cadence} h, runs take up to {RUN_BUDGET_H} h)")
        streak = mine[:FAILED_STREAK]
        if len(streak) == FAILED_STREAK and all(r.get("status") in ("failed", "running") for r in streak):
            alerts[f"{routine}:failing"] = f":x: *{routine}*: last {FAILED_STREAK} runs failed or never finished"
        elif (routine in FINDS_DATASETS and len(streak) == FAILED_STREAK
              and all(not r.get("counts", {}).get("added") for r in streak)):
            alerts[f"{routine}:dry"] = (f":mag: *{routine}*: last {FAILED_STREAK} runs found no new dataset "
                                        f"({', '.join(r.get('stop_reason') or '?' for r in streak)}); "
                                        "the frontier may need new queries")
    return alerts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print, don't post or write state/watchdog.json")
    a = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    alerts = check(collect(), now)
    try:
        before = json.loads(git("show", f"origin/{STATE_BRANCH}:state/watchdog.json"))["alerts"]
    except (json.JSONDecodeError, KeyError):
        before = {}
    text = "*mia-agentic-search watchdog*\n" + ("\n".join(alerts.values()) or ":white_check_mark: all routines healthy")
    changed = set(alerts) != set(before)
    print(text + ("\n(changed: posting)" if changed else "\n(unchanged: not posting)"))
    if a.dry_run or not changed:  # unchanged: no state commit, no post
        return
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps({"checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "alerts": alerts}, indent=1) + "\n")
    hook = os.environ.get("SLACK_WEBHOOK_URL")
    if hook:
        requests.post(hook, data=json.dumps({"text": text}), headers={"Content-Type": "application/json"}, timeout=30)


if __name__ == "__main__":
    main()
