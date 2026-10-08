#!/usr/bin/env bash
# Native download agent: runs `/download-native next` with headless Claude Code, which downloads the next
# dataset that isn't on its downloads list (state/downloads-native.json on the state branch), converts it
# with tools/native.py (no TensorSwitch), checks it numerically and by looking at overlays, and records the
# outcome. One dataset per run.
#
#   crontab -e:   */30 * * * *  /groups/troidl/home/troidlj/mia-agentic-search/tools/cron/download_native_next.sh
#
# Like download_next.sh it works in its own worktree (on origin/main, refreshed every run), so your
# checkout is never touched; only the converted data goes to $ROOT (default: <your checkout>/demo-native).
# A lock makes a run that is still busy skip the next one. Logs: $ROOT/logs/.
set -uo pipefail
REPO=${MIA_REPO:-/groups/troidl/home/troidlj/mia-agentic-search}
WT=${MIA_NATIVE_WT:-$REPO-downloader-native}
REF=${MIA_NATIVE_REF:-origin/main}
ROOT=${MIA_NATIVE_ROOT:-$REPO/demo-native}
export PATH="$HOME/.local/bin:$HOME/miniconda3/envs/mia-harvester/bin:/usr/local/bin:/usr/bin:/bin"

mkdir -p "$ROOT/logs"
exec 9>"$ROOT/.download_native_next.lock"
if ! flock -n 9; then
    echo "$(date -u +%FT%TZ) previous run still going; skipped" >> "$ROOT/logs/skipped.log"
    exit 0
fi
LOG="$ROOT/logs/download-native-$(date -u +%Y%m%dT%H%M%SZ).log"
{
    echo "== $(date -u +%FT%TZ) download_native_next ($REF -> $ROOT)"
    git -C "$REPO" fetch -q origin || exit 1
    [ -d "$WT" ] || git -C "$REPO" worktree add -q --detach "$WT" "$REF" || exit 1
    git -C "$WT" checkout -q --force --detach "$REF" || exit 1
    cd "$WT" || exit 1
    claude -p "/download-native next root=$ROOT" \
        --allowedTools "Skill" "Read" "Glob" "Grep" \
            "Bash(python tools/:*)" "Bash(mkdir -p:*)" "Bash(ls:*)" "Bash(du:*)" "Bash(find:*)" "Bash(cat:*)"
    echo "== $(date -u +%FT%TZ) exit $?"
} >> "$LOG" 2>&1
