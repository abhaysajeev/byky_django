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
