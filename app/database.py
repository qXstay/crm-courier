from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings


def is_sqlite_database(url: str | None = None) -> bool:
    return (url or settings.database_url).startswith("sqlite")


def _is_in_memory_sqlite(url: str) -> bool:
    return url in {"sqlite", "sqlite://"} or url.endswith(":memory:")


# Флаг для текущего приложения: какой диалект настроен.
is_sqlite = is_sqlite_database()

# SQLite требует своей настройки пула/коннекта. PostgreSQL идёт прежней веткой.
_connect_args: dict = {}
_engine_kwargs: dict = {"pool_pre_ping": True}
if is_sqlite:
    _connect_args["check_same_thread"] = False
    # In-memory SQLite живёт только в рамках одного соединения, поэтому держим
    # одно соединение в пуле. Для файлового SQLite нужен обычный пул.
    if _is_in_memory_sqlite(settings.database_url):
        _engine_kwargs["poolclass"] = StaticPool

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    **_engine_kwargs,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
