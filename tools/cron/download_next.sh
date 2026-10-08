#!/usr/bin/env bash
# Hourly download agent: runs `/download-dataset next` with headless Claude Code, which downloads the
# next download-ready dataset that isn't on the downloads list (state/downloads.json on
# the state branch) and records the outcome. One dataset per run.
#
#   crontab -e:   17 * * * *  /groups/troidl/home/troidlj/mia-agentic-search/tools/cron/download_next.sh
#
# It works in its own worktree (on origin/main, refreshed every run), so your checkout is never
# touched; only the converted data goes to $ROOT (default: <your checkout>/demo). A lock makes a run
# that is still busy skip the next hour. Logs: $ROOT/logs/.
set -uo pipefail
REPO=${MIA_REPO:-/groups/troidl/home/troidlj/mia-agentic-search}
WT=${MIA_DOWNLOADER_WT:-$REPO-downloader}
REF=${MIA_DOWNLOADER_REF:-origin/main}
ROOT=${MIA_DOWNLOAD_ROOT:-$REPO/demo}
TS_REPO=${TENSORSWITCH_REPO:-/groups/troidl/home/troidlj/tensorswitch}
export PATH="$HOME/.local/bin:$HOME/.pixi/bin:$HOME/miniconda3/envs/mia-harvester/bin:/usr/local/bin:/usr/bin:/bin"
export TENSORSWITCH_REPO="$TS_REPO" TENSORSWITCH_SRC="$TS_REPO/src"

mkdir -p "$ROOT/logs"
exec 9>"$ROOT/.download_next.lock"
if ! flock -n 9; then
    echo "$(date -u +%FT%TZ) previous run still going; skipped" >> "$ROOT/logs/skipped.log"
    exit 0
fi
LOG="$ROOT/logs/download-$(date -u +%Y%m%dT%H%M%SZ).log"
{
    echo "== $(date -u +%FT%TZ) download_next ($REF -> $ROOT)"
    git -C "$REPO" fetch -q origin || exit 1
    [ -d "$WT" ] || git -C "$REPO" worktree add -q --detach "$WT" "$REF" || exit 1
    git -C "$WT" checkout -q --force --detach "$REF" || exit 1
    cd "$WT" || exit 1
    claude -p "/download-dataset next root=$ROOT" \
        --allowedTools "Skill" "Read" "Glob" "Grep" \
            "Bash(python tools/:*)" "Bash(pixi run --manifest-path:*)" \
            "Bash(mkdir -p:*)" "Bash(ls:*)" "Bash(du:*)" "Bash(find:*)" "Bash(cat:*)"
    echo "== $(date -u +%FT%TZ) exit $?"
} >> "$LOG" 2>&1
