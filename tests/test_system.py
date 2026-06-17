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


def test_system_page_and_nav_link(client):
    res = client.get("/system")
    assert res.status_code == 200
    assert "전체 백업" in res.text and "가져오기" in res.text
    assert 'href="/system"' in client.get("/").text  # 헤더 메뉴 노출


def test_system_page_shows_version(client):
    from chunchugwan import __version__

    res = client.get("/system")
    assert res.status_code == 200
    assert "버전" in res.text
    assert f"v{__version__}" in res.text


def test_capture_settings_toggle_mobile_screenshot(client):
    """캡처 설정 — 모바일 스크린샷 활성화 토글이 설정에 반영된다 (기본 off)."""
    res = client.get("/system")
    assert "캡처 설정" in res.text
    assert "모바일 해상도 스크린샷도 함께 저장" in res.text
    with db.connect() as conn:
        assert db.mobile_screenshot_enabled(conn) is False  # 기본 off

    res = client.post(
        "/system/capture-settings",
        data={"mobile_screenshot_enabled": "on"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    with db.connect() as conn:
        assert db.mobile_screenshot_enabled(conn) is True
    assert "checked" in client.get("/system").text

    # 체크박스 미포함 = off
    res = client.post(
        "/system/capture-settings", data={}, follow_redirects=False
    )
    assert res.status_code == 303
    with db.connect() as conn:
        assert db.mobile_screenshot_enabled(conn) is False


def test_document_settings_defaults_and_save(client):
    """문서 아카이브 설정 — 기본 한도가 보이고, 유효 값을 저장하면 반영된다."""
    from chunchugwan import documents

    res = client.get("/system")
    assert "문서 아카이브 설정" in res.text
    with db.connect() as conn:
        lim = documents.limits(conn)  # 설정 없으면 config 기본값
        assert lim.max_count == config.DOCUMENT_MAX_COUNT_DEFAULT
        assert lim.max_bytes == config.DOCUMENT_MAX_MB_DEFAULT * 1024 * 1024
        assert lim.timeout_seconds == config.DOCUMENT_FETCH_TIMEOUT_DEFAULT

    res = client.post(
        "/system/document-settings",
        data={"document_max_count": 5, "document_max_mb": 10,
              "document_fetch_timeout": 45},
        follow_redirects=False,
    )
    assert res.status_code == 303 and "notice=" in res.headers["location"]
    with db.connect() as conn:
        lim = documents.limits(conn)
        assert lim.max_count == 5
        assert lim.max_bytes == 10 * 1024 * 1024
        assert lim.timeout_seconds == 45


def test_document_settings_rejects_out_of_range(client):
    """범위를 벗어난 값은 저장하지 않고 오류로 돌려보낸다."""
    from chunchugwan import documents

    res = client.post(
        "/system/document-settings",
        data={"document_max_count": config.DOCUMENT_MAX_COUNT_MAX + 1,
              "document_max_mb": 10, "document_fetch_timeout": 45},
        follow_redirects=False,
    )
    assert res.status_code == 303 and "error=" in res.headers["location"]
    with db.connect() as conn:
        assert db.get_setting(conn, db.DOCUMENT_MAX_COUNT_KEY) is None  # 미저장


def test_document_limits_clamps_corrupt_setting(client):
    """오염·범위 밖 설정 값은 config 범위로 클램핑(또는 기본값)된다."""
    from chunchugwan import documents

    with db.connect() as conn:
        db.set_setting(conn, db.DOCUMENT_MAX_COUNT_KEY, "99999")  # 범위 초과
        db.set_setting(conn, db.DOCUMENT_MAX_MB_KEY, "쓰레기")      # 정수 아님
        lim = documents.limits(conn)
        assert lim.max_count == config.DOCUMENT_MAX_COUNT_MAX
        assert lim.max_bytes == config.DOCUMENT_MAX_MB_DEFAULT * 1024 * 1024


def test_backup_download_and_restore_upload(client, tmp_path, monkeypatch):
    res = client.post("/system/backup")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/gzip"
    cd = res.headers["content-disposition"]
    assert "chunchugwan-backup-" in cd and ".ccg.backup" in cd
    payload = res.content

    _patch_root(monkeypatch, tmp_path / "b")  # 빈 루트로 전환 후 복원
    assert _page_count() == 0
    res = client.post(
        "/system/restore",
        files={"file": ("b.ccg.backup", io.BytesIO(payload), "application/gzip")},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "/system?notice=" in res.headers["location"]
    assert _page_count() == 1


def test_restore_rejects_non_backup_extension(client, tmp_path, monkeypatch):
    """복원 업로드 파일명이 .ccg.backup 가 아니면 내용을 읽기 전에 거부한다."""
    payload = client.post("/system/backup").content
    _patch_root(monkeypatch, tmp_path / "b")
    res = client.post(
        "/system/restore",
        files={"file": ("b.tar.gz", io.BytesIO(payload), "application/gzip")},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "error=" in res.headers["location"]
    assert _page_count() == 0


def test_export_download_and_import_upload(client, tmp_path, monkeypatch):
    payload = client.post("/system/export").content

    _patch_root(monkeypatch, tmp_path / "b")
    for _ in range(2):  # 두 번째는 멱등 스킵
        res = client.post(
            "/system/import", data={"mode": "merge"},
            files={"file": ("e.ccg.export", io.BytesIO(payload), "application/gzip")},
            follow_redirects=False,
        )
        assert res.status_code == 303
        assert "notice=" in res.headers["location"]
    assert _page_count() == 1
    res = client.get(res.headers["location"])  # 리다이렉트 따라가 notice 렌더링 확인
    assert "스킵 1" in res.text


def test_site_export_download_and_import(client, tmp_path, monkeypatch):
    """사이트 내보내기 — 그 사이트만 담긴 export 파일을 받아 가져올 수 있다."""
    _seed("https://other.org/page")
    with db.connect() as conn:
        site_id = db.get_site_by_key(conn, storage.site_key(URL))["id"]
    res = client.post(f"/sites/{site_id}/export")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/gzip"
    cd = res.headers["content-disposition"]
    assert "chunchugwan-export-example.com-" in cd and ".ccg.export" in cd

    _patch_root(monkeypatch, tmp_path / "b")
    up = client.post(
        "/system/import", data={"mode": "merge"},
        files={"file": ("e.ccg.export", io.BytesIO(res.content), "application/gzip")},
        follow_redirects=False,
    )
    assert up.status_code == 303 and "notice=" in up.headers["location"]
    with db.connect() as conn:
        assert db.get_page(conn, URL) is not None
        assert db.get_page(conn, "https://other.org/page") is None


def test_site_export_unknown_site_404(client):
    assert client.post("/sites/9999/export").status_code == 404


def test_restore_rejects_export_file(client, tmp_path, monkeypatch):
    payload = client.post("/system/export").content
    _patch_root(monkeypatch, tmp_path / "b")
    # 확장자 게이트는 통과(.ccg.backup)시키되 내용이 내보내기라 거부돼야 한다
    res = client.post(
        "/system/restore",
        files={"file": ("e.ccg.backup", io.BytesIO(payload), "application/gzip")},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "error=" in res.headers["location"]
    assert _page_count() == 0


def test_import_rejects_garbage(client):
    res = client.post(
        "/system/import", data={"mode": "merge"},
        files={"file": ("x.ccg.export", io.BytesIO(b"not a tar"), "application/gzip")},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "error=" in res.headers["location"]


def test_import_rejects_non_export_extension(client):
    """업로드 파일명이 .ccg.export 가 아니면 내용을 읽기 전에 거부한다."""
    payload = client.post("/system/export").content
    res = client.post(
        "/system/import", data={"mode": "merge"},
        files={"file": ("e.tar.gz", io.BytesIO(payload), "application/gzip")},
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
        db.create_user(conn, "admin@test.co", auth.hash_password("adminpass123"), role="admin")
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
    assert auth_client.post("/system/compact").status_code == 403
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


# ---- 저장 공간 압축 ----


def test_optimize_button_and_run(client):
    """시스템 메뉴의 저장공간 최적화 — 압축 변환 + 참조 백필 + 고아 정리.

    시드 스냅샷은 자원 참조가 인덱스되지 않아 처음에는 백필 대상으로 폼이
    노출되고, 실행이 끝나면 대상이 없어 버튼이 비활성화된다.
    """
    res = client.get("/system")
    assert "저장공간 최적화" in res.text
    assert 'action="/system/compact"' in res.text  # 시드 스냅샷 백필 대상

    snap_dir = (
        storage.page_dir("example.com", storage.url_to_slug(URL)) / "2026-06-01T00-00-00"
    )
    (snap_dir / "page.html").write_text("<html>본문</html>", encoding="utf-8")
    (snap_dir / "raw.html").write_text("<html>원본</html>", encoding="utf-8")
    (snap_dir / "meta.json").write_text("{}", encoding="utf-8")

    res = client.post("/system/compact")
    assert res.status_code == 200
    assert "최적화 완료: 변환 1/1개" in res.text
    assert "참조 백필 1개" in res.text
    assert (snap_dir / "page.html.gz").is_file()
    assert (snap_dir / "raw.html.gz").is_file()
    assert not (snap_dir / "page.html").exists()

    # 멱등 — 두 번째 실행은 대상이 없어 안내만, 버튼도 비활성화
    res = client.post("/system/compact")
    assert "최적화할 항목이 없습니다" in res.text
    assert 'action="/system/compact"' not in res.text


def test_optimize_without_snapshots(client, tmp_path, monkeypatch):
    _patch_root(monkeypatch, tmp_path / "empty")
    res = client.post("/system/compact")
    assert res.status_code == 200
    assert "최적화할 항목이 없습니다" in res.text
