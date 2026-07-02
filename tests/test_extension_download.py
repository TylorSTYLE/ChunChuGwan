"""확장 배포 — /extension/download ZIP 서빙, 현황 안내 카드."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db
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
    yield TestClient(web_app.app, headers={"X-Requested-With": "fetch"})


def _login(client):
    client.post("/api/web/auth/login",
                json={"email": "boss@test.co", "password": "bosspass1234"})


def test_download_requires_session(client):
    # C2 컷오버: 자원 라우트는 미인증 시 401(로그인 리다이렉트 대신 — _require_viewer 가드).
    r = client.get("/extension/download", follow_redirects=False)
    assert r.status_code == 401


def test_download_serves_zip(client):
    from chunchugwan.web.api_routes import _extension_version
    ext_ver = _extension_version()  # 확장 정본 버전 (서버 앱 버전과 독립)
    _login(client)
    r = client.get("/extension/download")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    assert ext_ver in r.headers["content-disposition"]

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert "manifest.json" in names
    assert "background.js" in names
    assert "popup.html" in names
    assert "popup.js" in names
    assert "_locales/ko/messages.json" in names
    assert "_locales/en/messages.json" in names
    # manifest version 은 확장 정본 그대로 — 서버 버전으로 덮어쓰지 않는다
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["version"] == ext_ver
    assert manifest["manifest_version"] == 3
    # 아이콘이 함께 묶이고 manifest 가 참조한다 (툴바·확장 목록 아이콘)
    for size in ("16", "32", "48", "128"):
        assert f"icons/icon{size}.png" in names
        assert manifest["icons"][size] == f"icons/icon{size}.png"
        assert manifest["action"]["default_icon"][size] == f"icons/icon{size}.png"
    assert zf.read("icons/icon16.png")[:8] == b"\x89PNG\r\n\x1a\n"  # 실제 PNG
    # _locales 가 유효 JSON
    assert "tab_connect" in json.loads(zf.read("_locales/ko/messages.json"))
