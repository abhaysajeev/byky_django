#!/bin/sh
set -e

echo "Waiting for PostgreSQL at ${POSTGRES_HOST}:${POSTGRES_PORT}..."
python - <<'PY'
import os, socket, time
host = os.environ.get("POSTGRES_HOST", "db")
port = int(os.environ.get("POSTGRES_PORT", "5432"))
for attempt in range(60):
    try:
        with socket.create_connection((host, port), timeout=2):
            break
    except OSError:
        time.sleep(1)
else:
    raise SystemExit(f"PostgreSQL not reachable at {host}:{port}")
PY

mkdir -p staticfiles
python manage.py migrate --noinput
# Runserver (local dev) serves static files itself via the staticfiles app
# finder while DEBUG=True, so this was never needed there. Gunicorn (qa/prod)
# relies on WhiteNoise's CompressedManifestStaticFilesStorage, which needs
# hashed filenames pre-collected -- skip it and static assets 404 or serve
# stale. Harmless to always run: a no-op when nothing changed.
if [ "${DJANGO_COLLECTSTATIC:-0}" = "1" ]; then
    python manage.py collectstatic --noinput
fi
exec "$@"
