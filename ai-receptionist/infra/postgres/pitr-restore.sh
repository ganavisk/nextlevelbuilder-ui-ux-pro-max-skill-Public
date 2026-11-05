#!/usr/bin/env bash
# Point-in-time restore of a self-managed Postgres 16 into a NEW data
# directory, next to the original. Never restores over a live cluster.
#
#   pitr-restore.sh <base-backup-dir> <wal-archive-dir> <target> <new-data-dir> [port]
#
#   <base-backup-dir>  a pg_basebackup -Ft -z directory (base.tar.gz, pg_wal.tar.gz)
#   <wal-archive-dir>  where archive_command puts WAL segments (plain or .gz)
#   <target>           a timestamp with time zone ("2026-11-04 18:31:15.55+00"),
#                      or "latest" for everything archived (after losing the server)
#   <new-data-dir>     must not exist yet
#   [port]             default 5433
#
# Run as the postgres OS user. The restored copy starts with archiving OFF:
# its base backup carries the source's postgresql.auto.conf, and a restored
# cluster that archived into the source's archive would mix a second
# timeline into it. Turn archiving on deliberately if the copy is promoted
# to be the new primary.
#
# Prints the seconds from start to "accepting connections, promoted". That
# is the measured restore time for this backup size.
#
# Docs: docs/runbooks/backup-and-restore.md
set -euo pipefail

if [[ $# -lt 4 ]]; then
  sed -n '2,24p' "$0" >&2
  exit 2
fi
BASE=$1 ARCHIVE=$2 TARGET=$3 DATA=$4 PORT=${5:-5433}
BIN=${PG_BIN:-/usr/lib/postgresql/16/bin}

[[ -f $BASE/base.tar.gz ]] || { echo "no base.tar.gz in $BASE" >&2; exit 2; }
[[ -d $ARCHIVE ]] || { echo "no WAL archive at $ARCHIVE" >&2; exit 2; }
[[ ! -e $DATA ]] || { echo "$DATA exists; refusing to overwrite it" >&2; exit 2; }

start=$(date +%s.%N)
install -d -m 700 "$DATA"
tar -xzf "$BASE/base.tar.gz" -C "$DATA"
[[ -f $BASE/pg_wal.tar.gz ]] && tar -xzf "$BASE/pg_wal.tar.gz" -C "$DATA/pg_wal"

# Debian/Ubuntu keep postgresql.conf and pg_hba.conf outside the data
# directory, so the backup has none. Give the copy minimal ones: local
# socket only, nothing listening on the network. Roles other than postgres
# log in with their restored passwords, so the app's own login can be tested.
cat > "$DATA/postgresql.conf" <<EOF
port = $PORT
listen_addresses = ''
unix_socket_directories = '/var/run/postgresql'
shared_preload_libraries = ''
EOF
cat > "$DATA/pg_hba.conf" <<'EOF'
local all postgres peer
local all all scram-sha-256
EOF
: > "$DATA/pg_ident.conf"

{
  # Segments may be archived gzipped (%f.gz) or plain (%f); read either.
  # shellcheck disable=SC2016  # $s is for the shell restore_command runs in
  printf 'restore_command = '\''s=%s/%%f; if [ -f "$s.gz" ]; then gunzip -c "$s.gz" > "%%p"; else cp "$s" "%%p"; fi'\''\n' "$ARCHIVE"
  if [[ $TARGET != latest ]]; then
    echo "recovery_target_time = '$TARGET'"
    echo "recovery_target_inclusive = on"
  fi
  echo "recovery_target_action = 'promote'"
} >> "$DATA/postgresql.conf"
touch "$DATA/recovery.signal"

# Command-line settings beat postgresql.auto.conf, which came with the backup.
"$BIN/pg_ctl" -D "$DATA" -l "$DATA/restore.log" -w -t 3600 start -o \
  "-c config_file=$DATA/postgresql.conf -c hba_file=$DATA/pg_hba.conf \
   -c ident_file=$DATA/pg_ident.conf -c archive_mode=off" >/dev/null

# Startup returns once connections are allowed. Promotion follows the end
# of replay, when recovery.signal is removed.
until [[ ! -f $DATA/recovery.signal ]] && \
      [[ $("$BIN/psql" -h /var/run/postgresql -p "$PORT" -Atc 'select pg_is_in_recovery()' 2>/dev/null) == f ]]; do
  # A target the archive can't reach shuts the server down rather than
  # promoting a copy that is short of what was asked for.
  if ! "$BIN/pg_ctl" -D "$DATA" status >/dev/null; then
    echo "restore FAILED; the server stopped. Last log lines:" >&2
    tail -5 "$DATA/restore.log" >&2
    exit 1
  fi
  sleep 0.2
done
end=$(date +%s.%N)

grep -E 'recovery stopping|redo done|selected new timeline|archive recovery complete|database system is ready' \
  "$DATA/restore.log" | sed 's/^[^]]*] /  /' || true
printf 'restored and promoted on port %s in %.1f s\n' "$PORT" "$(echo "$end - $start" | bc)"
