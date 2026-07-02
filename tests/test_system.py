"""대시보드 시스템 메뉴(백업/복원·내보내기/가져오기) 테스트."""

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
    monkeypatch.setattr(config, "RESOURCES_DIR", root / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", root / "documents")
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


def test_document_limits_clamps_corrupt_setting(client):
    """오염·범위 밖 설정 값은 config 범위로 클램핑(또는 기본값)된다."""
    from chunchugwan import documents

    with db.connect() as conn:
        db.set_setting(conn, db.DOCUMENT_MAX_COUNT_KEY, "99999")  # 범위 초과
        db.set_setting(conn, db.DOCUMENT_MAX_MB_KEY, "쓰레기")      # 정수 아님
        lim = documents.limits(conn)
        assert lim.max_count == config.DOCUMENT_MAX_COUNT_MAX
        assert lim.max_bytes == config.DOCUMENT_MAX_MB_DEFAULT * 1024 * 1024


# ---- 관리자 게이트 (인증 on) ----


@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    """인증 on. 관리자 + 일반 사용자가 등록된 TestClient."""
    _patch_root(monkeypatch, tmp_path / "a")
    with db.connect() as conn:
        db.create_user(conn, "admin@test.co", auth.hash_password("adminpass123"), role="admin")
        db.create_user(conn, "user@test.co", auth.hash_password("userpass1234"))
    return TestClient(web_app.app)


def _login(client, email: str, password: str) -> None:
    res = client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )
    assert res.status_code == 303, res.text


# ---- 저장 공간 압축 ----


