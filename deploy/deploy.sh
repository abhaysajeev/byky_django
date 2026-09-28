#!/usr/bin/env bash
# Deploys the checked-out code on the VPS: back up the database, build, start,
# and wait until the app answers. Run from the project folder as the `deploy`
# user (in the docker group, no sudo); .github/workflows/deploy.yml calls it
# after checking out the commit being deployed. Also safe to run by hand:
#
#   cd ~/byky_django && ./deploy/deploy.sh
#
# Exits non-zero -- and so turns the GitHub run red -- if the backup or the
# health check fails.
set -euo pipefail

COMMIT="${1:-$(git rev-parse HEAD)}"
SHORT="${COMMIT:0:7}"
COMPOSE=(docker compose -f docker-compose.qa.yml -p byky_f_qa)
BACKUPS="$HOME/backups"
KEEP=14
HEALTH_URL="http://127.0.0.1:8007/healthz/"

log() { printf '\n== %s\n' "$*"; }

cd "$(dirname "$0")/.."
[ -f .env ] || { echo ".env is missing in $(pwd) -- copy .env.qa.example and fill it in."; exit 1; }

# 1. Back up the database before anything changes: migrations run on start.
mkdir -p "$BACKUPS"
if [ -n "$("${COMPOSE[@]}" ps -q --status running db 2>/dev/null)" ]; then
    FILE="$BACKUPS/byky-$(date +%Y-%m-%d-%H%M)-$SHORT.dump"
    log "Backing up the database to $FILE"
    # Single quotes on purpose: the variables expand inside the db container.
    # shellcheck disable=SC2016
    "${COMPOSE[@]}" exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > "$FILE"
    if [ ! -s "$FILE" ]; then
        rm -f "$FILE"
        echo "The backup is empty -- stopping before anything is deployed."
        exit 1
    fi
    ls -lh "$FILE"
    # Keep the newest $KEEP backups. The names are this script's own
    # (byky-<date>-<sha>.dump), so plain ls is safe here.
    # shellcheck disable=SC2012
    ls -1t "$BACKUPS"/byky-*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
else
    log "No running database yet (first deploy) -- nothing to back up"
fi

# 2. Build and start. docker/entrypoint.sh applies migrations and collects
#    static files when the web container starts.
log "Building and starting $SHORT"
"${COMPOSE[@]}" build web
"${COMPOSE[@]}" up -d

# 3. Wait for the app to answer.
log "Waiting for $HEALTH_URL"
for _ in $(seq 1 45); do
    if curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
        log "Deployed $SHORT -- healthy"
        "${COMPOSE[@]}" ps
        # Old image layers would slowly fill the disk.
        docker image prune -f >/dev/null
        exit 0
    fi
    sleep 2
done

log "The app did not become healthy in 90 s -- last log lines:"
"${COMPOSE[@]}" logs --tail 80 web || true
echo
echo "The database backup taken before this deploy is the newest file in $BACKUPS."
exit 1
