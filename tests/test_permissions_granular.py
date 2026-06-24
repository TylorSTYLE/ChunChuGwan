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
    # viewer = 보기 + 메모 보기
    assert db.effective_permissions("viewer") == frozenset({"view", "memo_view"})
    # 신규 설치 archiver = 보기·아카이빙·개인 API Key·메모 보기/등록 (삭제 없음)
    assert db.effective_permissions("archiver") == frozenset(
        {"view", "archive", "use_api_keys", "memo_view", "memo_create"}
    )
    # 아카이브 관리 = +삭제 +메모 삭제
    assert db.effective_permissions("archive_manager") == frozenset(
        {"view", "archive", "delete", "use_api_keys",
         "memo_view", "memo_create", "memo_delete"}
    )
    assert db.effective_permissions("admin") == frozenset(db.PERMISSIONS)
    for inactive in ("pending", "blocked", "withdrawn"):
        assert db.effective_permissions(inactive) == frozenset()


def test_overrides_add_and_remove(tmp_db):
    # archive_manager 에서 삭제만 제거
    assert db.effective_permissions(
        "archive_manager", '{"delete": false}'
    ) == frozenset(
        {"view", "archive", "use_api_keys",
         "memo_view", "memo_create", "memo_delete"}
    )
    # viewer 에게 아카이브 추가
    assert db.effective_permissions("viewer", '{"archive": true}') == frozenset(
        {"view", "memo_view", "archive"}
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


# ---- 라스트-관리자 잠김 방지 ----


@pytest.fixture
def solo_admin(tmp_db):
    """founder 없이 비-최초 관리자 1명만 있는 환경 (잠김 방지 경로 검증용)."""
    with db.connect() as conn:
        db.create_user(conn, "solo@test.co", auth.hash_password("password1234"),
                       role="admin")
    return TestClient(web_app.app)


