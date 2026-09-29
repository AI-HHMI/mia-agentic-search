"""Dead-man's switch: alert Slack when a routine stops running or keeps failing.

    python tools/watchdog.py [--dry-run]

Reads run logs from main + claude/* branches (see collect_runs.py). Posts to the
Slack incoming webhook in $SLACK_WEBHOOK_URL. Exit code 1 if any alert fired.
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

# Expected cadence in hours; keep in sync with the routine schedules and site_template.html.
CADENCE_H = {"harvest-repositories": 1, "harvest-literature": 3, "harvest-websearch": 3, "maintainer": 24}
STALE_FACTOR = 2
RUN_BUDGET_H = 2  # a run may take up to 2 h, and its log is only pushed when it ends
FAILED_STREAK = 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of posting")
    a = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    runs = collect()
    alerts = []
    for routine, cadence in CADENCE_H.items():
        mine = [r for r in runs if r["routine"] == routine]
        if not mine:
            alerts.append(f":warning: *{routine}* has no run logs yet")
            continue
        last = dt.datetime.fromisoformat(mine[0]["started_at"].replace("Z", "+00:00"))
        age_h = (now - last).total_seconds() / 3600
        if age_h > STALE_FACTOR * cadence + RUN_BUDGET_H:
            alerts.append(f":rotating_light: *{routine}* is stale: last run started {age_h:.1f} h ago "
                          f"(expected every {cadence} h, runs take up to {RUN_BUDGET_H} h)")
        streak = mine[:FAILED_STREAK]
        if len(streak) == FAILED_STREAK and all(r.get("status") in ("failed", "running") for r in streak):
            alerts.append(f":x: *{routine}*: last {FAILED_STREAK} runs failed or never finished")
        elif len(streak) == FAILED_STREAK and all(not r.get("counts", {}).get("added") for r in streak):
            alerts.append(f":mag: *{routine}*: last {FAILED_STREAK} runs found no new dataset "
                          f"({', '.join(r.get('stop_reason') or '?' for r in streak)}); the frontier may need new queries")

    if not alerts:
        print("all routines healthy")
        return
    text = "*mia-agentic-search watchdog*\n" + "\n".join(alerts)
    print(text)
    hook = os.environ.get("SLACK_WEBHOOK_URL")
    if hook and not a.dry_run:
        requests.post(hook, data=json.dumps({"text": text}), headers={"Content-Type": "application/json"}, timeout=30)
    sys.exit(1)


if __name__ == "__main__":
    main()
