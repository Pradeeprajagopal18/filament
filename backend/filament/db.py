"""Database engine and session handling (SQLite by default, Postgres via FILAMENT_DATABASE_URL)."""

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from filament.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str, **kwargs):
    if url.startswith("sqlite"):
        kwargs.setdefault("connect_args", {"check_same_thread": False})
    eng = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        # pysqlite's own transaction handling breaks SAVEPOINTs, which agent runs use to roll
        # back a failed step without losing the rest of the run. Let SQLAlchemy own BEGIN.
        @event.listens_for(eng, "connect")
        def _connect(dbapi_connection, _record):
            dbapi_connection.isolation_level = None

        @event.listens_for(eng, "begin")
        def _begin(conn):
            conn.exec_driver_sql("BEGIN")

    return eng


engine = make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(bind=None) -> None:
    from filament import models  # noqa: F401  (register tables)

    Base.metadata.create_all(bind=bind or engine)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
