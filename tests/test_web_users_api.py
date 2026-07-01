"""SvelteKit SPA 사용자 관리 API(/api/web/system/users) 테스트 — 목록·역할·권한·삭제·초대.

Phase C2 컷오버로 제거되는 SSR 사용자 화면(system_routes.users_view 와 변경 POST)의 검증
로직을 JSON API 기준으로 대체한다. SSR test_roles·test_permissions_granular 의 단정
(라스트-관리자 잠김 방지·founder/withdrawn 불변·manage_users vs manage_system 분리)에 대응.
"""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db
from chunchugwan.web import app as web_app

POST_HEADERS = {"X-Requested-With": "fetch", "Origin": "http://testserver"}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    web_app._active_jobs.clear()
    yield
    web_app._active_jobs.clear()


def make_user(email, password="userpass123", role="archiver", overrides=None):
    with db.connect() as conn:
        pw = auth.hash_password(password) if password else None
        uid = db.create_user(conn, email, pw, role=role)
        if overrides is not None:
            db.set_permission_overrides(conn, uid, overrides)
        token = auth.issue_session(conn, uid)
    return uid, token


def make_founder(email="boss@test.co", password="bosspass123"):
    with db.connect() as conn:
        uid = db.create_first_admin(conn, email, auth.hash_password(password))
    return uid


def client_for(token=None):
    c = TestClient(web_app.app)
    if token:
        c.cookies.set(config.SESSION_COOKIE, token)
    return c


# ---- 권한 게이트 ----


def test_users_requires_manage_users(tmp_db):
    _, arch = make_user("arch@test.co", role="archiver")
    assert client_for().get("/api/web/system/users").status_code == 401
    assert client_for(arch).get("/api/web/system/users").status_code == 403


def test_manage_users_vs_manage_system_split(tmp_db):
    """manage_users 만 가진 계정: /system/users 통과, /system(manage_system) 거부."""
    _, token = make_user("u@test.co", role="viewer", overrides={"manage_users": True})
    c = client_for(token)
    assert c.get("/api/web/system/users").status_code == 200
    assert c.get("/api/web/system").status_code == 403


# ---- 목록 ----


def test_users_list(tmp_db):
    make_founder()
    _, admin = make_user("admin@test.co", role="admin")
    body = client_for(admin).get("/api/web/system/users").json()
    emails = {u["email"] for u in body["users"]}
    assert {"boss@test.co", "admin@test.co"} <= emails
    assert "roles" in body and "role_labels" in body


# ---- 역할 변경 ----


def test_user_role_change(tmp_db):
    _, admin = make_user("admin@test.co", role="admin")
    target, _ = make_user("v@test.co", role="viewer")
    r = client_for(admin).post(f"/api/web/system/users/{target}/role",
                               json={"role": "archiver"}, headers=POST_HEADERS)
    assert r.status_code == 200
    with db.connect() as conn:
        assert db.get_user_by_id(conn, target)["role"] == "archiver"


def test_user_role_invalid(tmp_db):
    _, admin = make_user("admin@test.co", role="admin")
    target, _ = make_user("v@test.co", role="viewer")
    r = client_for(admin).post(f"/api/web/system/users/{target}/role",
                               json={"role": "superboss"}, headers=POST_HEADERS)
    assert r.status_code == 400


def test_user_role_founder_protected(tmp_db):
    founder = make_founder()
    _, admin = make_user("admin@test.co", role="admin")
    r = client_for(admin).post(f"/api/web/system/users/{founder}/role",
                               json={"role": "viewer"}, headers=POST_HEADERS)
    assert r.status_code == 400


def test_user_role_withdrawn_protected(tmp_db):
    _, admin = make_user("admin@test.co", role="admin")
    target, _ = make_user("w@test.co", role="viewer")
    with db.connect() as conn:
        db.set_role(conn, target, "withdrawn")
    r = client_for(admin).post(f"/api/web/system/users/{target}/role",
                               json={"role": "archiver"}, headers=POST_HEADERS)
    assert r.status_code == 400


def test_user_role_last_manage_users_blocked(tmp_db):
    """유일한 manage_users 보유 계정의 자기 강등은 거부(라스트-관리자 잠김 방지)."""
    uid, admin = make_user("admin@test.co", role="admin")
    r = client_for(admin).post(f"/api/web/system/users/{uid}/role",
                               json={"role": "viewer"}, headers=POST_HEADERS)
    assert r.status_code == 400


# 세분 권한(사용자별 오버라이드) 편집 기능은 제거됐다 — 권한은 역할 단위로만 부여한다.


# ---- 삭제 ----


def test_user_delete_email_mismatch(tmp_db):
    _, admin = make_user("admin@test.co", role="admin")
    target, _ = make_user("v@test.co", role="viewer")
    r = client_for(admin).post(f"/api/web/system/users/{target}/delete",
                               json={"email": "wrong@test.co"}, headers=POST_HEADERS)
    assert r.status_code == 400


def test_user_delete_success_and_founder_protected(tmp_db):
    founder = make_founder()
    _, admin = make_user("admin@test.co", role="admin")
    target, _ = make_user("v@test.co", role="viewer")
    c = client_for(admin)
    # founder 는 삭제 불가
    assert c.post(f"/api/web/system/users/{founder}/delete",
                  json={"email": "boss@test.co"}, headers=POST_HEADERS).status_code == 400
    # 이메일 일치 시 삭제
    assert c.post(f"/api/web/system/users/{target}/delete",
                  json={"email": "v@test.co"}, headers=POST_HEADERS).status_code == 200
    with db.connect() as conn:
        assert db.get_user_by_id(conn, target) is None


# ---- 초대 ----


def test_user_invite(tmp_db):
    _, admin = make_user("admin@test.co", role="admin")
    c = client_for(admin)
    r = c.post("/api/web/system/users/invite",
               json={"email": "new@test.co", "role": "viewer"}, headers=POST_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "new@test.co"
    assert "/invite/" in body["link"]
    # 같은 이메일 재초대 시도 → 이미 초대/가입 충돌은 아니나 가입자 중복은 거부
    make_user("dup@test.co", role="viewer")
    dup = c.post("/api/web/system/users/invite",
                 json={"email": "dup@test.co", "role": "viewer"}, headers=POST_HEADERS)
    assert dup.status_code == 400


def test_invite_delete_404(tmp_db):
    _, admin = make_user("admin@test.co", role="admin")
    r = client_for(admin).post("/api/web/system/users/invite/9999/delete",
                               headers=POST_HEADERS)
    assert r.status_code == 404


def test_invite_regenerate(tmp_db):
    """재생성 — 같은 이메일·역할로 새 링크 발급, 이전 토큰은 무효화된다."""
    _, admin = make_user("admin@test.co", role="admin")
    c = client_for(admin)
    inv = c.post("/api/web/system/users/invite",
                 json={"email": "new@test.co", "role": "viewer"}, headers=POST_HEADERS).json()
    old_token = inv["link"].rsplit("/invite/", 1)[1]
    with db.connect() as conn:
        invite_id = db.list_invites(conn)[0]["id"]
    r = c.post(f"/api/web/system/users/invite/{invite_id}/regenerate", headers=POST_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "new@test.co"
    new_token = body["link"].rsplit("/invite/", 1)[1]
    assert new_token != old_token
    # 이전 토큰 무효, 새 토큰만 유효
    with db.connect() as conn:
        assert db.get_invite_by_token(conn, auth.hash_token(old_token)) is None
        assert db.get_invite_by_token(conn, auth.hash_token(new_token))["email"] == "new@test.co"


def test_invite_regenerate_404(tmp_db):
    _, admin = make_user("admin@test.co", role="admin")
    r = client_for(admin).post("/api/web/system/users/invite/9999/regenerate",
                               headers=POST_HEADERS)
    assert r.status_code == 404


def test_invite_regenerate_rejects_registered(tmp_db):
    """초대 후 같은 이메일이 가입을 완료했으면 재생성은 거부된다."""
    _, admin = make_user("admin@test.co", role="admin")
    c = client_for(admin)
    c.post("/api/web/system/users/invite",
           json={"email": "soon@test.co", "role": "viewer"}, headers=POST_HEADERS)
    with db.connect() as conn:
        invite_id = db.list_invites(conn)[0]["id"]
    make_user("soon@test.co", role="viewer")  # 같은 이메일 가입 완료
    r = c.post(f"/api/web/system/users/invite/{invite_id}/regenerate", headers=POST_HEADERS)
    assert r.status_code == 400


def test_invite_regenerate_requires_manage_users(tmp_db):
    _, arch = make_user("arch@test.co", role="archiver")
    assert client_for().post("/api/web/system/users/invite/1/regenerate",
                             headers=POST_HEADERS).status_code == 401
    assert client_for(arch).post("/api/web/system/users/invite/1/regenerate",
                                 headers=POST_HEADERS).status_code == 403


def test_invite_regenerate_expired(tmp_db):
    """이 기능의 핵심 — 이미 만료된 초대도 재생성되어 새 토큰·새 만료가 발급된다."""
    admin_id, admin = make_user("admin@test.co", role="admin")
    with db.connect() as conn:
        iid = db.create_invite(conn, "stale@test.co", auth.hash_token("stale-tok"),
                               "viewer", admin_id, ttl_seconds=-60)
    r = client_for(admin).post(f"/api/web/system/users/invite/{iid}/regenerate",
                               headers=POST_HEADERS)
    assert r.status_code == 200
    new_token = r.json()["link"].rsplit("/invite/", 1)[1]
    with db.connect() as conn:
        # 이전(만료) 토큰은 무효, 새 토큰은 유효(만료 리셋됨)
        assert db.get_invite_by_token(conn, auth.hash_token("stale-tok")) is None
        assert db.get_invite_by_token(conn, auth.hash_token(new_token))["email"] == "stale@test.co"


def test_system_users_serializes_expired_flag(tmp_db):
    """system_users GET 이 만료(grace 내) 초대를 expired=true 로, 미만료는 false 로 내려준다."""
    admin_id, admin = make_user("admin@test.co", role="admin")
    with db.connect() as conn:
        db.create_invite(conn, "live@test.co", auth.hash_token("a"), "viewer", admin_id, ttl_seconds=3600)
        db.create_invite(conn, "dead@test.co", auth.hash_token("b"), "viewer", admin_id, ttl_seconds=-60)
    body = client_for(admin).get("/api/web/system/users").json()
    flags = {i["email"]: i["expired"] for i in body["invites"]}
    assert flags == {"live@test.co": False, "dead@test.co": True}
    assert all(isinstance(v, bool) for v in flags.values())


def test_users_pagination(tmp_db):
    """사용자 목록 페이징 — 기본 25, 선택지 10/25/50/100, clamp. (초대는 페이징 대상 아님)"""
    _, admin = make_user("admin@test.co", role="admin")
    for i in range(12):
        make_user(f"u{i:02d}@test.co", role="viewer")
    c = client_for(admin)
    # 페이지 크기 쿼리명은 limit — 프론트 PageSize·딥링크와 일치(H3)
    body = c.get("/api/web/system/users?limit=10&page=1").json()
    assert body["total"] == 13 and body["total_pages"] == 2  # admin + 12
    assert len(body["users"]) == 10 and body["limit"] == 10
    assert body["limits"] == [10, 25, 50, 100] and body["page_num"] == 1
    body2 = c.get("/api/web/system/users?limit=10&page=2").json()
    assert len(body2["users"]) == 3
    # 허용 밖 limit → 기본 25, 범위 초과 page → 마지막으로 클램프
    assert c.get("/api/web/system/users?limit=999").json()["limit"] == 25
    clamped = c.get("/api/web/system/users?limit=10&page=99").json()
    assert clamped["page_num"] == clamped["total_pages"] == 2
