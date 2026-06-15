"""확장 배포 — /extension/download ZIP 서빙, 현황 안내 카드."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from chunchugwan import __version__, auth, config, db
from chunchugwan.web import app as web_app


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")


@pytest.fixture
def client(tmp_db):
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
    yield TestClient(web_app.app)


def _login(client):
    client.post("/login", data={"email": "boss@test.co", "password": "bosspass1234"},
                follow_redirects=False)


def test_download_requires_session(client):
    r = client.get("/extension/download", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/login" in r.headers["location"]


def test_download_serves_zip(client):
    _login(client)
    r = client.get("/extension/download")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    assert __version__ in r.headers["content-disposition"]

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert "manifest.json" in names
    assert "background.js" in names
    assert "popup.html" in names
    assert "popup.js" in names
    assert "_locales/ko/messages.json" in names
    assert "_locales/en/messages.json" in names
    # manifest version 은 서버 버전으로 맞춰진다
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["version"] == __version__
    assert manifest["manifest_version"] == 3
    # _locales 가 유효 JSON
    assert "tab_archive" in json.loads(zf.read("_locales/ko/messages.json"))


def test_dashboard_shows_extension_card(client):
    _login(client)
    body = client.get("/").text
    assert "/extension/download" in body
    assert "더 빠르고 편리하게" in body
