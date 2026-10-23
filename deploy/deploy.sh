#!/usr/bin/env bash
# Deploys the checked-out code on the VPS: back up the database, pull the image
# GitHub built (or build it, when run by hand), start, and wait until the app
# answers. Run from the project folder as the `deploy` user (in the docker
# group, no sudo); .github/workflows/deploy.yml calls it after checking out the
# commit being deployed, as `deploy.sh <commit> <image> <user>` with the
# registry token on stdin. Also safe to run by hand (builds on this server):
#
#   cd ~/byky_django && ./deploy/deploy.sh          # qa
#   cd ~/byky_django_dev && ./deploy/deploy.sh      # dev
#   cd ~/byky_django_live && ./deploy/deploy.sh     # live
#
# Which copy it deploys comes from that folder's .env (docs/deployments.md):
# COMPOSE_PROJECT, APP_PORT and BACKUP_DIR. Each defaults to qa's value, so a
# .env without them deploys qa exactly as before.
#
# Exits non-zero -- and so turns the GitHub run red -- if the backup or the
# health check fails.
set -euo pipefail

COMMIT="${1:-$(git rev-parse HEAD)}"
SHORT="${COMMIT:0:7}"
# The image GitHub built for this commit (.github/workflows/deploy.yml), and the
# account to pull it as; the token comes on stdin. Left out (a run by hand), the
# image is built here, as before.
IMAGE="${2:-}"
PULL_USER="${3:-}"
PULL_TOKEN=""
# Read first: the backup's `docker compose exec` would otherwise take stdin.
if [ -n "$IMAGE" ]; then
    IFS= read -r PULL_TOKEN || true
    [ -n "$PULL_TOKEN" ] || { echo "No registry token on stdin -- cannot pull $IMAGE."; exit 1; }
fi
KEEP=14

log() { printf '\n== %s\n' "$*"; }

cd "$(dirname "$0")/.."
[ -f .env ] || { echo ".env is missing in $(pwd) -- copy .env.qa.example and fill it in."; exit 1; }

# One value from .env, or the default. Read as text, never sourced: .env holds
# secrets and is not a shell script. Surrounding quotes are dropped.
env_value() {
    local value
    # `|| true`: a key not in .env is normal (the default applies), not an error.
    value="$(grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- || true)"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    printf '%s' "${value:-$2}"
}

PROJECT="$(env_value COMPOSE_PROJECT byky_f_qa)"
APP_PORT="$(env_value APP_PORT 8007)"
BACKUPS="$(env_value BACKUP_DIR "$HOME/backups")"
COMPOSE=(docker compose -f docker-compose.qa.yml -p "$PROJECT")
HEALTH_URL="http://127.0.0.1:${APP_PORT}/healthz/"
log "Deploying $SHORT as $PROJECT on port $APP_PORT (backups in $BACKUPS)"

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

# 2. Get the image and start. docker/entrypoint.sh applies migrations and
#    collects static files when the web container starts.
if [ -n "$IMAGE" ]; then
    log "Pulling $IMAGE"
    # A throwaway Docker config: the login never stays on this server.
    PULL_CONFIG="$(mktemp -d)"
    trap 'rm -rf "$PULL_CONFIG"' EXIT
    printf '%s\n' "$PULL_TOKEN" | docker --config "$PULL_CONFIG" login ghcr.io -u "$PULL_USER" --password-stdin >/dev/null
    PULL_TOKEN=""
    docker --config "$PULL_CONFIG" pull --quiet "$IMAGE"
    rm -rf "$PULL_CONFIG"
    trap - EXIT
    # Under this copy's own image name -- what web, worker and beat run
    # (docker-compose.qa.yml), so a later `docker compose up` by hand finds it
    # and never builds. The ghcr name is dropped; the image before becomes
    # dangling and is pruned below.
    docker tag "$IMAGE" "$PROJECT-app"
    docker rmi "$IMAGE" >/dev/null
    log "Starting $SHORT"
    "${COMPOSE[@]}" up -d --no-build
else
    log "Building and starting $SHORT (no image given: built on this server)"
    "${COMPOSE[@]}" build web
    "${COMPOSE[@]}" up -d
fi

# nginx reads deploy/nginx/qa.conf only when its container is created, and
# `up` leaves a running proxy alone when just that file changed (the bind
# mount even keeps the old copy: git replaces the file). So when the config
# this deploy brings differs from the one nginx runs, recreate the proxy --
# a second's gap, and only on deploys that change it.
if ! "${COMPOSE[@]}" exec -T proxy cat /etc/nginx/conf.d/default.conf 2>/dev/null | cmp -s - deploy/nginx/qa.conf; then
    log "nginx config changed -- recreating the proxy"
    "${COMPOSE[@]}" up -d --no-deps --force-recreate proxy
fi

# 3. Wait for the app to answer -- through nginx (the `proxy` service owns
#    APP_PORT), so this proves the whole path a browser takes.
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
"${COMPOSE[@]}" logs --tail 30 proxy || true
echo
"${COMPOSE[@]}" logs --tail 30 realtime || true
echo
echo "The database backup taken before this deploy is the newest file in $BACKUPS."
exit 1
