# Deployments — `live`, `qa` and `dev` on one VPS

Three independent copies of the app run on the VPS (97.74.81.23), each with
its own code, database, uploads, backups and settings.

| | **live** | **qa** | **dev** |
|---|---|---|---|
| Used by | real stations and customers | testers, **older** app builds | the dev team, newest app builds |
| Branch | `release/live` (promotions from qa, fixes) | `release/qa` (frozen; fixes and promotions only) | `main` |
| Folder on the VPS | `/home/deploy/byky_django_live` | `/home/deploy/byky_django` | `/home/deploy/byky_django_dev` |
| Compose project | `byky_f_live` | `byky_f_qa` | `byky_f_dev` |
| App | `http://97.74.81.23:8009` (HTTPS + domain before go-live) | `http://97.74.81.23:8007` | `http://97.74.81.23:8008` |
| Database port | `127.0.0.1:5436` only (SSH tunnel) | 5434 (DBeaver) | 5435 (DBeaver) |
| Backups | `/home/deploy/backups/live/` | `/home/deploy/backups/` | `/home/deploy/backups/dev/` |
| Web workers | 3 | 3 | 2 |
| Logs | log project `byky-live` | log project `byky-qa`, environment `qa` | log project `byky-qa`, environment `dev` |

Each folder's `.env` says which copy it is (`COMPOSE_PROJECT`, `APP_PORT`,
`QA_DB_PUBLIC_BIND`, `QA_DB_PUBLIC_PORT`, `BACKUP_DIR`, `GUNICORN_WORKERS`,
`LOG_ENVIRONMENT`; see `.env.qa.example`). Left out, each takes qa's value.

## Deploying

**Actions → Deploy → Run workflow**, then:

| To deploy | "Use workflow from" | Target |
|---|---|---|
| dev | `main` | `dev` |
| qa | `release/qa` | `qa` |
| live | `release/live` | `live` |

A run on the wrong branch for its target is refused. Deploys never overlap:
a second one waits. Each deploy backs up **its own** database first
(`deploy/deploy.sh`), then pulls, migrates and health-checks that copy only.

**Images are built on GitHub, never on the VPS.** The workflow's `image` job
builds the app image once and pushes it to
`ghcr.io/abhaysajeev/byky_django:tree-<id>`, named by the commit's **file
tree**: a promotion's merge commit has new ids but the same files, so qa and
live reuse the exact image dev ran ("exists -- reusing it" in the log). The VPS
logs in with the run's own short-lived token (never stored there), pulls, tags
it as the copy's `<project>-app` image and starts it. Rolling back = deploying
an older commit: its image is still there. `./deploy/deploy.sh` run by hand on
the VPS still builds there, as before.

## Background jobs and photos

Each copy also runs **redis**, a Celery **worker** and **beat**
(`docker-compose.qa.yml`): photo previews after an upload, an hourly sweep for
previews that could not be queued, and the nightly deletion of photos no
request attached within 48 h. They use the image `web` was built as, so a
deploy updates all three; only `web` runs migrations.

**Live updates.** A fourth service, **realtime**, serves the websockets of
pages that update themselves (Rental → Live Monitoring). It is the same image
run under uvicorn (`config/asgi.py`); nginx sends `/ws/` to it and everything
else to `web` (`deploy/nginx/qa.conf`). An order change is published through
redis database 1 after it commits (`core/realtime.py`); if redis or realtime
is down nothing is lost — the order still saves, and the page fetches the
whole list again when it reconnects. Nothing to set in `.env`; a deploy starts
it like the others. A domain in front (Caddy) passes websockets through as is.

**Where photos live.** Unset, in the copy's own Docker volume. To use the FTP
folder on this VPS instead, set `MEDIA_HOST_DIR` in that copy's `.env` to its
own subfolder (`.../byky/qa`, `.../byky/dev` -- never the same one for both),
copy what is already in the volume across, then deploy:

```bash
# once, as deploy, for the copy in ~/byky_django (qa)
# The FTP login sees /ftp/upload: vsftpd locks ftpuser into its home.
sudo mkdir -p /home/ftpuser/ftp/upload/byky/qa
docker run --rm -v byky_f_qa_byky_f_qa_media:/from -v /home/ftpuser/ftp/upload/byky/qa:/to \
  alpine sh -c 'cp -a /from/. /to/'
echo 'MEDIA_HOST_DIR=/home/ftpuser/ftp/upload/byky/qa' >> ~/byky_django/.env
```

The apps never use FTP: they upload through the API, and photos are sent
only after the app's permission check (nginx `location /_protected/`). The
containers run as root, so files they write are root-owned: the FTP user can
read them but not delete them, which is intended.

**Push notifications.** The worker sends them through Firebase (FCM v1), one
Firebase project per environment. Until a copy has its key file nothing is
sent: notifications are stored and marked `skipped`. To switch a copy on, put
that project's service-account key on the VPS, readable only by deploy, name
it in `.env`, and deploy -- the worker gets it read-only, nothing else does:

```bash
# once, as deploy, for the copy in ~/byky_django (qa); dev uses its own file
mkdir -p ~/secrets && chmod 700 ~/secrets
# copy the key file from the Firebase console to ~/secrets/byky-qa-fcm.json, then:
chmod 600 ~/secrets/byky-qa-fcm.json
echo 'FCM_CREDENTIALS_FILE=/home/deploy/secrets/byky-qa-fcm.json' >> ~/byky_django/.env
```

## Working with the three branches

```
feature/* ──PR──▶ main ──────────────▶ Deploy dev
                   │ promote: tag, PR main → release/qa
                   ▼
fix/* ──────PR──▶ release/qa ────────▶ Deploy qa
                   │ promote: PR release/qa → release/live (same tag)
                   ▼
fix/* ──────PR──▶ release/live ──────▶ Deploy live
```

All three are protected: changes only through a PR with green CI.

1. **A feature or API change** — branch from `main`, PR into `main`, Deploy
   dev. qa is untouched.
2. **A bug testers need fixed, that `main` also has** — fix it on `main`
   first (case 1). Then branch from `release/qa`, `git cherry-pick <sha>`, PR
   into `release/qa`, Deploy qa. *A fix always lands on `main` first*, so the
   newest code never loses it.
3. **A bug only in qa's code** (code `main` has since rewritten) — branch from
   `release/qa`, fix, PR into `release/qa`, Deploy qa. Nothing to `main`.
4. **Promote** — when a new app build goes to testers: tag `main`
   (`v2026.10.20`, the tag message lists the app builds it serves), PR `main`
   → `release/qa`, Deploy qa. qa's database runs the new migrations on that
   deploy, after its backup.

5. **Go live** — once testers sign off on qa: PR `release/qa` → `release/live`
   (the tag qa was promoted at), Deploy live. Same image as qa, nothing
   rebuilt. A fix live needs at once follows case 2 one step further: `main`,
   then `release/qa`, then `release/live`.

Never put a feature on `release/qa` or `release/live` — they only take fixes
(cases 2–3) or a promotion (cases 4–5); otherwise the branches drift and
promotions conflict.

**App Releases** are per copy (each has its own database). On qa publish only
builds that match qa's server version, and never a forced update the older
builds can't take.

## One-time setup of `dev` (run on the VPS as `deploy`)

Before: `free -h; df -h` — note memory and disk. qa keeps running throughout.

1. **qa's `.env`** — add one line, so its logs are labelled:
   ```bash
   echo 'LOG_ENVIRONMENT=qa' >> ~/byky_django/.env
   ```
2. **dev's code:**
   ```bash
   git clone "$(git -C ~/byky_django remote get-url origin)" ~/byky_django_dev
   ```
   (it checks out `main`).
3. **dev's `.env`** — start from qa's, then edit:
   ```bash
   cp ~/byky_django/.env ~/byky_django_dev/.env
   ```
   Change: `DJANGO_SECRET_KEY` (new — see `.env.qa.example`),
   `POSTGRES_PASSWORD` (new), and set
   ```
   COMPOSE_PROJECT=byky_f_dev
   APP_PORT=8008
   QA_DB_PUBLIC_PORT=5435
   BACKUP_DIR=/home/deploy/backups/dev
   GUNICORN_WORKERS=2
   LOG_ENVIRONMENT=dev
   ```
   Keep `LOG_INGEST_URL` / `LOG_INGEST_KEY` as qa's (one log project, two
   environments).
4. **Seed dev's database from qa** (a one-time copy; afterwards they drift):
   ```bash
   cd ~/byky_django
   docker compose -f docker-compose.qa.yml -p byky_f_qa exec -T db \
     sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > ~/backups/seed-dev.dump
   cd ~/byky_django_dev
   docker compose -f docker-compose.qa.yml -p byky_f_dev up -d db
   docker compose -f docker-compose.qa.yml -p byky_f_dev exec -T db \
     sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --clean --if-exists' \
     < ~/backups/seed-dev.dump
   ```
5. **Copy uploaded files** (station logos etc.) — check the names first with
   `docker volume ls | grep media`:
   ```bash
   docker volume create byky_f_dev_byky_f_qa_media
   docker run --rm -v byky_f_qa_byky_f_qa_media:/from -v byky_f_dev_byky_f_qa_media:/to \
     alpine sh -c 'cp -a /from/. /to/'
   ```
6. **Firewall** — open 8008 (and 5435 if DBeaver needs it) in the provider's
   firewall, if it has one; published Docker ports bypass ufw.
7. **Deploy** — Actions → Deploy: first **qa** from `release/qa` (same code as
   before; proves the new script on the live copy), then **dev** from `main`.
8. **Check:**
   ```bash
   curl -fsS http://127.0.0.1:8007/healthz/ && curl -fsS http://127.0.0.1:8008/healthz/
   free -h
   ```
   Sign in on both; an old tablet books on qa, a dev build on dev; a log line
   from each shows its environment.

Anything recorded on qa after step 4 is not in dev's copy — dev is a snapshot.

## One-time setup of `live` (run on the VPS as `deploy`)

live starts **empty** — its data comes from the masters migration, never from
qa. qa and dev keep running throughout. Before: `free -h; df -h`.

1. **live's code:**
   ```bash
   git clone "$(git -C ~/byky_django remote get-url origin)" ~/byky_django_live
   git -C ~/byky_django_live checkout release/live
   ```
2. **live's `.env`** — start from the example, **never from qa's** (new
   secrets, nothing shared):
   ```bash
   cp ~/byky_django_live/.env.qa.example ~/byky_django_live/.env
   chmod 600 ~/byky_django_live/.env
   ```
   Fill in a new `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD` (see the
   example), `DJANGO_ALLOWED_HOSTS=97.74.81.23,127.0.0.1,localhost`, and set
   ```
   POSTGRES_DB=byky_live
   POSTGRES_USER=byky_live
   COMPOSE_PROJECT=byky_f_live
   APP_PORT=8009
   QA_DB_PUBLIC_BIND=127.0.0.1
   QA_DB_PUBLIC_PORT=5436
   BACKUP_DIR=/home/deploy/backups/live
   GUNICORN_WORKERS=3
   LOG_ENVIRONMENT=live
   ```
   **Logs:** `LOG_INGEST_URL` as qa's, `LOG_INGEST_KEY` = the ingest key of
   the separate **`byky-live`** log project (create it on the log server with
   `scripts/add_project.py`), so the testers' `byky-qa` link never shows live
   requests. **Push:** `FCM_CREDENTIALS_FILE` = the byky-prod key, once that
   Firebase project exists (left out, nothing is sent). **Photos:** optionally
   `MEDIA_HOST_DIR=/home/ftpuser/ftp/upload/byky/live` (`sudo mkdir -p` it
   first, as for qa).
3. **Backups** — add the live database to the backup script (the deploy's own
   dump before each deploy goes to `BACKUP_DIR`).
4. **Firewall** — open 8009 in the provider's firewall if it has one. Do not
   open 5436: live's database answers only on 127.0.0.1.
5. **Deploy** — Actions → Deploy, "Use workflow from" `release/live`, target
   `live`. The first deploy migrates the empty database.
6. **First administrator** (the password is typed, never on the command line):
   ```bash
   cd ~/byky_django_live
   read -rs -p 'New admin password: ' PW; echo
   docker compose -f docker-compose.qa.yml -p byky_f_live exec -T web \
     python manage.py create_admin --username <admin> --password "$PW" \
     --company "BYKY" --company-code BYKY
   unset PW
   ```
7. **Check:**
   ```bash
   curl -fsS http://127.0.0.1:8009/healthz/
   ss -ltn | grep 5436        # must show 127.0.0.1:5436, never 0.0.0.0
   free -h
   ```

**Before real users:** HTTPS on a domain. `msync-web-1` (Caddy) owns ports
80/443 on this VPS, so the live domain is added there as a site proxying to
`127.0.0.1:8009`; then `DJANGO_FORCE_HTTPS=1` and the domain in
`DJANGO_ALLOWED_HOSTS` / `DJANGO_CSRF_TRUSTED_ORIGINS`.
