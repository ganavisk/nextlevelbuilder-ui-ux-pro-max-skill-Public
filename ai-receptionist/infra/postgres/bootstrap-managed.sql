-- One-time setup for a MANAGED Postgres (Railway, Neon, RDS...), run as the
-- provider's admin login. The compose stack does the equivalent in
-- init/00-app-role.sql.
--
--   psql "$ADMIN_DATABASE_URL" -v app_password="$(openssl rand -hex 24)" \
--        -f infra/postgres/bootstrap-managed.sql
--
-- Then point the application's DATABASE_URL at `receptionist_app`, never at the
-- admin login. Why this is not optional: managed providers hand out a
-- superuser, and superusers bypass row-level security unconditionally — every
-- policy still shows in \d, queries still succeed, and tenants can read each
-- other's data with no error. The API and workers check for this at startup in
-- staging/production and refuse to run (core/db.py: ensure_rls_enforced).
--
-- Idempotent: safe to re-run, e.g. to rotate the password.
-- Requires the pgvector extension to be installable on the server. A plain
-- Postgres service on some providers does not ship it — use their pgvector
-- image/template.

\set ON_ERROR_STOP on

SELECT format(
    'CREATE ROLE receptionist_app LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB',
    :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'receptionist_app')
\gexec

-- Re-assert the safety attributes every run, and rotate the password.
SELECT format(
    'ALTER ROLE receptionist_app PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB',
    :'app_password'
)
\gexec

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector, knowledge embeddings
CREATE EXTENSION IF NOT EXISTS btree_gist;   -- per-bay no-overlap constraint (migration 0008)

-- The app runs the migrations, so it owns its tables. Ownership is harmless
-- for isolation: migration 0001 sets FORCE ROW LEVEL SECURITY, which subjects
-- the owner to its own policies (only superuser/BYPASSRLS escape them).
SELECT format('GRANT ALL ON DATABASE %I TO receptionist_app', current_database())
\gexec
GRANT ALL ON SCHEMA public TO receptionist_app;
