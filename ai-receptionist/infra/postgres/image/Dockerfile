# Postgres 16 + pgvector + pgBackRest, for running the database yourself
# (e.g. a Railway service with a volume) with backups to an offsite bucket.
#
#   docker build -f infra/postgres/image/Dockerfile -t receptionist-postgres infra
#
# Ubuntu 24.04's own packages, pinned. The Debian-based official image would
# need the PostgreSQL apt repository for pgBackRest, and every version here
# is one the platform has been tested and restore-drilled on.
#
# Configuration, first boot, restores and backups:
# docs/runbooks/postgres-image.md
FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852

ARG POSTGRESQL_VERSION=16.15-0ubuntu0.24.04.1
ARG PGVECTOR_VERSION=0.6.0-1
ARG PGBACKREST_VERSION=2.50-1build2
ARG TINI_VERSION=0.19.0-1
ARG PYTHON3_VERSION=3.12.3-0ubuntu2.1

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PATH=/usr/lib/postgresql/16/bin:$PATH \
    PGDATA=/var/lib/postgresql/data/pgdata \
    PGPORT=5432

# create_main_cluster=false: the package would otherwise initdb a cluster
# into the image at install time. The entrypoint initializes the volume.
RUN mkdir -p /etc/postgresql-common/createcluster.d \
 && echo 'create_main_cluster = false' > /etc/postgresql-common/createcluster.d/no-main.conf \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      postgresql-16="$POSTGRESQL_VERSION" \
      postgresql-client-16="$POSTGRESQL_VERSION" \
      postgresql-16-pgvector="$PGVECTOR_VERSION" \
      pgbackrest="$PGBACKREST_VERSION" \
      tini="$TINI_VERSION" \
      python3="$PYTHON3_VERSION" \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && install -d -m 2775 -o postgres -g postgres /var/run/postgresql \
 && install -d -m 750 -o postgres -g postgres /var/spool/pgbackrest /var/log/pgbackrest

COPY pgbackrest/render-config.sh pgbackrest/backup.sh pgbackrest/backup-status.sh \
     postgres/bootstrap-managed.sql \
     postgres/image/entrypoint.sh postgres/image/scheduler.sh \
     /usr/local/lib/receptionist/
RUN chmod 755 /usr/local/lib/receptionist/*.sh \
 && ln -s /usr/local/lib/receptionist/backup-status.sh /usr/local/bin/backup-status \
 && ln -s /usr/local/lib/receptionist/backup.sh /usr/local/bin/backup

EXPOSE 5432
# Postgres's "fast" shutdown: roll back open transactions and stop cleanly,
# instead of waiting for clients to disconnect (SIGTERM, "smart").
STOPSIGNAL SIGINT
# As postgres over the socket (peer auth): a check as root would log
# 'FATAL: role "root" does not exist' every interval.
HEALTHCHECK --interval=10s --timeout=5s --start-period=60s --retries=3 \
  CMD setpriv --reuid=postgres --regid=postgres --init-groups -- \
      pg_isready -q -h /var/run/postgresql -p "$PGPORT" -U postgres -d postgres || exit 1

# The entrypoint starts as root only to take ownership of the volume, then
# runs everything as postgres. tini reaps and forwards signals.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/lib/receptionist/entrypoint.sh"]
CMD []
