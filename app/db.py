from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings



def enable_sqlite_foreign_keys(engine: Engine) -> None:
    """SQLite, yabancı anahtar kurallarını (ör. ON DELETE CASCADE) varsayılan olarak UYGULAMAZ.

    PostgreSQL'de bu kurallar her zaman açıktır. Geliştirmede SQLite kullanılırsa
    "klasör silinince alt klasörleri de silinsin" gibi kuralların aynı çalışması için
    her yeni bağlantıda bu ayar açılır.
    """
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# create_engine tembeldir: bağlantı ilk sorguda açılır.
engine = create_engine(settings.database_url, pool_pre_ping=True)
enable_sqlite_foreign_keys(engine)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
