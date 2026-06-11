"""대시보드 시스템 메뉴(백업/복원·내보내기/가져오기) 테스트."""
import io

import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, storage
from chunchugwan.web import app as web_app

URL = "https://example.com/post"


def _patch_root(monkeypatch, root):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", root)
    monkeypatch.setattr(config, "SITES_DIR", root / "sites")
    monkeypatch.setattr(config, "DB_PATH", root / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", root / "cache")
    monkeypatch.setattr(config, "RULES_PATH", root / "rules.json")


def _seed(url: str = URL) -> None:
    """현재 루트에 페이지 1개 + 스냅샷 1개(content.md 포함) 구성."""
    domain, slug = url.split("/")[2], storage.url_to_slug(url)
    dir_name = "2026-06-01T00-00-00"
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, url, domain, slug)
        snap_dir = storage.page_dir(domain, slug) / dir_name
        snap_dir.mkdir(parents=True)
        (snap_dir / "content.md").write_text("본문", encoding="utf-8")
        db.insert_snapshot(
            conn, page_id, taken_at="2026-06-01T00:00:00+00:00", dir_name=dir_name,
            content_hash=storage.content_sha256("본문"), final_url=url,
            http_status=200, changed=1,
        )


def _page_count() -> int:
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM pages").fetchone()["c"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """인증 off(loopback 가정) + 데이터가 있는 루트 A 위의 TestClient."""
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    _patch_root(monkeypatch, tmp_path / "a")
    _seed()
    return TestClient(web_app.app)


def test_system_page_and_nav_link(client):
    res = client.get("/system")
    assert res.status_code == 200
    assert "전체 백업" in res.text and "가져오기" in res.text
    assert 'href="/system"' in client.get("/").text  # 헤더 메뉴 노출


def test_backup_download_and_restore_upload(client, tmp_path, monkeypatch):
    res = client.post("/system/backup")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/gzip"
    assert "chunchugwan-backup-" in res.headers["content-disposition"]
    payload = res.content

    _patch_root(monkeypatch, tmp_path / "b")  # 빈 루트로 전환 후 복원
    assert _page_count() == 0
    res = client.post(
        "/system/restore",
        files={"file": ("b.tar.gz", io.BytesIO(payload), "application/gzip")},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "/system?notice=" in res.headers["location"]
    assert _page_count() == 1


def test_export_download_and_import_upload(client, tmp_path, monkeypatch):
    payload = client.post("/system/export").content

    _patch_root(monkeypatch, tmp_path / "b")
    for _ in range(2):  # 두 번째는 멱등 스킵
        res = client.post(
            "/system/import", data={"mode": "merge"},
            files={"file": ("e.tar.gz", io.BytesIO(payload), "application/gzip")},
            follow_redirects=False,
        )
        assert res.status_code == 303
        assert "notice=" in res.headers["location"]
    assert _page_count() == 1
    res = client.get(res.headers["location"])  # 리다이렉트 따라가 notice 렌더링 확인
    assert "스킵 1" in res.text


def test_restore_rejects_export_file(client, tmp_path, monkeypatch):
    payload = client.post("/system/export").content
    _patch_root(monkeypatch, tmp_path / "b")
    res = client.post(
        "/system/restore",
        files={"file": ("e.tar.gz", io.BytesIO(payload), "application/gzip")},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "error=" in res.headers["location"]
    assert _page_count() == 0


def test_import_rejects_garbage(client):
    res = client.post(
        "/system/import", data={"mode": "merge"},
        files={"file": ("x.tar.gz", io.BytesIO(b"not a tar"), "application/gzip")},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "error=" in res.headers["location"]


def test_import_rejects_unknown_mode(client):
    res = client.post(
        "/system/import", data={"mode": "evil"},
        files={"file": ("x.tar.gz", io.BytesIO(b""), "application/gzip")},
    )
    assert res.status_code == 400


# ---- 관리자 게이트 (인증 on) ----


@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    """인증 on. 관리자 + 일반 사용자가 등록된 TestClient."""
    _patch_root(monkeypatch, tmp_path / "a")
    with db.connect() as conn:
        db.create_user(conn, "admin@test.co", auth.hash_password("adminpass123"), is_admin=True)
        db.create_user(conn, "user@test.co", auth.hash_password("userpass1234"))
    return TestClient(web_app.app)


def _login(client, email: str, password: str) -> None:
    res = client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )
    assert res.status_code == 303, res.text


def test_system_requires_admin(auth_client):
    _login(auth_client, "user@test.co", "userpass1234")
    assert auth_client.get("/system").status_code == 403
    assert auth_client.post("/system/backup").status_code == 403
    assert 'href="/system"' not in auth_client.get("/").text  # 메뉴도 숨김


def test_system_allows_admin(auth_client):
    _login(auth_client, "admin@test.co", "adminpass123")
    assert auth_client.get("/system").status_code == 200
    assert 'href="/system"' in auth_client.get("/").text
    assert auth_client.post("/system/backup").status_code == 200


def test_system_requires_login(auth_client):
    res = auth_client.get("/system", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"].startswith("/login")
