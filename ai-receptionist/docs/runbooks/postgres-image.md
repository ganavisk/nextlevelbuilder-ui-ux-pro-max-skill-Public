# Runbook: the Postgres image (pgvector + pgBackRest)

`infra/postgres/image` builds the database for running Postgres yourself. A typical home is a Railway service with a volume, with point-in-time recovery to an offsite bucket. Use it when the managed Postgres you'd otherwise choose has no point-in-time recovery. How the backups themselves work is in `backup-and-restore.md`.

```bash
docker build -f infra/postgres/image/Dockerfile -t receptionist-postgres infra
```

## What's in it

| | Version | Why this one |
|---|---|---|
| Base | Ubuntu 24.04, pinned by digest | Its own packages carry all three pieces below |
| PostgreSQL | 16.15 (`16.15-0ubuntu0.24.04.1`) | Same major version as dev and CI |
| pgvector | 0.6.0 | Ubuntu's package. CI runs the full suite against this image (job `postgres-image`) |
| pgBackRest | 2.50 | The version the restore drills ran on |
| tini | 0.19 | PID 1: reaps processes and forwards stop signals |

Every version is pinned, so a rebuild can't change the engine silently. Bump them on purpose, in the Dockerfile's `ARG`s, and CI re-tests.

**pgvector 0.6 vs. the dev and CI default (0.8).** The app needs neither 0.8's iterative scans nor any index: retrieval is exact and per tenant (`PHASE-8-PRODUCTION.md`, §3.2). If that ever changes, the image needs pgvector from the PostgreSQL apt repository. That repository was unreachable from where this was built.

## Configuration

**Backups:** `PGBACKREST_REPO1_*` exactly as in `backup-and-restore.md`:
- bucket, endpoint, region, key, key secret, cipher passphrase;
- `..._S3_URI_STYLE=path` for R2, B2 and MinIO.

Without them the container **refuses to start**.

**Database:**

| Variable | Default | |
|---|---|---|
| `POSTGRES_PASSWORD` | (required on first boot) | The `postgres` superuser's password, 16+ characters. Only for admin work; the app never uses it. |
| `APP_DB_PASSWORD` | unset | If set on first boot, creates `receptionist_app`, the restricted role the app connects as. Otherwise run `bootstrap-managed.sql` yourself. |
| `POSTGRES_DB` | `receptionist` | |
| `PGDATA` | `/var/lib/postgresql/data/pgdata` | Mount the volume at `/var/lib/postgresql/data`. PGDATA is a subdirectory because a volume's root holds `lost+found`, which `initdb` refuses. |
| `PGPORT` | `5432` | |
| `BACKUP_MODE` | `required` | `off` runs with no backups, for a database you can afford to lose (and logs a warning on every boot). |
| `BACKUP_AT` | `03:17` | Daily backup time, UTC: full on Sundays, incremental otherwise. |
| `BACKUP_RETENTION_DAYS` | `14` | The recovery window. |
| `STATUS_EVERY_SECONDS` | `3600` | How often the backup status is logged. |
| `RESTORE_FROM_BACKUP` | unset | On an **empty** volume: `latest` or a timestamp. See below. |
| `RESTORE_AS_PRIMARY` | `false` | With `RESTORE_FROM_BACKUP`: `true` makes the restore the new primary. |

Extra container arguments go to `postgres`, e.g. `-c shared_buffers=1GB -c max_connections=200`. The defaults suit a small instance. Size `shared_buffers` at about 25% of the service's memory, and keep `max_connections` above the connection budget in `infra/railway/README.md`.

## First boot

On an empty volume, the image:
1. runs `initdb` with **data checksums on** and SCRAM passwords;
2. creates the database and the `vector`, `btree_gist` and `pgcrypto` extensions;
3. creates `receptionist_app` if `APP_DB_PASSWORD` is set.

Postgres then starts with archiving to the bucket. The built-in scheduler:
- creates the pgBackRest stanza;
- runs `check`, which archives a segment and reads it back;
- takes the first full backup.

Then point the API's `migrate` step at it, as `receptionist_app`.

**If it seems stuck:** the log says `backup-scheduler: CRIT: the backup repository isn't usable yet` every 5 minutes. Postgres is serving, but nothing is backed up yet. Fix the bucket settings, and it continues by itself.

## Running it

- **Processes:** Postgres and the scheduler run as `postgres`. Only tini and the entrypoint shell are root, and they exist to take ownership of the volume and to forward signals.
- **Stopping:** a stop is Postgres's fast shutdown. Open transactions roll back, the final WAL segment is pushed to the bucket, and the container exits 0. That takes 0.4 s idle.
- **Monitoring:** the scheduler logs `backup-status:` lines every hour. Alert on log lines containing `CRIT`. To check by hand:

  ```bash
  docker exec -u postgres <container> backup-status       # exit 0 / 1 / 2
  docker exec -u postgres <container> backup full          # an extra backup now
  ```

- **Healthcheck:** `pg_isready` as `postgres` over the socket.
- **Network:** Postgres listens on all addresses, IPv4 and IPv6, with password (SCRAM) logins. Expose it on a private network only. On Railway, that means no public TCP proxy.

## Restoring

On a **new service with an empty volume**, using the same bucket variables.

### A copy, for a drill or to rescue one business's data

```
RESTORE_FROM_BACKUP=2026-11-05 01:49:35+00     # or: latest
```

The copy restores, replays up to the target, and promotes. It **never archives or backs up**, so it can't write a second timeline into the repository. It stays that way across restarts, and the log says so on every boot. Verify it with `scripts/verify_restore.py`; the procedure is in `backup-and-restore.md`.

### The new primary, after losing the old one

1. **Make sure the old primary is gone for good.** Delete its service, not just stop it. Two primaries with the same system ID would both write backups into one repository, and "latest" could become the wrong server's.
2. Start the new service with:

   ```
   RESTORE_FROM_BACKUP=latest
   RESTORE_AS_PRIMARY=true
   ```

   It restores, starts archiving on the new timeline, and takes a **full backup at once**. The recovery window protects it from then on.
3. Point `DATABASE_URL` at it (cutover steps in `backup-and-restore.md`).

After the first boot, the `RESTORE_*` variables are ignored, because the volume isn't empty any more. Remove them anyway, so a future empty volume doesn't restore by surprise.

## Upgrading

- **A minor version** (16.15 to 16.x): bump the pinned versions, let CI pass, and redeploy. The data directory is compatible.
- **A major version** (16 to 17): not supported in place by this image. Plan it with `pg_upgrade` from a one-off container that has both versions, or with a dump and restore into a new service. Take a full backup first, and repeat the restore drill afterwards.

## Drill log

**2026-11-05: image `receptionist-postgres` (PostgreSQL 16.15, pgvector 0.6.0, pgBackRest 2.50).** The bucket was the S3 stand-in described in `backup-and-restore.md`.

| Check | Result |
|---|---|
| Refuses to start with no backup settings, a short passphrase, or no password | ✅ clear message for each |
| First boot to healthy, with stanza, `check` and a first full backup | **8–10 s** |
| Full API test suite against the image, as `receptionist_app` | **517 passed** |
| Load test through the real webhook path against it | 150 calls, 0 errors, 0 bay overlaps |
| Restore copy (new container, empty volume) to just before a deletion | Serving in **7 s**. `verify --against` **25/25**. Archiving off, no scheduler, and both still true after a restart. |
| Primary stop | **0.4 s**, exit 0, final WAL pushed |
| New primary from `latest` (old one stopped) | **12 s** to serving, on timeline 2, archiving, its own full backup taken. A restart neither restores again nor takes another full backup. Structural verify **8/8**. |
| `infra/scripts/smoke-postgres-image.sh` | 9/9 (also run in CI) |

Defects found while building it, all fixed:
- the healthcheck logged `FATAL: role "root"` every 10 s;
- the stop signal reached a bash subshell instead of Postgres;
- the password check crashed under `set -u`;
- `verify_restore.py` falsely failed an empty database.
