# Model context — how work is done on this project

Read this first in a fresh session. It is the working agreement between the
owner and the coding assistant: the facts that are not obvious from the code,
the rules that always apply, and the exact workflow for every change.
Project-wide design rules live in `../CLAUDE.md` (the migration's prime
directive) and `../../CLAUDE.md` (UI rules); this file does not repeat them.

---

## 1. Facts

| Thing | Value |
|---|---|
| This repo | `github.com/abhaysajeev/byky_django` (public). Push over **SSH** (`git@github.com:…`). |
| Stack | Django 5.2, DRF, PostgreSQL 16, gunicorn (sync, 3 workers) behind nginx, WhiteNoise |
| Local dev | `docker-compose up` (v2.24 standalone binary) → http://localhost:8010, Postgres on 5434 |
| Test suite | ~1,000 tests, pytest-xdist (`-n 2`), ~3 min |
| **New QA VPS** | `97.74.81.23`, app `http://97.74.81.23:8007` (nginx container `proxy` → gunicorn `web`). Code in `/home/deploy/byky_django`, `.env` there, owned by user `deploy`. Log in as `rmsadmin`. |
| **Old QA VPS** | `108.181.201.77`, user `administrator` (sudo for docker). Host nginx owns 80/443 (sites: flash, silwhatsapp, logs). Old BYKY QA app retiring there; other projects run on it. |
| Log server | Project **log-ingestion** (`github.com/abhaysajeev/log_ingestion`, local `~/Desktop/2026/Log Ingestion/log-ingestion`) on the old VPS at `~/containers/log_ingestion`, **https://logs.softlandindia.net**, stack on `127.0.0.1:8095` behind host nginx. BYKY project slug `byky-qa`. Retention 45 days. |
| Log shipping | `apps/monitoring/shipper.py`; on when `LOG_INGEST_URL` + `LOG_INGEST_KEY` are in the new VPS `.env`. Never blocks a device call. |
| Containers (new VPS) | `byky_f_qa-web-1`, `byky_f_qa-proxy-1`, `byky_f_qa-db-1` (compose project `byky_f_qa`, file `docker-compose.qa.yml`) |

---

## 2. Standing rules (always)

1. **Never connect to a server (ssh/scp/rsync) without asking the owner first.**
   Server commands are handed to the owner to run; they paste the output back.
2. **`main` is protected** (PR + green `checks` and `image` required). Never push
   to `main`. Work on a branch, push it, give the owner the compare link; the
   owner merges after CI is green.
3. **Work in a git worktree**, never in the owner's own checkout. For this repo
   the session's worktree is under `.claude/worktrees/`; for another repo
   create one with `git worktree add` (see §4).
4. **Don't change code when the ask is "find the bug" / "explain" / "plan".**
   Investigate, prove with evidence, report; build only when asked.
5. **Never guess.** Verify against the code, the database, the logs or a
   reproduction before stating a cause. Say how confident, and what would
   confirm it.
6. **Legacy rules must cite their source proc** (`../CLAUDE.md` §1); a field
   only exists if the SRS/legacy data/procs show it. New fields asked by the
   owner are fine — record the reason in the model docstring.
7. **Secrets never go into chat or git.** `.env`, `config/projects.json`,
   passwords, tokens stay on the servers. If one was pasted in chat, advise
   rotating it (before first use when possible).
8. UI text is minimal: labels only, no explanations of the owner's reasoning
   on screens; Swagger descriptions short.
9. Keep the owner informed: one line on the approach before acting, a short
   status after each chunk, a clear summary at the end with links and the
   next command.

---

## 3. The workflow for a change

### 3.1 Understand before touching anything
- Restate what was asked; ask only if a real decision is the owner's.
- **Check what exists**: search the code for the feature, the model, the
  screen, the API, the tests (`grep -rn`), and read them. Reuse existing
  helpers and patterns (drawers in `apps/*/drawers.py`, list views,
  `byky-screen.css` classes, `core/api.py` envelopes, `core/network.py`,
  shared test fixtures such as `apps/fare/tests/test_api.py::world`).
- For legacy behaviour: `analysis/_procs/<Proc>.sql`, `analysis/_raw/*.json`,
  and the QA database via `.venv/bin/python tools/db.py "SELECT …"` (from
  `../`), to prove what data really says.
- For a bigger change, write the plan (what, which files, verification) and
  get agreement before building.

### 3.2 Branch
```bash
git status --short                 # must be clean
git fetch -q origin
git checkout -q -b <short-topic> origin/main
```

### 3.3 Build
- Match the surrounding code: naming, comment density, docstrings that say
  why; model changes get a migration via `makemigrations` (never hand-written
  unless unavoidable) and the SQL is read (`sqlmigrate`) to confirm it is safe
  for existing rows.
- Every change comes with tests next to the existing ones.

### 3.4 Check (the same as CI, run locally in a throwaway container)
The worktree is mounted into the dev image and run against the dev Postgres
(network `byky_django_default`, host `db`):
```bash
docker run --rm --network byky_django_default -v "<worktree>":/app -w /app -u 1000:1000 \
  -e HOME=/tmp -e DJANGO_SECRET_KEY=test -e POSTGRES_DB=byky -e POSTGRES_USER=byky \
  -e POSTGRES_PASSWORD=byky -e POSTGRES_HOST=db -e POSTGRES_PORT=5432 \
  --entrypoint sh byky_django-web -c "ruff check . \
    && python manage.py makemigrations --check --dry-run \
    && python manage.py spectacular --validate --fail-on-warn --file /tmp/s.yml \
    && pytest -p no:cacheprovider --create-db -q"
```
(Run only the affected test files while iterating; the whole suite before
pushing when shared code changed.) A flaky, order-dependent test is fixed in
the test, and the fix is proven by reproducing the failure first.

### 3.5 See it for real (screens, APIs, deploy-related changes)
Run the **QA compose file** locally under its own project name and empty
database, so the dev data is untouched:
```bash
export DJANGO_SECRET_KEY=local-test DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost POSTGRES_PASSWORD=local-test
docker-compose -f docker-compose.qa.yml -p byky_proxy_test up -d --build   # http://127.0.0.1:8007
docker exec byky_proxy_test-web-1 python manage.py create_admin --username <u> --password <p> --name "Test"
# … click through in Chrome, curl the APIs …
docker-compose -f docker-compose.qa.yml -p byky_proxy_test down -v && docker rmi byky_proxy_test-web
```
Screens are checked in Chrome (add → edit → save → list, errors shown); API
changes are checked against Swagger (`/api/docs/`, the example must match the
real response — `fare_api.shape()` tests lock this).

### 3.6 Commit and push
```bash
git add -A && git status --short          # nothing unexpected, no secrets
git commit -F - <<'EOF'
<Imperative summary line>

<What changed and why; how it was verified.>

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
git push -u origin <short-topic>
```
Give the owner: **`https://github.com/abhaysajeev/byky_django/compare/main...<short-topic>`**
(type it clean — the link git prints can carry trailing spaces).

### 3.7 CI → merge → deploy
- The PR runs `.github/workflows/ci.yml`: **checks** (ruff, migrations,
  schema, full pytest on Postgres 16) and **image** (production build,
  compose + nginx config validation).
- Check CI status without a login:
  `curl -s https://api.github.com/repos/abhaysajeev/byky_django/commits/<sha>/check-runs`
- If a run shows *cancelled*, a newer run on the same PR replaced it (or an
  old run was re-run) — the owner re-runs the run for the **latest** commit.
- Owner merges, then **GitHub → Actions → Deploy → Run workflow** (main).
  `deploy/deploy.sh` backs up the DB, builds, starts, health-checks through
  nginx; red = look at the printed web/proxy logs.

### 3.8 After deploy — commands for the owner (new VPS, as rmsadmin)
```bash
sudo docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' | grep byky_f_qa
sudo docker logs byky_f_qa-web-1 2>&1 | tail -50
sudo docker logs byky_f_qa-web-1 2>&1 | grep -c "WORKER TIMEOUT"     # should stay 0
sudo -u deploy nano /home/deploy/byky_django/.env                      # settings
sudo -u deploy bash -c 'cd ~/byky_django && docker compose -f docker-compose.qa.yml -p byky_f_qa up -d'
```
`.env` changes need `up -d` (recreates the container), **not** `restart`.

### 3.9 Clean up and report
Remove every throwaway stack, image, test user, temp file. End with: what
changed, where it is (branch/commit/link), how it was verified, the exact
next command or click for the owner.

---

## 4. The log-ingestion project (separate repo, no CI)

- Work in a worktree so the owner's checkout is untouched:
  ```bash
  git -C "<log-ingestion checkout>" fetch -q git@github.com:abhaysajeev/log_ingestion.git main:refs/remotes/origin/main
  git -C "<log-ingestion checkout>" worktree add -b <topic> <scratch dir> origin/main
  ```
- Test on a local stack: `.env` (random secrets, `COMPOSE_FILE=docker-compose.yml:docker-compose.proxied.yml`),
  `python3 scripts/add_project.py …`, `docker-compose -p logtest up -d --build`,
  then the end-to-end checks (auth, project isolation, every filter, export
  auth, large batch, TTL index). Tear down with `down -v`.
- Push over SSH: `git push git@github.com:abhaysajeev/log_ingestion.git <topic>`;
  owner merges at `https://github.com/abhaysajeev/log_ingestion/compare/main...<topic>`.
- Deploy (owner, old VPS, `~/containers/log_ingestion`):
  `git pull && sudo docker compose up -d --build` (+ `sudo docker compose restart nginx` if nginx conf changed).
- Projects/tokens: `python3 scripts/add_project.py <slug> --token <t>` (own
  dashboard token, ingest key unchanged) or `--rotate` (new key + link), then
  `sudo docker compose restart fastapi`. Ingest key must match `LOG_INGEST_KEY`
  on the new VPS.
- Host nginx edits on the old VPS: write the file with `nano` (long pasted
  lines get split), `sudo nginx -t` **before** `sudo systemctl reload nginx`.

---

## 5. Decisions already made (not visible in code)

| Decision | Detail |
|---|---|
| Chrome 500s fixed | Cause was gunicorn sync workers exposed directly (Chrome preconnect sockets → worker timeout → canned 500). Fix: nginx `proxy` in front (merged). Watch `WORKER TIMEOUT` count. |
| Vehicle type `is_direct_rent` | Fixed-time kids' rides billed before handover; legacy `ImsSubcategory.IsReturn` inverted (only DRIFT CAR). Returned per type in `/api/v1/{app}/vehicles`. Set it on Drift Car on the screen after deploy. |
| Device requests log | Local `request_log` 30 days (system admin screens); central copy 45 days on the log server; testers use the per-project token link. |
| Offline sync (for the coming transaction APIs) | Every offline-created record/event carries a tablet-generated UUID **`sync_id`** (name to confirm with the owner), resent unchanged; server unique `(device, sync_id)` kept permanently; batch replies per record `created` / `duplicate` / `rejected`; bill number stays unique too (catches different bills with the same number). Only on writes, never on downloads/login. |
| Open items | Media files (`/media/`) have no route yet; old QA app on the old VPS to be stopped once everyone uses the new VPS; domain + HTTPS for the BYKY app itself later. |
