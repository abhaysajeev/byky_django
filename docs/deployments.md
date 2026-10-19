# Deployments — `qa` and `dev` on one VPS

Two independent copies of the app run on the VPS (97.74.81.23), each with its
own code, database, uploads, backups and settings.

| | **qa** | **dev** |
|---|---|---|
| Used by | testers, **older** app builds | the dev team, newest app builds |
| Branch | `release/qa` (frozen; fixes and promotions only) | `main` |
| Folder on the VPS | `/home/deploy/byky_django` | `/home/deploy/byky_django_dev` |
| Compose project | `byky_f_qa` | `byky_f_dev` |
| App | `http://97.74.81.23:8007` | `http://97.74.81.23:8008` |
| Database port (DBeaver) | 5434 | 5435 |
| Backups | `/home/deploy/backups/` | `/home/deploy/backups/dev/` |
| Web workers | 3 | 2 |
| Logs | log viewer, environment `qa` | log viewer, environment `dev` |

Each folder's `.env` says which copy it is (`COMPOSE_PROJECT`, `APP_PORT`,
`QA_DB_PUBLIC_PORT`, `BACKUP_DIR`, `GUNICORN_WORKERS`, `LOG_ENVIRONMENT`; see
`.env.qa.example`). Left out, each takes qa's value.

## Deploying

**Actions → Deploy → Run workflow**, then:

| To deploy | "Use workflow from" | Target |
|---|---|---|
| dev | `main` | `dev` |
| qa | `release/qa` | `qa` |

A run on the wrong branch for its target is refused. Deploys never overlap:
a second one waits. Each deploy backs up **its own** database first
(`deploy/deploy.sh`), then builds, migrates and health-checks that copy only.

## Background jobs and photos

Each copy also runs **redis**, a Celery **worker** and **beat**
(`docker-compose.qa.yml`): photo previews after an upload, an hourly sweep for
previews that could not be queued, and the nightly deletion of photos no
request attached within 48 h. They use the image `web` was built as, so a
deploy updates all three; only `web` runs migrations.

**Where photos live.** Unset, in the copy's own Docker volume. To use the FTP
folder on this VPS instead, set `MEDIA_HOST_DIR` in that copy's `.env` to its
own subfolder (`.../byky/qa`, `.../byky/dev` -- never the same one for both),
copy what is already in the volume across, then deploy:

```bash
# once, as deploy, for the copy in ~/byky_django (qa)
sudo mkdir -p /ftp/upload/byky/qa          # the FTP folder (its real host path)
docker run --rm -v byky_f_qa_byky_f_qa_media:/from -v /ftp/upload/byky/qa:/to \
  alpine sh -c 'cp -a /from/. /to/'
echo 'MEDIA_HOST_DIR=/ftp/upload/byky/qa' >> ~/byky_django/.env
```

The apps never use FTP: they upload through the API, and photos are sent
only after the app's permission check (nginx `location /_protected/`). The
containers run as root, so files they write are root-owned: the FTP user can
read them but not delete them, which is intended.

## Working with the two branches

```
feature/* ──PR──▶ main ──────────────▶ Deploy dev
                   │ promote: tag, PR main → release/qa
                   ▼
fix/* ──────PR──▶ release/qa ────────▶ Deploy qa
```

Both branches are protected: changes only through a PR with green CI.

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

Never put a feature on `release/qa` — it only takes fixes (cases 2–3) or a
promotion (case 4); otherwise the branches drift and promotions conflict.

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
