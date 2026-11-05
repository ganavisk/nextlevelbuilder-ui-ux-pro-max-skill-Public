#!/usr/bin/env bash
# Validate the backup settings in the environment and write pgBackRest's
# configuration from them: /etc/pgbackrest/pgbackrest.conf (no secrets,
# world-readable) and conf.d/secrets.conf (640 root:postgres).
#
# Shared by setup.sh (a self-managed host) and the Postgres image's
# entrypoint (infra/postgres/image), so both always run the same settings.
# Run as root. The variables are listed in setup.sh's header.
set -euo pipefail

need() { [[ -n ${!1:-} ]] || { echo "missing $1 (see docs/runbooks/backup-and-restore.md)" >&2; exit 2; }; }
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
PG_PORT=${PG_PORT:-${PGPORT:-5432}}
REPO_PATH=${PGBACKREST_REPO1_PATH:-/receptionist}
URI_STYLE=${PGBACKREST_REPO1_S3_URI_STYLE:-host}
RETENTION_DAYS=${BACKUP_RETENTION_DAYS:-14}
CONF=/etc/pgbackrest

command -v pgbackrest >/dev/null || { echo "install pgbackrest first (apt install pgbackrest)" >&2; exit 2; }
install -d -m 750 -o postgres -g postgres /var/spool/pgbackrest /var/log/pgbackrest
install -d -m 755 "$CONF" && install -d -m 750 -o root -g postgres "$CONF/conf.d"

# Settings. Nothing secret in this file; it is safe to read.
cat > "$CONF/pgbackrest.conf" <<EOF
# Written by infra/pgbackrest/render-config.sh. Secrets are in conf.d/secrets.conf.
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
