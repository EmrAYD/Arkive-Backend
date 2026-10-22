import os

# app.config içe aktarılmadan önce ayarlanmalı.
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("FIREBASE_PROJECT_ID", "arkive-test")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401
from app.auth import get_token_claims
from app.db import Base, enable_sqlite_foreign_keys, get_db
from app.main import app
from app.storage import get_optional_storage, get_storage


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def client(session_factory):
    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # TestClient'ı "with" olmadan kullanıyoruz: lifespan (Firebase init) çalışmaz.
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def login_as(client):
    """Belirli Firebase claim'leriyle giriş yapmış gibi davranır."""

    def _login(**claims):
        app.dependency_overrides[get_token_claims] = lambda: claims

    return _login


class FakeStorage:
    """RustFS yerine bellekte çalışan sahte depolama (testler ağa çıkmaz).

    `objects`: anahtar -> boyut. Test, istemcinin yüklemesini `storage.objects[key] = boyut` ile taklit eder.
    """

    def __init__(self):
        self.objects: dict[str, int] = {}
        self.deleted: list[str] = []
        self.last_put = None

    def presign_put(self, key, content_type, size, expires):
        self.last_put = {"key": key, "content_type": content_type, "size": size, "expires": expires}
        return f"https://fake-storage.test/put/{key}"

    def presign_get(self, key, filename, expires):
        return f"https://fake-storage.test/get/{key}?name={filename}"

    def object_size(self, key):
        return self.objects.get(key)

    def delete(self, key):
        self.objects.pop(key, None)
        self.deleted.append(key)


@pytest.fixture()
def storage(client):
    fake = FakeStorage()
    app.dependency_overrides[get_storage] = lambda: fake
    app.dependency_overrides[get_optional_storage] = lambda: fake
    return fake
