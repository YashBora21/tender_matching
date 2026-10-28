"""Alembic environment.

The database URL comes from application config (DATABASE_URL) by default, so
there is exactly one place where credentials live. An explicitly configured
`sqlalchemy.url` wins over it - the integration test points the migration at a
disposable database, and without this the migration would run against the
configured one while the test asserted on the other.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.repositories.schema import metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def get_url() -> str:
    """Configured URL if one was set, else the application's DATABASE_URL."""
    configured = config.get_main_option("sqlalchemy.url", None)
    return configured or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()

    # A connection given by the caller is used as it is, so a test can run the
    # migration inside a transaction and roll it back.
    existing = config.attributes.get("connection")
    if existing is not None:
        context.configure(
            connection=existing,
            target_metadata=target_metadata,
            compare_type=True,
            # Where the version table lives, when the caller says so. A test
            # that migrates a schema of its own must not read the version in
            # `public` and conclude there is nothing to do.
            version_table_schema=config.get_main_option("version_table_schema"),
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            version_table_schema=config.get_main_option("version_table_schema"),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
