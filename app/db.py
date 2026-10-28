from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


connect_args = {"check_same_thread": False, "timeout": 30} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def sqlite_connection(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from . import models  # noqa: F401

    if settings.auto_create_schema:
        Base.metadata.create_all(bind=engine)
    else:
        actual = inspect(engine)
        for table in Base.metadata.sorted_tables:
            if not actual.has_table(table.name) or not set(table.columns.keys()).issubset({c["name"] for c in actual.get_columns(table.name)}):
                raise RuntimeError("Database schema is incomplete; run the migration service first")
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

