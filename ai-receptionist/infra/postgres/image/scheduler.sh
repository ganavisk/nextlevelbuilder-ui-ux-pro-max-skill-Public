#!/usr/bin/env bash
# Backup scheduler inside the Postgres container, run as postgres:
# - once Postgres is up: create the stanza (idempotent), check the whole
#   archive path, and take a full backup if the repository has none (or
#   after a restore promoted to primary);
# - then backup.sh daily at BACKUP_AT (HH:MM UTC, default 03:17): full on
#   Sundays, block incremental otherwise;
# - every STATUS_EVERY_SECONDS (default 3600), log backup-status.sh's verdict,
#   prefixed "backup-status:", so log alerts can match "CRIT".
# Failures are logged and retried; they never stop Postgres.
set -uo pipefail

STANZA=${BACKUP_STANZA:-receptionist}
AT=${BACKUP_AT:-03:17}
EVERY=${STATUS_EVERY_SECONDS:-3600}
LIB=/usr/local/lib/receptionist
NEEDS_FULL=$PGDATA/.needs-full-backup

log() { echo "backup-scheduler: $*" >&2; }
nap() { sleep "$1" & wait $!; }          # interruptible by SIGTERM
trap 'log "stopping"; exit 0' TERM INT

[[ $AT =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { log "BACKUP_AT must be HH:MM (UTC), got $AT"; exit 1; }

until pg_isready -q -h /var/run/postgresql -p "$PGPORT"; do nap 2; done

# Retry until the repository is usable: a wrong key or an unreachable bucket
# at boot shouldn't need a restart once fixed, but it must be loud.
until pgbackrest --stanza="$STANZA" stanza-create && pgbackrest --stanza="$STANZA" check; do
  log "CRIT: the backup repository isn't usable yet; retrying in 5 minutes"
  nap 300
done

has_full() { pgbackrest --stanza="$STANZA" --output=json info | grep -q '"type":"full"'; }
if [[ -f $NEEDS_FULL ]] || ! has_full; then
  log "taking a full backup now"
  if "$LIB/backup.sh" full; then rm -f "$NEEDS_FULL"; else log "CRIT: full backup failed"; fi
fi

next_backup() {  # epoch seconds of the next BACKUP_AT
  local t; t=$(date -u -d "today $AT" +%s)
  (( t > $(date -u +%s) )) || t=$(date -u -d "tomorrow $AT" +%s)
  echo "$t"
}

due=$(next_backup)
log "next scheduled backup $(date -u -d "@$due" '+%F %R UTC')"
while true; do
  now=$(date -u +%s)
  if (( now >= due )); then
    if [[ -f $NEEDS_FULL ]]; then type=full; else type=; fi
    if "$LIB/backup.sh" $type; then rm -f "$NEEDS_FULL"; else log "CRIT: scheduled backup failed"; fi
    due=$(next_backup)
    log "next scheduled backup $(date -u -d "@$due" '+%F %R UTC')"
  fi
  "$LIB/backup-status.sh" 2>&1 | sed 's/^/backup-status: /' >&2
  now=$(date -u +%s)
  wait_s=$(( due - now < EVERY ? due - now : EVERY ))
  nap $(( wait_s > 1 ? wait_s : 1 ))
done
