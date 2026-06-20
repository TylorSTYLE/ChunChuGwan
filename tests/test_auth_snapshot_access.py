"""로그인 자격증명으로 캡처된 스냅샷의 접근 제한 — 소유자/관리자만 열람."""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, storage
from chunchugwan.web import app as web_app

URL = "https://example.com/secret"


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")


def _seed():
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(conn, "arch@test.co", auth.hash_password("password1234"), role="archiver")
        db.create_user(conn, "arch2@test.co", auth.hash_password("password1234"), role="archiver")
        db.create_user(conn, "viewer@test.co", auth.hash_password("password1234"), role="viewer")


@pytest.fixture
def client(tmp_db):
    _seed()
    web_app._active_jobs.clear()
    yield TestClient(web_app.app, headers={"X-Requested-With": "fetch"})
    web_app._active_jobs.clear()


def _uid(email):
    with db.connect() as conn:
        return db.get_user_by_email(conn, email)["id"]


def _ext_token(email):
    uid = _uid(email)
    with db.connect() as conn:
        return auth.issue_api_key(conn, "ext", can_view=True, can_archive=True,
                                  created_by=uid, owner_user_id=uid, ttl_seconds=None)


def _system_token():
    with db.connect() as conn:
        return auth.issue_api_key(conn, "sys", can_view=True, can_archive=True,
                                  created_by=1, ttl_seconds=None)


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _login(client, email, pw="password1234"):
    client.post("/api/web/auth/login", json={"email": email, "password": pw})


def _insert_auth_snapshots(owner_id, specs):
    """specs: [(dir_suffix, content_hash, body, authenticated, authenticated_by)] → page_id."""
    domain, slug = "example.com", storage.url_to_slug(URL)
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, URL, domain, slug)
        for i, (chash, body, authed, by) in enumerate(specs):
            d = f"2026-06-0{i + 1}T00-00-00"
            snap_dir = storage.page_dir(domain, slug) / d
            snap_dir.mkdir(parents=True)
            (snap_dir / "content.md").write_text(body, encoding="utf-8")
            db.insert_snapshot(
                conn, page_id, taken_at=f"2026-06-0{i + 1}T00:00:00+00:00",
                dir_name=d, content_hash=chash, final_url=URL, http_status=200,
                changed=1, authenticated=authed, authenticated_by=by,
            )
    return page_id


# ---- _may_view_authenticated 로직 ----


def test_may_view_authenticated_logic(client):
    owner = _uid("arch@test.co")
    _insert_auth_snapshots(owner, [("h", "secret", 1, owner)])
    with db.connect() as conn:
        snap = db.get_snapshot(conn, 1)

    class _Req:
        def __init__(self, user=None, api_key=None):
            self.state = type("S", (), {"user": user, "api_key": api_key})()

    admin = {"role": "admin", "id": _uid("boss@test.co")}
    owner_user = {"role": "archiver", "id": owner}
    other = {"role": "archiver", "id": _uid("arch2@test.co")}
    assert web_app._may_view_authenticated(_Req(user=admin), snap) is True
    assert web_app._may_view_authenticated(_Req(user=owner_user), snap) is True
    assert web_app._may_view_authenticated(_Req(user=other), snap) is False
    assert web_app._may_view_authenticated(
        _Req(api_key={"owner_user_id": owner}), snap) is True
    assert web_app._may_view_authenticated(
        _Req(api_key={"owner_user_id": None}), snap) is False


# ---- API 접근 제한 ----


def test_api_authenticated_snapshot_owner_only(client):
    owner = _uid("arch@test.co")
    page_id = _insert_auth_snapshots(owner, [("hsecret", "일급비밀", 1, owner)])
    owner_tok, other_tok, sys_tok = (
        _ext_token("arch@test.co"), _ext_token("arch2@test.co"), _system_token()
    )
    # 메타데이터: 소유자만
    assert client.get("/api/v1/snapshots/1", headers=_headers(owner_tok)).status_code == 200
    assert client.get("/api/v1/snapshots/1", headers=_headers(other_tok)).status_code == 404
    assert client.get("/api/v1/snapshots/1", headers=_headers(sys_tok)).status_code == 401  # 시스템 키 인증 거부
    # 파일(content): 소유자만 (_load_snapshot 가드)
    r = client.get("/api/v1/snapshots/1/file/content.md", headers=_headers(owner_tok))
    assert r.status_code == 200 and "일급비밀" in r.text
    assert client.get(
        "/api/v1/snapshots/1/file/content.md", headers=_headers(other_tok)
    ).status_code == 404
    # 페이지 히스토리: 비소유자에겐 인증 스냅샷이 빠진다
    owner_snaps = client.get(f"/api/v1/pages/{page_id}", headers=_headers(owner_tok)).json()["snapshots"]
    other_snaps = client.get(f"/api/v1/pages/{page_id}", headers=_headers(other_tok)).json()["snapshots"]
    assert len(owner_snaps) == 1 and other_snaps == []


def test_regular_snapshot_unrestricted(client):
    owner = _uid("arch@test.co")
    _insert_auth_snapshots(owner, [("pub", "공개", 0, None)])  # authenticated=0
    other_tok = _ext_token("arch2@test.co")
    assert client.get("/api/v1/snapshots/1", headers=_headers(other_tok)).status_code == 200


# ---- 웹 뷰어·diff·타임라인 접근 제한 ----


def test_web_authenticated_snapshot_denied_to_other(client):
    # C2 컷오버: 스냅샷 콘텐츠는 자원 라우트(/snapshot/{id}/file/…)가 _load_snapshot
    # 가드로 비소유자에게 404(존재 은폐). SSR 뷰(/snapshot/{id})는 이제 SPA 셸이다.
    owner = _uid("arch@test.co")
    _insert_auth_snapshots(owner, [("h", "secret", 1, owner)])
    _login(client, "viewer@test.co")
    assert client.get("/snapshot/1/file/content.md").status_code == 404
    _login(client, "arch@test.co")  # 소유자
    assert client.get("/snapshot/1/file/content.md").status_code == 200


def test_web_diff_blocks_authenticated(client):
    # diff 데이터는 /api/web/diff/{page_id} (JSON) — 비소유자는 인증 스냅샷이 가려져 404.
    owner = _uid("arch@test.co")
    page_id = _insert_auth_snapshots(owner, [
        ("h1", "비밀1", 1, owner), ("h2", "비밀2", 1, owner),
    ])
    _login(client, "viewer@test.co")
    assert client.get(f"/api/web/diff/{page_id}").status_code == 404
    _login(client, "arch@test.co")  # 소유자
    assert client.get(f"/api/web/diff/{page_id}").status_code == 200


def test_web_timeline_hides_authenticated(client):
    # 타임라인 데이터는 /api/web/pages/{page_id} (JSON) — 비소유자에겐 인증 스냅샷이 빠진다.
    owner = _uid("arch@test.co")
    page_id = _insert_auth_snapshots(owner, [
        ("publichash01", "공개", 0, None), ("secrethash99", "비밀", 1, owner),
    ])
    _login(client, "viewer@test.co")
    hashes = [s["snap"]["content_hash"]
              for s in client.get(f"/api/web/pages/{page_id}").json()["snapshots"]]
    assert "publichash01" in hashes and "secrethash99" not in hashes
    _login(client, "arch@test.co")
    owner_hashes = [s["snap"]["content_hash"]
                    for s in client.get(f"/api/web/pages/{page_id}").json()["snapshots"]]
    assert "secrethash99" in owner_hashes


# ---- 사용자 삭제 FK ----


def test_delete_user_nulls_authenticated_by(client):
    owner = _uid("arch@test.co")
    page_id = _insert_auth_snapshots(owner, [("h", "secret", 1, owner)])
    with db.connect() as conn:
        db.delete_user(conn, owner)  # FK 위반 없이 성공
        snap = db.list_snapshots(conn, page_id)[0]
        assert snap["authenticated"] == 1 and snap["authenticated_by"] is None


# ---- 집계 메타데이터 누출 차단 (snapshot_count / last_taken_at / checks) ----


def test_list_pages_count_is_viewer_aware(client):
    owner = _uid("arch@test.co")
    _insert_auth_snapshots(owner, [
        ("publichashBB", "공개", 0, None),       # 2026-06-01 (public)
        ("secrethashAA", "비밀", 1, owner),       # 2026-06-02 (authenticated, 더 최근)
    ])
    other = _uid("arch2@test.co")
    with db.connect() as conn:
        allp = {p["url"]: p for p in db.list_pages(conn)}                  # viewer=None=전체
        otherp = {p["url"]: p for p in db.list_pages(conn, viewer=(other, False))}
        ownp = {p["url"]: p for p in db.list_pages(conn, viewer=(owner, False))}
        admp = {p["url"]: p for p in db.list_pages(conn, viewer=(_uid("boss@test.co"), True))}
    assert allp[URL]["snapshot_count"] == 2
    assert otherp[URL]["snapshot_count"] == 1        # 인증 제외
    assert otherp[URL]["last_taken_at"] == "2026-06-01T00:00:00+00:00"  # 공개분만
    assert ownp[URL]["snapshot_count"] == 2          # 소유자 포함
    assert admp[URL]["snapshot_count"] == 2          # admin 전부


def test_api_pages_count_hides_authenticated(client):
    owner = _uid("arch@test.co")
    _insert_auth_snapshots(owner, [("publichashBB", "공개", 0, None),
                                   ("secrethashAA", "비밀", 1, owner)])
    own = client.get("/api/v1/pages", headers=_headers(_ext_token("arch@test.co"))).json()["pages"]
    other = client.get("/api/v1/pages", headers=_headers(_ext_token("arch2@test.co"))).json()["pages"]
    assert next(p for p in own if p["url"] == URL)["snapshot_count"] == 2
    assert next(p for p in other if p["url"] == URL)["snapshot_count"] == 1


def test_list_sites_overview_count_is_viewer_aware(client):
    owner = _uid("arch@test.co")
    _insert_auth_snapshots(owner, [("publichashBB", "공개", 0, None),
                                   ("secrethashAA", "비밀", 1, owner)])
    with db.connect() as conn:
        other = next(s for s in db.list_sites_overview(conn, viewer=(_uid("arch2@test.co"), False))
                     if s["site_key"] == "example.com")
        own = next(s for s in db.list_sites_overview(conn, viewer=(owner, False))
                   if s["site_key"] == "example.com")
    assert other["snapshot_count"] == 1 and own["snapshot_count"] == 2


def test_timeline_hides_checks_hash_for_non_owner(client):
    owner = _uid("arch@test.co")
    page_id = _insert_auth_snapshots(owner, [("publichashBB", "공개", 0, None),
                                             ("secrethashAA", "비밀", 1, owner)])
    with db.connect() as conn:
        db.insert_check(conn, page_id, "checkhashCC9")  # 변경없음 확인 기록
    # /api/web/pages 는 가려진 인증 스냅샷이 있으면 checks 도 통째로 숨긴다(비소유자).
    _login(client, "viewer@test.co")
    checks = client.get(f"/api/web/pages/{page_id}").json()["checks"]
    assert all(c["content_hash"] != "checkhashCC9" for c in checks)
    _login(client, "arch@test.co")       # 소유자
    owner_checks = client.get(f"/api/web/pages/{page_id}").json()["checks"]
    assert any(c["content_hash"] == "checkhashCC9" for c in owner_checks)
