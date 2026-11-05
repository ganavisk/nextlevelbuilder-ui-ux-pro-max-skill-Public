#!/usr/bin/env bash
# Scheduled backup: full on Sundays (UTC), block-level incremental otherwise.
# Run daily as the postgres user, e.g. from cron:
#
#   17 3 * * *  postgres  /opt/receptionist/infra/pgbackrest/backup.sh >> /var/log/pgbackrest/cron.log 2>&1
#
# pgBackRest expires what the retention window (setup.sh) no longer needs
# after each backup, backups and WAL alike. Pass full|diff|incr to override.
#
# Docs: docs/runbooks/backup-and-restore.md
set -euo pipefail
STANZA=${BACKUP_STANZA:-receptionist}

type=${1:-}
if [[ -z $type ]]; then
  if [[ $(date -u +%u) == 7 ]]; then type=full; else type=incr; fi
fi
case $type in full|diff|incr) ;; *) echo "type must be full, diff or incr" >&2; exit 2 ;; esac

# An incremental with no full to build on is promoted to full by pgBackRest.
exec pgbackrest --stanza="$STANZA" --type="$type" backup
