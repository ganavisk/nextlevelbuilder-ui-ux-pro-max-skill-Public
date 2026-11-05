# One image, five roles. Railway runs the same build as the API, the three
# queue workers, and the pre-deploy migration; the role is the first argument
# (see docker-entrypoint.sh). One image means the API and its workers can never
# be deployed from different code.
#
# Build context: apps/api

FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /build

# Layer 1: third-party dependencies only. Rebuilt when pyproject.toml or the
# lock changes, not on every source edit. requirements.lock pins every
# transitive version — floating versions already broke migrations once
# (SQLAlchemy 2.1 changed the default driver for a bare postgresql:// URL).
COPY pyproject.toml requirements.lock ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > requirements.txt \
 && pip install -r requirements.txt -c requirements.lock

# Layer 2: the application itself.
COPY src ./src
RUN pip install --no-deps .


FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

# Unprivileged: a compromise of the API should not be root in the container.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0555 /usr/local/bin/docker-entrypoint.sh

USER app
EXPOSE 8000

# The API role answers /healthz. The worker roles have no HTTP surface, so the
# platform's own restart policy supervises them instead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["docker-entrypoint.sh", "healthcheck"]

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["api"]
