"""Migrate explicit versions; never blindly stamp an unknown nonempty database."""
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from app.db import engine, Base
from app import models  # noqa: F401


def main():
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if tables and "alembic_version" not in tables:
        raise RuntimeError("Unversioned nonempty database: do not auto-stamp; restore a verified migrated snapshot")
    if "alembic_version" in tables:
        with engine.connect() as connection:
            versions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        if not versions and tables - {"alembic_version"}:
            raise RuntimeError("Nonempty database has no migration revision")
    command.upgrade(Config("alembic.ini"), "head")
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not set(table.columns.keys()).issubset({c["name"] for c in inspector.get_columns(table.name)}):
            raise RuntimeError("Schema verification failed after migration")
    if engine.dialect.name == "sqlite":
        with engine.connect() as connection:
            connection.execute(text("PRAGMA journal_mode=WAL"))
            if connection.execute(text("PRAGMA integrity_check")).scalar_one() != "ok":
                raise RuntimeError("SQLite integrity check failed")
    print("Schema migrations and integrity check completed")


if __name__ == "__main__":
    main()
