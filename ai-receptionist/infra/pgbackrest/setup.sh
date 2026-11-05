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

need() { [[ -n ${!1:-} ]] || { echo "missing $1 (see the header of $0)" >&2; exit 2; }; }
for v in PGBACKREST_REPO1_S3_BUCKET PGBACKREST_REPO1_S3_ENDPOINT PGBACKREST_REPO1_S3_REGION \
         PGBACKREST_REPO1_S3_KEY PGBACKREST_REPO1_S3_KEY_SECRET PGBACKREST_REPO1_CIPHER_PASS; do
  need "$v"
done
if (( ${#PGBACKREST_REPO1_CIPHER_PASS} < 32 )); then
  echo "PGBACKREST_REPO1_CIPHER_PASS is shorter than 32 characters" >&2; exit 2
fi
[[ $(id -u) == 0 ]] || { echo "run as root" >&2; exit 2; }

STANZA=${BACKUP_STANZA:-receptionist}
PG_DATA=${PG_DATA:-/var/lib/postgresql/16/main}
PG_PORT=${PG_PORT:-5432}
REPO_PATH=${PGBACKREST_REPO1_PATH:-/receptionist}
URI_STYLE=${PGBACKREST_REPO1_S3_URI_STYLE:-host}
RETENTION_DAYS=${BACKUP_RETENTION_DAYS:-14}
CONF=/etc/pgbackrest

command -v pgbackrest >/dev/null || { echo "install pgbackrest first (apt install pgbackrest)" >&2; exit 2; }
install -d -m 750 -o postgres -g postgres /var/spool/pgbackrest /var/log/pgbackrest
install -d -m 755 "$CONF" && install -d -m 750 -o root -g postgres "$CONF/conf.d"

# Settings. Nothing secret in this file; it is safe to read.
cat > "$CONF/pgbackrest.conf" <<EOF
# Written by infra/pgbackrest/setup.sh. Secrets are in conf.d/secrets.conf.
[global]
repo1-type=s3
repo1-s3-bucket=$PGBACKREST_REPO1_S3_BUCKET
repo1-s3-endpoint=$PGBACKREST_REPO1_S3_ENDPOINT
repo1-s3-region=$PGBACKREST_REPO1_S3_REGION
repo1-s3-uri-style=$URI_STYLE
repo1-path=$REPO_PATH
${PGBACKREST_REPO1_STORAGE_CA_FILE:+repo1-storage-ca-file=$PGBACKREST_REPO1_STORAGE_CA_FILE}

# Encrypted on this host before upload; the bucket only ever holds ciphertext.
repo1-cipher-type=aes-256-cbc

# PITR window: keep full backups (and all WAL needed by them) for N days.
repo1-retention-full-type=time
repo1-retention-full=$RETENTION_DAYS

# Small files bundled, and block-level incrementals: daily backups upload
# changed blocks, not whole changed files.
repo1-bundle=y
repo1-block=y

compress-type=zst
compress-level=3
process-max=2
start-fast=y

# WAL is pushed asynchronously from a local spool, so a slow bucket never
# blocks commits. If the bucket is unreachable for long, the spool would
# grow until the disk fills and Postgres stops. archive-push-queue-max caps
# it: past 4 GiB, WAL is DROPPED (Postgres keeps running, PITR has a gap).
# That trades the recovery window for availability; the monitoring in
# backup-status.sh alerts long before the cap.
archive-async=y
spool-path=/var/spool/pgbackrest
archive-push-queue-max=4GiB
archive-get-queue-max=1GiB

log-path=/var/log/pgbackrest
log-level-console=info
log-level-file=detail

[$STANZA]
pg1-path=$PG_DATA
pg1-port=$PG_PORT
pg1-socket-path=/var/run/postgresql
EOF
chmod 644 "$CONF/pgbackrest.conf"

# Secrets: readable by root and the postgres group only.
umask 027
cat > "$CONF/conf.d/secrets.conf" <<EOF
[global]
repo1-s3-key=$PGBACKREST_REPO1_S3_KEY
repo1-s3-key-secret=$PGBACKREST_REPO1_S3_KEY_SECRET
repo1-cipher-pass=$PGBACKREST_REPO1_CIPHER_PASS
EOF
chown root:postgres "$CONF/conf.d/secrets.conf"; chmod 640 "$CONF/conf.d/secrets.conf"
umask 022

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
