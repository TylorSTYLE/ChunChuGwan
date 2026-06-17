"""커스텀 권한 그룹(역할) — 추가·삭제·편집 + 동적 프리셋 캐시.

역할 프리셋(ROLE_PRESETS 상수)을 DB permission_groups 로 옮겨, 관리자가 코드
배포 없이 역할의 기본 권한을 편집하고 새 그룹을 추가·삭제할 수 있다.
"""
import json

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


# ---- 시드 / 빌트인 ----


def test_seed_is_idempotent(tmp_db):
    """_migrate 가 여러 번 돌아도 빌트인 4개만 유지된다(INSERT OR IGNORE)."""
    builtin = {"admin", "archive_manager", "archiver", "viewer"}
    with db.connect() as conn:
        names = {r["name"] for r in db.list_permission_groups(conn)}
    assert names == builtin
    # 두 번째 연결(=_migrate 재호출 가능)에도 그대로
    with db.connect() as conn:
        rows = db.list_permission_groups(conn)
    assert {r["name"] for r in rows} == builtin
    assert all(r["is_builtin"] for r in rows)


def test_builtin_presets_seed_defaults(tmp_db):
    """신규 설치 기본 프리셋 — archiver 는 삭제 없음, archive_manager 가 삭제 보유.

    개인 API Key(use_api_keys)는 viewer 외 빌트인에 기본 부여된다.
    """
    with db.connect() as conn:
        presets = db.role_presets(conn)
    assert presets["viewer"] == frozenset({"view"})
    assert presets["archiver"] == frozenset({"view", "archive", "use_api_keys"})
    assert presets["archive_manager"] == frozenset(
        {"view", "archive", "delete", "use_api_keys"}
    )
    assert presets["admin"] == frozenset(db.PERMISSIONS)


def test_migrate_adds_use_api_keys_to_existing_builtins(tmp_db):
    """기존 설치 보강 — admin·archiver 에 use_api_keys 추가, archiver 의 삭제는 유지.

    use_api_keys 는 신규 권한이라 레거시 그룹 JSON 에 없다. 기존 archiver 는
    삭제 권한을 그대로 두고(결정 사항) use_api_keys 만 보강하며, archive_manager
    그룹이 새로 시드된다. viewer 는 보강 대상이 아니다.
    """
    # 레거시 상태로 되돌림 — use_api_keys 없음, archiver 는 삭제 보유, archive_manager 제거
    with db.connect() as conn:
        conn.execute("DELETE FROM permission_groups WHERE name = 'archive_manager'")
        conn.execute(
            "UPDATE permission_groups SET permissions = ? WHERE name = 'admin'",
            (json.dumps([p for p in db.PERMISSIONS if p != "use_api_keys"]),),
        )
        conn.execute(
            "UPDATE permission_groups SET permissions = ? WHERE name = 'archiver'",
            (json.dumps(["view", "archive", "delete"]),),
        )
    # 보강 마이그레이션 재실행 (seed + use_api_keys 보강)
    with db.connect() as conn:
        db._seed_permission_groups(conn)
        db._migrate_api_key_permission(conn)
        presets = db.role_presets(conn)
    # archiver 는 기존 삭제 유지 + use_api_keys 보강 (기존 설치는 삭제를 잃지 않는다)
    assert presets["archiver"] == frozenset(
        {"view", "archive", "delete", "use_api_keys"}
    )
    assert presets["admin"] == frozenset(db.PERMISSIONS)  # use_api_keys 포함
    assert presets["archive_manager"] == frozenset(
        {"view", "archive", "delete", "use_api_keys"}
    )
    assert presets["viewer"] == frozenset({"view"})  # 보강 대상 아님


# ---- 이름 정규화 ----


def test_normalize_group_name():
    assert db.normalize_group_name("  Editor ") == "editor"
    assert db.normalize_group_name("Power User") == "power_user"
    assert db.normalize_group_name("read-only") == "read_only"
    for bad in ("", "  ", "한글", "a" * 33, "bad!name"):
        with pytest.raises(ValueError):
            db.normalize_group_name(bad)
    for reserved in ("admin", "archive_manager", "viewer", "pending", "blocked", "withdrawn"):
        with pytest.raises(ValueError):
            db.normalize_group_name(reserved)


# ---- CRUD ----


def test_create_update_delete_group(tmp_db):
    with db.connect() as conn:
        name = db.create_permission_group(conn, "Editor", "편집자", ["view", "archive"])
        assert name == "editor"
        g = db.get_permission_group(conn, "editor")
        assert g["label"] == "편집자" and not g["is_builtin"]
        assert db.role_presets(conn)["editor"] == frozenset({"view", "archive"})
        # 중복 거부
        with pytest.raises(ValueError):
            db.create_permission_group(conn, "editor", "x", [])
        # 권한·라벨 갱신
        db.update_permission_group(
            conn, "editor", label="에디터", permissions=["view", "archive", "delete"]
        )
        assert db.role_presets(conn)["editor"] == frozenset(
            {"view", "archive", "delete"}
        )
        assert db.get_permission_group(conn, "editor")["label"] == "에디터"
        # 삭제
        assert db.delete_permission_group(conn, "editor") is True
        assert db.get_permission_group(conn, "editor") is None


def test_builtin_label_locked_permissions_editable(tmp_db):
    with db.connect() as conn:
        # 빌트인 라벨은 잠김, permissions 는 편집 가능
        db.update_permission_group(
            conn, "viewer", label="HACKED", permissions=["view", "archive"]
        )
        g = db.get_permission_group(conn, "viewer")
        assert g["label"] == "보기 전용"  # 라벨 변경 무시
        assert db.role_presets(conn)["viewer"] == frozenset({"view", "archive"})
        # 빌트인 삭제 거부
        assert db.delete_permission_group(conn, "viewer") is False


# ---- 동적 접근자 ----


def test_dynamic_role_lists_include_custom(tmp_db):
    with db.connect() as conn:
        db.create_permission_group(conn, "editor", "편집자", ["view", "archive"])
        assert "editor" in db.permission_group_names(conn)
        assert "editor" in db.assignable_roles(conn)
        assert ("pending", "blocked") == db.assignable_roles(conn)[-2:]
        assert "editor" in db.invitable_roles(conn)
        # 가입 역할은 admin 을 빼고 custom 은 포함
        signup = db.signup_roles(conn)
        assert "admin" not in signup and "editor" in signup and signup[0] == "pending"
        assert db.role_labels(conn)["editor"] == "편집자"
        assert db.all_valid_roles(conn) >= {"editor", "pending", "blocked", "withdrawn"}


# ---- 캐시 / 멀티프로세스 staleness ----


def test_preset_cache_reloads_on_version_bump(tmp_db):
    """그룹 편집 후 새 conn 의 role_presets 가 버전 불일치로 재로드한다."""
    with db.connect() as conn:
        db.create_permission_group(conn, "editor", "편집자", ["view"])
    # 워밍
    with db.connect() as conn:
        assert db.role_presets(conn)["editor"] == frozenset({"view"})
    # 다른 conn 에서 편집(버전 +1) — 다음 role_presets 호출이 재로드해야 한다
    with db.connect() as conn:
        db.update_permission_group(conn, "editor", permissions=["view", "archive"])
    with db.connect() as conn:
        assert db.role_presets(conn)["editor"] == frozenset({"view", "archive"})


# ---- API 토큰 파생 ----


def test_token_permissions_for_custom_group(tmp_db):
    with db.connect() as conn:
        db.create_permission_group(conn, "editor", "편집자", ["view", "archive"])
        db.role_presets(conn)  # 캐시 워밍 (token_permissions_for_role 은 캐시 사용)
    assert permissions.token_permissions_for_role("editor") == (True, True)


# ---- 라우트 ----


def test_groups_screen_and_assignment(client):
    """관리자가 그룹을 만들고 사용자에 배정 → 화면에 소속 수가 뜬다."""
    _login(client, "boss@test.co", "bosspass1234")
    # 새 그룹 추가
    res = client.post(
        "/system/groups",
        data={"name": "editor", "label": "편집자", "perm_view": "on", "perm_archive": "on"},
        follow_redirects=False,
    )
    assert res.status_code == 303 and "error" not in res.headers["location"]
    with db.connect() as conn:
        assert db.role_presets(conn)["editor"] == frozenset({"view", "archive"})
    # 사용자에 배정
    vid = _uid("viewer@test.co")
    res = client.post(
        f"/system/users/{vid}/role", data={"role": "editor"}, follow_redirects=False
    )
    assert res.status_code == 303
    with db.connect() as conn:
        assert db.get_user_by_id(conn, vid)["role"] == "editor"
        assert db.count_users_with_role(conn, "editor") == 1
    # 화면에 그룹과 소속 수 노출
    page = client.get("/system/groups")
    assert page.status_code == 200 and "editor" in page.text


def test_delete_group_with_members_rejected(client):
    _login(client, "boss@test.co", "bosspass1234")
    client.post(
        "/system/groups",
        data={"name": "editor", "label": "편집자", "perm_view": "on"},
        follow_redirects=False,
    )
    vid = _uid("viewer@test.co")
    client.post(f"/system/users/{vid}/role", data={"role": "editor"}, follow_redirects=False)
    # 소속자 있는 그룹 삭제 거부
    res = client.post("/system/groups/editor/delete", follow_redirects=False)
    assert res.status_code == 303 and "error" in res.headers["location"]
    with db.connect() as conn:
        assert db.get_permission_group(conn, "editor") is not None
    # 소속자를 옮기면 삭제 성공
    client.post(f"/system/users/{vid}/role", data={"role": "viewer"}, follow_redirects=False)
    res = client.post("/system/groups/editor/delete", follow_redirects=False)
    assert "error" not in res.headers["location"]
    with db.connect() as conn:
        assert db.get_permission_group(conn, "editor") is None


def test_delete_builtin_group_rejected(client):
    _login(client, "boss@test.co", "bosspass1234")
    res = client.post("/system/groups/viewer/delete", follow_redirects=False)
    assert res.status_code == 303 and "error" in res.headers["location"]
    with db.connect() as conn:
        assert db.get_permission_group(conn, "viewer") is not None


def test_edit_admin_group_last_manage_users_locked(client):
    """admin 그룹에서 manage_users 를 떼면(유일 보유자 founder) 거부된다."""
    _login(client, "boss@test.co", "bosspass1234")
    # manage_users 외 모든 권한 체크 → manage_users 제거 시도
    data = {f"perm_{p}": "on" for p in db.PERMISSIONS if p != "manage_users"}
    res = client.post("/system/groups/admin", data=data, follow_redirects=False)
    assert res.status_code == 303 and "error" in res.headers["location"]
    with db.connect() as conn:
        assert "manage_users" in db.role_presets(conn)["admin"]


def test_custom_group_member_gets_permissions(client):
    """커스텀 그룹(view+archive) 사용자는 아카이빙 가능·삭제 불가."""
    _login(client, "boss@test.co", "bosspass1234")
    client.post(
        "/system/groups",
        data={"name": "editor", "label": "편집자", "perm_view": "on", "perm_archive": "on"},
        follow_redirects=False,
    )
    vid = _uid("viewer@test.co")
    client.post(f"/system/users/{vid}/role", data={"role": "editor"}, follow_redirects=False)
    # 그 사용자로 로그인 — archive 가능(존재하지 않는 page 라 404, 403 아님), delete 는 403
    user_client = TestClient(web_app.app)
    _login(user_client, "viewer@test.co")
    assert user_client.post("/page/999/rearchive").status_code == 404
    assert user_client.post("/page/999/delete").status_code == 403
