#!/usr/bin/env bash
# Dashboard gallery: thumbnails of $DATA (the converted datasets in demo/data/) pushed to
# the state branch, so the GitHub Pages dashboard, which has no demo/, can show them.
# Pushes and redeploys the dashboard only when the gallery changed (a new dataset or crop).
#
#   crontab -e:   */10 * * * *  /groups/troidl/home/troidlj/mia-agentic-search/tools/cron/publish_gallery.sh
#
# Like download_next.sh it works in its own worktree on origin/main, so your checkout is never touched.
# Logs: $REPO/demo/logs/gallery.log.
set -uo pipefail
REPO=${MIA_REPO:-/groups/troidl/home/troidlj/mia-agentic-search}
WT=${MIA_GALLERY_WT:-$REPO-gallery}
REF=${MIA_GALLERY_REF:-origin/main}
DATA=${MIA_GALLERY_DATA:-$REPO/demo/data}
export PATH="$HOME/.local/bin:$HOME/.pixi/bin:$HOME/miniconda3/envs/mia-harvester/bin:/usr/local/bin:/usr/bin:/bin"

LOGDIR="$REPO/demo/logs"
mkdir -p "$LOGDIR"
exec 9>"$REPO/demo/.publish_gallery.lock"
flock -n 9 || exit 0
{
    echo "== $(date -u +%FT%TZ) publish_gallery ($DATA)"
    git -C "$REPO" fetch -q origin || exit 1
    [ -d "$WT" ] || git -C "$REPO" worktree add -q --detach "$WT" "$REF" || exit 1
    git -C "$WT" checkout -q --force --detach "$REF" || exit 1
    cd "$WT" && python tools/demo_gallery.py --publish --data "$DATA"
} >> "$LOGDIR/gallery.log" 2>&1
