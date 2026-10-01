from contextlib import contextmanager
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings


class Base(DeclarativeBase):
    pass


def _engine():
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    return create_engine(settings.database_url, pool_pre_ping=True, connect_args=args)


engine = _engine()
SessionLocal = sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db():
    from . import models  # noqa: F401
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        ))
        if engine.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(2058813401)"))
            installed = connection.execute(text(
                "SELECT version FROM schema_migrations WHERE version = 1"
            )).first()
            if not installed:
                for table, column in (
                    ("runs", "installation_id"), ("runs", "check_run_id"),
                    ("runs", "comment_id"), ("pull_request_settings", "comment_id"),
                ):
                    connection.execute(text(
                        f"ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT"
                    ))
                connection.execute(text(
                    "INSERT INTO schema_migrations (version) VALUES (1)"
                ))
