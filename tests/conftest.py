import pytest
from fastapi.testclient import TestClient

from app import db


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setenv("FREIGHTDESK_DEMO", "1")


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)
