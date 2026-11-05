#!/usr/bin/env bash
# Entrypoint for the receptionist Postgres image. See
# docs/runbooks/postgres-image.md for every variable.
#
# On an empty volume it either initializes a new database or, with
# RESTORE_FROM_BACKUP set, restores one from the bucket. Then it runs
# Postgres in the foreground and the backup scheduler beside it, both as
# the postgres user. Extra arguments are passed to postgres (e.g.
# -c shared_buffers=1GB).
set -euo pipefail

export PGDATA=${PGDATA:-/var/lib/postgresql/data/pgdata}
export PGPORT=${PGPORT:-5432}
export PG_DATA=$PGDATA PG_PORT=$PGPORT   # what render-config.sh reads
BACKUP_MODE=${BACKUP_MODE:-required}
STANZA=${BACKUP_STANZA:-receptionist}
LIB=/usr/local/lib/receptionist
RESTORED_COPY=$PGDATA/RESTORED_COPY       # marker: a restore that must not back up
NEEDS_FULL=$PGDATA/.needs-full-backup     # marker: take a full backup first

log() { echo "receptionist-postgres: $*" >&2; }
as_pg() { setpriv --reuid=postgres --regid=postgres --init-groups -- "$@"; }

[[ $(id -u) == 0 ]] || { log "start the container as root; it drops to postgres itself"; exit 1; }

# ── backups: required unless explicitly turned off ──────────────────────
case $BACKUP_MODE in
  required)
    if ! "$LIB/render-config.sh"; then
      log "backups are required: set the PGBACKREST_REPO1_* variables, or"
      log "BACKUP_MODE=off for a database you can afford to lose."
      exit 1
    fi ;;
  off) log "WARNING: BACKUP_MODE=off. This database is NOT backed up." ;;
  *) log "BACKUP_MODE must be required or off"; exit 1 ;;
esac

# The volume's root may be owned by root (Railway, Docker); Postgres needs a
# directory of its own inside it. PGDATA is a subdirectory for that reason,
# and because a volume's lost+found makes initdb refuse its root.
install -d -m 700 -o postgres -g postgres "$PGDATA"
install -d -m 2775 -o postgres -g postgres /var/run/postgresql

# ── first boot: restore or initialize ───────────────────────────────────
if [[ ! -s $PGDATA/PG_VERSION ]]; then
  if [[ -n ${RESTORE_FROM_BACKUP:-} ]]; then
    [[ $BACKUP_MODE == required ]] || { log "RESTORE_FROM_BACKUP needs the bucket settings"; exit 1; }
    args=(--stanza="$STANZA" --pg1-path="$PGDATA")
    if [[ $RESTORE_FROM_BACKUP != latest ]]; then
      args+=(--type=time --target="$RESTORE_FROM_BACKUP" --target-action=promote)
    fi
    if [[ ${RESTORE_AS_PRIMARY:-false} == true ]]; then
      log "restoring ($RESTORE_FROM_BACKUP) as the NEW PRIMARY: it will archive and back up to the bucket"
    else
      args+=(--archive-mode=off)
      log "restoring ($RESTORE_FROM_BACKUP) as a COPY: no archiving, no backups"
    fi
    as_pg pgbackrest "${args[@]}" --log-level-console=info restore
    if [[ ${RESTORE_AS_PRIMARY:-false} == true ]]; then
      as_pg touch "$NEEDS_FULL"
    else
      as_pg touch "$RESTORED_COPY"
    fi
  else
    pw=${POSTGRES_PASSWORD:-}
    if (( ${#pw} < 16 )); then
      log "set POSTGRES_PASSWORD (16+ characters) for a new database"; exit 1
    fi
    log "initializing a new database in $PGDATA"
    pwfile=$(mktemp); chown postgres "$pwfile"; chmod 600 "$pwfile"
    printf '%s\n' "$pw" > "$pwfile"; unset pw
    # Data checksums: corruption is caught on read, and pgBackRest verifies
    # every page it backs up.
    as_pg initdb --data-checksums --encoding=UTF8 --locale=C.UTF-8 \
      --username=postgres --pwfile="$pwfile" \
      --auth-local=peer --auth-host=scram-sha-256 >/dev/null
    rm -f "$pwfile"
    printf '%s\n' "host all all 0.0.0.0/0 scram-sha-256" "host all all ::/0 scram-sha-256" \
      >> "$PGDATA/pg_hba.conf"

    db=${POSTGRES_DB:-receptionist}
    as_pg pg_ctl -D "$PGDATA" -w -o "-c listen_addresses='' -c archive_mode=off" start >/dev/null
    as_pg psql -v ON_ERROR_STOP=1 -q -d postgres -c "CREATE DATABASE \"$db\""
    as_pg psql -v ON_ERROR_STOP=1 -q -d "$db" \
      -c 'CREATE EXTENSION IF NOT EXISTS pgcrypto' \
      -c 'CREATE EXTENSION IF NOT EXISTS vector' \
      -c 'CREATE EXTENSION IF NOT EXISTS btree_gist'
    if [[ -n ${APP_DB_PASSWORD:-} ]]; then
      # The restricted role the application connects as (never postgres).
      as_pg psql -v ON_ERROR_STOP=1 -q -d "$db" -v app_password="$APP_DB_PASSWORD" \
        -f "$LIB/bootstrap-managed.sql" >/dev/null
      log "created role receptionist_app in $db"
    fi
    as_pg pg_ctl -D "$PGDATA" -w -m fast stop >/dev/null
    log "database $db initialized"
  fi
fi

# ── run ─────────────────────────────────────────────────────────────────
args=(-D "$PGDATA" -c listen_addresses='*')
backups=false
if [[ -f $RESTORED_COPY ]]; then
  log "this is a RESTORED COPY: archiving off, no backups (see the runbook to promote it)"
  args+=(-c archive_mode=off)
elif [[ $BACKUP_MODE == required ]]; then
  backups=true
  args+=(-c archive_mode=on -c archive_timeout=60s
         -c "archive_command=pgbackrest --stanza=$STANZA archive-push %p")
else
  args+=(-c archive_mode=off)
fi

# setpriv execs its program, so these PIDs are Postgres's and the
# scheduler's own. (Backgrounding the as_pg function would fork a subshell,
# and a stop signal would reach bash instead of Postgres.)
drop=(setpriv --reuid=postgres --regid=postgres --init-groups --)
"${drop[@]}" postgres "${args[@]}" "$@" &
pg=$!
sched=
if $backups; then
  "${drop[@]}" "$LIB/scheduler.sh" &
  sched=$!
fi

# A stop signal becomes Postgres's fast shutdown; the scheduler just stops.
stop() {
  kill -INT "$pg" 2>/dev/null || true
  [[ -n $sched ]] && kill -TERM "$sched" 2>/dev/null || true
}
trap stop INT TERM

set +e
wait "$pg"; rc=$?
while kill -0 "$pg" 2>/dev/null; do wait "$pg"; rc=$?; done
[[ -n $sched ]] && { kill -TERM "$sched" 2>/dev/null; wait "$sched" 2>/dev/null; }
log "postgres exited ($rc)"
exit "$rc"
