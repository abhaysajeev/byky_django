# byky_django

The clean rebuild. Screens are ported one module at a time from the read-only
wireframe project at `../source/ui-template/django-version/full-version/`,
following `.claude/skills/port-ui/SKILL.md`.

## Run it

```bash
cp .env.example .env          # already done once
docker-compose up --build     # http://localhost:8010
```

`docker-compose` waits for PostgreSQL, applies migrations, then serves the app.

| Thing | Where |
|---|---|
| App | http://localhost:8010 |
| Health check | http://localhost:8010/healthz/ |
| PostgreSQL | localhost:5434 (inside Docker: `db:5432`) |

Both host ports are deliberately clear of the other stacks already running on
this machine (5050, 5433).

## Layout

```
byky_django/
├── config/          settings, root urls, wsgi/asgi
├── core/            shared base models, enums, the User identity
├── theme/           the ported UI shell (arrives with the first screen)
├── apps/            one package per module, added as each is built
├── templates/       project-level templates
├── static/          project-level assets
├── requirements/    base.txt, dev.txt
└── docker/          entrypoint
```

## Decisions already baked in

- **PostgreSQL 16**, Python 3.12, Django 5.2, DRF + SimpleJWT.
- **Timezone `Asia/Dubai`, `USE_TZ = True`** — the legacy stored local time and
  converted through an application-config offset.
- **`AUTH_USER_MODEL = "core.User"`** from the first migration, so it never has
  to be swapped later.
- **`django.contrib.auth` is not installed.** Permissions come from Role and
  RolePermission (`design/rbac.md`); the user extends `AbstractBaseUser` only.
  Django's admin site is therefore not available — back-office screens are the
  ported UI.
- **Sessions**: browser-close expiry, no idle timeout, `HttpOnly`/`SameSite=Lax`
  (`design/03-login.md` decisions 3 and 7).
- **JWT**: 30-minute access, 30-day refresh, rotation on
  (`design/03-login.md` section 6.2).

## First sign-in

```bash
docker-compose exec web python manage.py create_admin \
    --username admin --password 'change-me' --name 'System Administrator'
```

Then open http://localhost:8010/login/. There is no Django admin; this command
is how a fresh database gets its first user.

## Commands

```bash
docker-compose exec web python manage.py makemigrations
docker-compose exec web python manage.py migrate
docker-compose exec web python manage.py shell
docker-compose exec web pytest
docker-compose exec web ruff check .
```

## CI/CD

**Every pull request and every push to `main`** runs `.github/workflows/ci.yml`
on GitHub:

| Job | What it checks |
|---|---|
| `checks` | `ruff check .`, no missing migrations, the API schema validates, the full test suite on a fresh Postgres 16 |
| `image` | the production image (`requirements/base.txt`) builds |

`main` only accepts pull requests whose checks are green. Work on a branch,
push it, open the pull request from the link the push prints, merge on GitHub.

**Deploying** is a button: GitHub → Actions → **Deploy** → Run workflow (it
always deploys `main`). `.github/workflows/deploy.yml` logs in to the VPS as
`deploy` and runs `deploy/deploy.sh`, which:

1. backs up the database to `~/backups/byky-<date>-<commit>.dump` (the newest
   14 are kept) — before anything changes, since migrations run on start;
2. builds and starts `docker-compose.qa.yml` (project `byky_f_qa`);
3. waits for `http://127.0.0.1:8007/healthz/` and turns the run red, with the
   app's last log lines, if it does not answer within 90 seconds.

The same script can be run by hand on the VPS: `cd ~/byky_django && ./deploy/deploy.sh`.

**Restoring a backup** (on the VPS, in `~/byky_django`):

```bash
docker compose -f docker-compose.qa.yml -p byky_f_qa stop web
docker compose -f docker-compose.qa.yml -p byky_f_qa exec -T db sh -c 'dropdb -U "$POSTGRES_USER" --force "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
docker compose -f docker-compose.qa.yml -p byky_f_qa exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-acl' < ~/backups/<file>.dump
docker compose -f docker-compose.qa.yml -p byky_f_qa up -d
```

The VPS's `.env` comes from `.env.qa.example`; note that `DJANGO_ALLOWED_HOSTS`
must include `127.0.0.1` and `localhost` for the health checks.

**nginx sits in front of gunicorn** (the `proxy` service; config in
`deploy/nginx/qa.conf`). Only nginx publishes port 8007. gunicorn's sync
workers each wait on one connection until a request arrives, and Chrome opens
connections ahead of need: without nginx those idle connections tied up the
workers, gunicorn killed them after 30 s, and Chrome showed the resulting bare
"Internal Server Error" to the user. nginx holds idle connections itself and
passes gunicorn only complete requests. Because every request now arrives from
nginx, the visitor's address comes from `X-Forwarded-For`, trusted for exactly
one proxy (`DJANGO_NUM_PROXIES=1`, read by `core/network.py` and DRF's
throttles).
