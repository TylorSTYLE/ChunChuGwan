"""세분 권한(하이브리드) — 역할 프리셋 + 사용자별 오버라이드.

역할은 권한 묶음(프리셋)이고, 사용자별 permission_overrides 로 개별 가감한다.
프리셋만으로는 기존 역할 체계와 동작이 동일하고, 오버라이드를 줄 때만 달라진다.
"""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db
from chunchugwan.web import app as web_app
from chunchugwan.web import permissions


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """임시 아카이브 DB 환경 (인증 on)."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")


@pytest.fixture
def client(tmp_db):
    """founder 관리자 + 역할별 사용자."""
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        for email, role in (
            ("archiver@test.co", "archiver"),
            ("viewer@test.co", "viewer"),
        ):
            db.create_user(conn, email, auth.hash_password("password1234"), role=role)
    return TestClient(web_app.app)


def _login(client, email: str, password: str = "password1234"):
    return client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )


def _uid(email: str) -> int:
    with db.connect() as conn:
        return db.get_user_by_email(conn, email)["id"]


# ---- DB 계층: 프리셋이 기존 역할 동작을 재현 ----


def test_presets_reproduce_role_behaviour(tmp_db):
    assert db.effective_permissions("viewer") == frozenset({"view"})
    assert db.effective_permissions("archiver") == frozenset({"view", "archive", "delete"})
    assert db.effective_permissions("admin") == frozenset(db.PERMISSIONS)
    for inactive in ("pending", "blocked", "withdrawn"):
        assert db.effective_permissions(inactive) == frozenset()


def test_overrides_add_and_remove(tmp_db):
    # archiver 에서 삭제만 제거
    assert db.effective_permissions("archiver", '{"delete": false}') == frozenset(
        {"view", "archive"}
    )
    # viewer 에게 아카이브 추가
    assert db.effective_permissions("viewer", '{"archive": true}') == frozenset(
        {"view", "archive"}
    )


def test_parse_overrides_ignores_garbage(tmp_db):
    assert db.parse_permission_overrides(None) == {}
    assert db.parse_permission_overrides("") == {}
    assert db.parse_permission_overrides("not json") == {}
    assert db.parse_permission_overrides("[1,2]") == {}
    # 알 수 없는 권한 키는 버린다
    assert db.parse_permission_overrides('{"delete": true, "root": true}') == {
        "delete": True
    }


def test_set_permission_overrides_persists_and_skips_founder(tmp_db):
    with db.connect() as conn:
        founder = db.create_first_admin(conn, "f@b.co", "x")
        uid = db.create_user(conn, "a@b.co", role="archiver")
        db.set_permission_overrides(conn, uid, {"delete": False})
        assert db.get_user_by_id(conn, uid)["permission_overrides"] == '{"delete": false}'
        # 최초 관리자는 오버라이드도 거부 (행이 안 바뀐다)
        db.set_permission_overrides(conn, founder, {"manage_users": False})
        assert db.get_user_by_id(conn, founder)["permission_overrides"] == "{}"


def test_count_active_users_with_permission(tmp_db):
    with db.connect() as conn:
        a = db.create_user(conn, "a@b.co", role="admin")
        db.create_user(conn, "b@b.co", role="archiver")  # manage_users 없음
        db.create_user(conn, "c@b.co", role="blocked")   # 비활성
        db.set_permission_overrides(conn, db.create_user(conn, "d@b.co", role="viewer"),
                                    {"manage_users": True})  # viewer + 부여
        assert db.count_active_users_with_permission(conn, "manage_users") == 2
        assert (
            db.count_active_users_with_permission(conn, "manage_users", exclude_user_id=a)
            == 1
        )


def test_token_permissions_reflect_overrides(tmp_db):
    with db.connect() as conn:
        uid = db.create_user(conn, "a@b.co", role="viewer")
        db.set_permission_overrides(conn, uid, {"archive": True})
        user = db.get_user_by_id(conn, uid)
    # 역할 기준은 viewer (보기만), 사용자 실효 기준은 archive 까지
    assert permissions.token_permissions_for_role("viewer") == (True, False)
    assert permissions.token_permissions_for_user(user) == (True, True)


# ---- 라우트 가드: 오버라이드가 전 경로에 반영 ----


def test_archiver_without_delete_override(client):
    """archiver 에서 삭제 권한만 떼면 삭제는 403, 아카이브는 그대로."""
    with db.connect() as conn:
        db.set_permission_overrides(conn, _uid("archiver@test.co"), {"delete": False})
    _login(client, "archiver@test.co")
    # 삭제 라우트는 막힌다 (없는 대상이어도 가드가 먼저 403)
    assert client.post("/page/999/delete").status_code == 403
    assert client.post("/snapshot/999/delete").status_code == 403
    # 아카이브는 여전히 가능 (가드 통과 후 검증 단계)
    res = client.post(
        "/archive", data={"url": "ftp://example.com/x"}, follow_redirects=False
    )
    assert res.status_code == 303 and "error=" in res.headers["location"]


def test_viewer_with_archive_override(client):
    """viewer 에게 아카이브 권한을 부여하면 아카이빙 메뉴·라우트가 열린다."""
    with db.connect() as conn:
        db.set_permission_overrides(conn, _uid("viewer@test.co"), {"archive": True})
    _login(client, "viewer@test.co")
    assert client.get("/archive/new").status_code == 200
    assert 'href="/archive/new"' in client.get("/archives").text
    res = client.post(
        "/archive", data={"url": "ftp://example.com/x"}, follow_redirects=False
    )
    assert res.status_code == 303 and "error=" in res.headers["location"]


def test_admin_area_split_manage_users_vs_system(client):
    """manage_users 와 manage_system 을 분리 부여하면 접근 영역이 갈린다."""
    with db.connect() as conn:
        db.set_permission_overrides(conn, _uid("viewer@test.co"), {"manage_users": True})
        db.set_permission_overrides(conn, _uid("archiver@test.co"), {"manage_system": True})

    # 사용자 관리만 가진 계정: /system/users 는 되고 /system 은 막힌다
    _login(client, "viewer@test.co")
    assert client.get("/system/users").status_code == 200
    assert client.get("/system").status_code == 403
    nav = client.get("/archives").text
    assert 'href="/system/users"' in nav and 'href="/system"' not in nav

    # 시스템 관리만 가진 계정: 반대로
    _login(client, "archiver@test.co")
    assert client.get("/system").status_code == 200
    assert client.get("/system/users").status_code == 403


def test_manage_credentials_split(client):
    """자격증명 관리 권한이 없으면 자격증명 화면은 403, 부여하면 통과한다."""
    _login(client, "archiver@test.co")
    assert client.get("/sites/999/credentials").status_code == 403
    with db.connect() as conn:
        db.set_permission_overrides(
            conn, _uid("archiver@test.co"), {"manage_credentials": True}
        )
    # 가드는 통과 — 없는 사이트라 403 이 아니라 404/리다이렉트 등 다른 응답
    assert client.get("/sites/999/credentials").status_code != 403


def test_role_change_resets_overrides(client):
    """역할을 바꾸면 세분 권한 오버라이드가 프리셋으로 초기화된다."""
    with db.connect() as conn:
        db.set_permission_overrides(conn, _uid("archiver@test.co"), {"delete": False})
    _login(client, "boss@test.co", "bosspass1234")
    uid = _uid("archiver@test.co")
    res = client.post(
        f"/system/users/{uid}/role", data={"role": "viewer"}, follow_redirects=False
    )
    assert res.status_code == 303
    with db.connect() as conn:
        assert db.get_user_by_id(conn, uid)["permission_overrides"] == "{}"


def test_set_permissions_route_saves_override(client):
    """관리자가 세분 권한 화면에서 archiver 의 삭제 권한을 떼면 저장된다."""
    _login(client, "boss@test.co", "bosspass1234")
    uid = _uid("archiver@test.co")
    # delete 만 빼고 archiver 프리셋(view, archive)을 체크해 제출
    res = client.post(
        f"/system/users/{uid}/permissions",
        data={"perm_view": "on", "perm_archive": "on"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    with db.connect() as conn:
        eff = db.effective_permissions(
            "archiver", db.get_user_by_id(conn, uid)["permission_overrides"]
        )
    assert eff == frozenset({"view", "archive"})


# ---- 라스트-관리자 잠김 방지 ----


@pytest.fixture
def solo_admin(tmp_db):
    """founder 없이 비-최초 관리자 1명만 있는 환경 (잠김 방지 경로 검증용)."""
    with db.connect() as conn:
        db.create_user(conn, "solo@test.co", auth.hash_password("password1234"),
                       role="admin")
    return TestClient(web_app.app)


def test_solo_admin_cannot_remove_own_manage_users_via_role(solo_admin):
    _login(solo_admin, "solo@test.co")
    uid = _uid("solo@test.co")
    res = solo_admin.post(
        f"/system/users/{uid}/role", data={"role": "viewer"}, follow_redirects=False
    )
    assert res.status_code == 303 and "error=" in res.headers["location"]
    with db.connect() as conn:
        assert db.get_user_by_id(conn, uid)["role"] == "admin"  # 변경 거부됨


def test_solo_admin_cannot_remove_own_manage_users_via_override(solo_admin):
    _login(solo_admin, "solo@test.co")
    uid = _uid("solo@test.co")
    # manage_users 만 빼고 나머지 admin 권한을 모두 체크해 제출
    data = {f"perm_{p}": "on" for p in db.PERMISSIONS if p != "manage_users"}
    res = solo_admin.post(
        f"/system/users/{uid}/permissions", data=data, follow_redirects=False
    )
    assert res.status_code == 303 and "error=" in res.headers["location"]
    with db.connect() as conn:
        eff = db.effective_permissions(
            "admin", db.get_user_by_id(conn, uid)["permission_overrides"]
        )
    assert "manage_users" in eff  # 떼이지 않았다
