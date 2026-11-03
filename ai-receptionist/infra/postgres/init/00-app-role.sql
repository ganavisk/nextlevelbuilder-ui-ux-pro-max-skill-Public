-- Runs once, as the bootstrap superuser, on first container start.
--
-- Why this file exists: PostgreSQL superusers bypass Row-Level Security
-- unconditionally — FORCE ROW LEVEL SECURITY does not apply to them. The
-- official image makes POSTGRES_USER a superuser, so an application that
-- connects as POSTGRES_USER gets *no* tenant isolation at all, while every
-- policy still looks correct in \d output. Dev would silently disagree with
-- production, and cross-tenant leaks would pass their own tests.
--
-- So the container bootstraps as `postgres` and the application connects as
-- `receptionist`, which is deliberately NOSUPERUSER NOBYPASSRLS. Keep it that
-- way in every environment: the RLS policies are the tenant boundary.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS vector;       -- pgvector, for knowledge embeddings
CREATE EXTENSION IF NOT EXISTS btree_gist;   -- per-bay no-overlap constraint (migration 0008)

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'receptionist') THEN
        CREATE ROLE receptionist LOGIN PASSWORD 'receptionist' NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$$;

-- The app owns its own tables (it runs the migrations that create them), so it
-- needs CREATE on the schema. Table ownership is fine: migration 0001 sets
-- FORCE ROW LEVEL SECURITY, which is what subjects the owner to its own
-- policies.
GRANT ALL ON DATABASE receptionist TO receptionist;
GRANT ALL ON SCHEMA public TO receptionist;
