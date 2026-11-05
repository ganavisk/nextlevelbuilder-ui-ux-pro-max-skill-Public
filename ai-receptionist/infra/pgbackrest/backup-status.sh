#!/usr/bin/env bash
# Is the database recoverable right now? For monitoring: run every few
# minutes as the postgres user and alert on a non-zero exit.
#
#   0  OK
#   1  WARNING  (e.g. WAL queue building up)
#   2  CRITICAL (no recent backup, archiving failing, repository unhealthy)
#
# Prints one line per check. Thresholds (environment):
#   MAX_BACKUP_AGE_HOURS   default 26 (a daily backup plus slack)
#   WARN_READY_SEGMENTS    default 16 (WAL waiting to be archived, x 16 MB)
#
# Docs: docs/runbooks/backup-and-restore.md
set -uo pipefail
STANZA=${BACKUP_STANZA:-receptionist}
MAX_AGE_H=${MAX_BACKUP_AGE_HOURS:-26}
WARN_READY=${WARN_READY_SEGMENTS:-16}
PG_PORT=${PG_PORT:-${PGPORT:-5432}}

status=0
report() {  # level message
  echo "$1  $2"
  case $1 in WARN) (( status < 1 )) && status=1 ;; CRIT) status=2 ;; esac
}

# 1. Repository health and the newest backup, from pgBackRest itself.
# Bounded: against an unreachable bucket pgBackRest retries for minutes, and
# a check that hangs during the outage it exists to report is no check.
info=$(timeout "${INFO_TIMEOUT_SECONDS:-30}" pgbackrest --stanza="$STANZA" --output=json info 2>&1)
rc=$?
if (( rc == 124 )); then
  report CRIT "repository did not answer within ${INFO_TIMEOUT_SECONDS:-30} s (bucket unreachable?)"
elif (( rc != 0 )); then
  report CRIT "pgbackrest info failed: ${info//$'\n'/ }"
else
  read -r code age label < <(python3 -c '
import json, sys, time
s = json.load(sys.stdin)[0]
backups = s.get("backup", [])
newest = max(backups, key=lambda b: b["timestamp"]["stop"]) if backups else None
age = (time.time() - newest["timestamp"]["stop"]) / 3600 if newest else -1
print(s["status"]["code"], f"{age:.1f}", newest["label"] if newest else "-")
' <<<"$info")
  if [[ $code != 0 ]]; then
    report CRIT "stanza status code $code (pgbackrest info for details)"
  else
    report OK "repository reachable, stanza healthy"
  fi
  if [[ $label == - ]]; then
    report CRIT "no backup in the repository"
  elif python3 -c "import sys; sys.exit(0 if float('$age') <= $MAX_AGE_H else 1)"; then
    report OK "newest backup $label, ${age} h old"
  else
    report CRIT "newest backup $label is ${age} h old (limit ${MAX_AGE_H} h)"
  fi
fi

# 2. WAL archiving, from Postgres: is it failing now, and how far behind?
row=$(psql -p "$PG_PORT" -XAt -F' ' -c "
  SELECT coalesce(last_failed_time > coalesce(last_archived_time, 'epoch'), false),
         coalesce(last_failed_wal, '-'),
         (SELECT count(*) FROM pg_ls_dir('pg_wal/archive_status') f WHERE f LIKE '%.ready')
  FROM pg_stat_archiver" 2>&1) || { report CRIT "can't query Postgres: $row"; echo; exit 2; }
read -r failing failed_wal ready <<<"$row"
if [[ $failing == t ]]; then
  report CRIT "WAL archiving is failing (last failed: $failed_wal); new writes are not recoverable"
else
  report OK "WAL archiving succeeding"
fi
if (( ready > WARN_READY )); then
  report WARN "$ready WAL segments waiting to be archived (~$(( ready * 16 )) MB)"
else
  report OK "$ready WAL segments waiting to be archived"
fi

exit $status
