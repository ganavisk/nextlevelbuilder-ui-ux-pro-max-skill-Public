#!/usr/bin/env bash
# Configure pgBackRest to back up and archive Postgres 16 to S3-compatible
# object storage (AWS S3, Cloudflare R2, Backblaze B2, MinIO...), encrypted
# before it leaves the machine. Then switch WAL archiving to it, create the
# stanza, check it end to end, and take the first full backup.
#
# Run as root on the database host. It's idempotent: re-run it to rotate
# keys or change settings (a cipher passphrase can't be changed for an
# existing repository, see docs/runbooks/backup-and-restore.md).
#
# Required environment (never put these in the repo, a shell history or a
# command line; use the host's secret store, or a root-only env file):
#   PGBACKREST_REPO1_S3_BUCKET      bucket, in a different region from the database
#   PGBACKREST_REPO1_S3_ENDPOINT    e.g. s3.eu-central-1.amazonaws.com, <acct>.r2.cloudflarestorage.com
#   PGBACKREST_REPO1_S3_REGION      e.g. eu-central-1 (R2: auto)
#   PGBACKREST_REPO1_S3_KEY         access key with read/write/list/delete on the bucket only
#   PGBACKREST_REPO1_S3_KEY_SECRET
#   PGBACKREST_REPO1_CIPHER_PASS    >= 32 random characters: openssl rand -base64 48
#                                   Store a copy OUTSIDE this host. Without it the
#                                   backups can't be read, by anyone, ever.
# Optional:
#   PGBACKREST_REPO1_S3_URI_STYLE   host (AWS, default) or path (R2, B2, MinIO)
#   PGBACKREST_REPO1_STORAGE_CA_FILE  CA bundle for a private endpoint
#   PGBACKREST_REPO1_PATH           default /receptionist
#   BACKUP_RETENTION_DAYS           PITR window in days, default 14
#   BACKUP_STANZA                   default receptionist
#   PG_DATA                         default /var/lib/postgresql/16/main
#   PG_PORT                         default 5432
set -euo pipefail

"$(dirname "$0")/render-config.sh"
STANZA=${BACKUP_STANZA:-receptionist}
PG_PORT=${PG_PORT:-${PGPORT:-5432}}

as_pg() { su postgres -s /bin/bash -c "$*"; }

as_pg "pgbackrest --stanza=$STANZA stanza-create"

# Point Postgres's archiving at pgBackRest. archive_mode needs a restart
# only if it was off.
was_on=$(as_pg "psql -p $PG_PORT -Atc 'show archive_mode'")
as_pg "psql -p $PG_PORT -q" <<SQL
ALTER SYSTEM SET archive_mode = 'on';
ALTER SYSTEM SET archive_timeout = '60s';
ALTER SYSTEM SET archive_command = 'pgbackrest --stanza=$STANZA archive-push %p';
SELECT pg_reload_conf();
SQL
if [[ $was_on != on ]]; then
  echo "archive_mode was off: restart Postgres now, then re-run this script" >&2
  exit 3
fi

# Proves the whole path: Postgres archives a segment through pgBackRest,
# and pgBackRest can read it back from the bucket.
as_pg "pgbackrest --stanza=$STANZA check"

if ! as_pg "pgbackrest --stanza=$STANZA info --output=json" | grep -q '"type":"full"'; then
  as_pg "pgbackrest --stanza=$STANZA --type=full backup"
fi
as_pg "pgbackrest --stanza=$STANZA info"
