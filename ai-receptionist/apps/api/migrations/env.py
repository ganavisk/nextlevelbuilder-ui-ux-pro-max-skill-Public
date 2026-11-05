"""Alembic environment. Uses the app's DATABASE_URL (sync driver for
migrations — Alembic runs psycopg-style, the app runs asyncpg)."""

from alembic import context
from sqlalchemy import create_engine

from app.core.config import get_settings


def _sync_url() -> str:
    # asyncpg URL -> sync psycopg URL for Alembic's offline/online runs
    return get_settings().database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)


def run_migrations_offline() -> None:
    context.configure(url=_sync_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_sync_url())
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
