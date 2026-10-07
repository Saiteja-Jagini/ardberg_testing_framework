"""Keep test records and artifact recovery checks separate from live reviews."""
import os
from tempfile import TemporaryDirectory

import pytest

_storage = TemporaryDirectory(prefix="ardberg-tests-")
os.environ["DATA_DIR"] = _storage.name
os.environ["DATABASE_URL"] = "sqlite:///" + _storage.name.replace("\\", "/") + "/tests.db"


@pytest.fixture(scope="session", autouse=True)
def isolated_database():
    from app.db import engine, init_db
    init_db()
    yield
    engine.dispose()
    _storage.cleanup()
