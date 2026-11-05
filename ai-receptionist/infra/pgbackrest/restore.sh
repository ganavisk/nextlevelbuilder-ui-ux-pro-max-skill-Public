#!/usr/bin/env bash
# Restore from the pgBackRest repository (the offsite bucket) into a NEW
# data directory, to a point in time or to the latest WAL in the bucket.
# Needs nothing from the old server: only pgBackRest's config and secrets.
#
#   restore.sh <target> <new-data-dir> [port]
#
#   <target>        a timestamp with time zone ("2026-11-05 01:31:15+00"),
#                   or "latest" for everything in the bucket (after losing the server)
#   <new-data-dir>  must not exist yet
#   [port]          default 5433
#
# Run as the postgres user. The copy starts with archiving OFF
# (--archive-mode=off), so it never writes a second timeline into the
# repository. When it is promoted to be the new primary, turn archiving back
# on and take a full backup at once (docs/runbooks/backup-and-restore.md).
# It listens on the local socket only, and prints the time taken.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  sed -n '2,18p' "$0" >&2
  exit 2
fi
TARGET=$1 DATA=$2 PORT=${3:-5433}
STANZA=${BACKUP_STANZA:-receptionist}
BIN=${PG_BIN:-/usr/lib/postgresql/16/bin}
[[ ! -e $DATA ]] || { echo "$DATA exists; refusing to overwrite it" >&2; exit 2; }

start=$(date +%s.%N)
install -d -m 700 "$DATA"

args=(--stanza="$STANZA" --pg1-path="$DATA" --archive-mode=off)
if [[ $TARGET != latest ]]; then
  # Without a target, recovery replays to the end of the archive and
  # promotes on its own; pgBackRest refuses --target-action there.
  args+=(--type=time --target="$TARGET" --target-action=promote)
fi
pgbackrest "${args[@]}" --log-level-console=warn restore

# Debian/Ubuntu keep postgresql.conf and pg_hba.conf outside the data
# directory, so a backup of one has none. Minimal ones for the copy. Roles
# other than postgres log in with their restored passwords.
cat > "$DATA/postgresql.conf" <<EOF
port = $PORT
listen_addresses = ''
unix_socket_directories = '/var/run/postgresql'
EOF
printf 'local all postgres peer\nlocal all all scram-sha-256\n' > "$DATA/pg_hba.conf"
: > "$DATA/pg_ident.conf"

"$BIN/pg_ctl" -D "$DATA" -l "$DATA/restore.log" -w -t 3600 start -o \
  "-c config_file=$DATA/postgresql.conf -c hba_file=$DATA/pg_hba.conf -c ident_file=$DATA/pg_ident.conf" >/dev/null

until [[ ! -f $DATA/recovery.signal ]] && \
      [[ $("$BIN/psql" -h /var/run/postgresql -p "$PORT" -Atc 'select pg_is_in_recovery()' 2>/dev/null) == f ]]; do
  if ! "$BIN/pg_ctl" -D "$DATA" status >/dev/null; then
    echo "restore FAILED; the server stopped. Last log lines:" >&2
    tail -5 "$DATA/restore.log" >&2
    exit 1
  fi
  sleep 0.2
done
end=$(date +%s.%N)

grep -E 'recovery stopping|redo done|selected new timeline|archive recovery complete' "$DATA/restore.log" \
  | sed 's/^[^]]*] /  /' || true
printf 'restored from the repository and promoted on port %s in %.1f s\n' "$PORT" "$(echo "$end - $start" | bc)"
