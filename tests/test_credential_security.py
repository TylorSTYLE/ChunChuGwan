"""컴포넌트3 보안 검토 후속 회귀 — diff/timeline 접근제한, 사용자 삭제 FK,
http 인증 캡처 거부, 쿠키 스코프 강화, cross-origin 자원 차단."""
import base64

import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, capture, config, credentials, db, storage
from chunchugwan.web import app as web_app

VALID_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()


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
        db.create_user(conn, "viewer@test.co", auth.hash_password("password1234"), role="viewer")


@pytest.fixture
def client(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "CREDENTIAL_KEY", VALID_KEY)
    _seed()
    web_app._active_jobs.clear()
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def _uid(email):
    with db.connect() as conn:
        return db.get_user_by_email(conn, email)["id"]


def _ext_token(email):
    uid = _uid(email)
    with db.connect() as conn:
        return auth.issue_api_key(conn, "ext", can_view=True, can_archive=True,
                                  created_by=uid, owner_user_id=uid, ttl_seconds=None)


def _login(client, email, pw="password1234"):
    client.post("/login", data={"email": email, "password": pw}, follow_redirects=False)


def _make_page_with_snaps(owner_id, specs):
    """specs: [(content_hash, content_md, authenticated_by_or_None)] → page_id."""
    url = "https://example.com/secret"
    domain, slug = "example.com", storage.url_to_slug(url)
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, url, domain, slug)
        for i, (chash, body, auth_by) in enumerate(specs):
            d = f"2026-06-0{i + 1}T00-00-00"
            snap_dir = storage.page_dir(domain, slug) / d
            snap_dir.mkdir(parents=True)
            (snap_dir / "content.md").write_text(body, encoding="utf-8")
            db.insert_snapshot(
                conn, page_id, taken_at=f"2026-06-0{i + 1}T00:00:00+00:00",
                dir_name=d, content_hash=chash, final_url=url, http_status=200,
                changed=1, authenticated=1 if auth_by else 0,
                authenticated_by=auth_by,
            )
    return page_id


# ---- diff 뷰어 접근제한 (치명 #6/#7) ----


def test_diff_blocks_authenticated_for_non_owner(client):
    owner = _uid("arch@test.co")
    page_id = _make_page_with_snaps(owner, [
        ("hashA", "비밀버전1", owner),
        ("hashB", "비밀버전2", owner),
    ])
    # 비소유자(viewer) — diff 본문 누출 차단(404)
    _login(client, "viewer@test.co")
    assert client.get(f"/diff/{page_id}").status_code == 404
    assert client.get(f"/diff/{page_id}/shotdiff").status_code == 404
    # 소유자(arch) — 열람 가능
    _login(client, "arch@test.co")
    r = client.get(f"/diff/{page_id}")
    assert r.status_code == 200 and "비밀버전" in r.text
    # 관리자 — 열람 가능
    _login(client, "boss@test.co", "bosspass1234")
    assert client.get(f"/diff/{page_id}").status_code == 200


# ---- 타임라인 접근제한 (높음 #8) ----


def test_timeline_hides_authenticated_from_non_owner(client):
    owner = _uid("arch@test.co")
    page_id = _make_page_with_snaps(owner, [
        ("publichash00", "공개", None),
        ("secrethash11", "비밀", owner),
    ])
    _login(client, "viewer@test.co")
    body = client.get(f"/page/{page_id}").text
    assert "publichash00" in body
    assert "secrethash11" not in body  # 인증 스냅샷 해시·존재 숨김
    _login(client, "arch@test.co")
    assert "secrethash11" in client.get(f"/page/{page_id}").text  # 소유자는 본다


# ---- 사용자 삭제 FK 정리 (높음 #4/#10) ----


def test_delete_user_cleans_capsule_and_auth_snapshot(client):
    owner = _uid("arch@test.co")
    page_id = _make_page_with_snaps(owner, [("h1", "x", owner)])
    with db.connect() as conn:
        db.create_auth_capsule(conn, url="https://example.com/secret",
                               scope_host="example.com", owner_user_id=owner,
                               ciphertext=b"c", network_tag_id=None, ttl_seconds=3600)
        db.delete_user(conn, owner)  # IntegrityError 없이 성공해야 한다
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM auth_capsules WHERE owner_user_id = ?", (owner,)
        ).fetchone()["c"] == 0
        # 스냅샷은 보존되되 소유자 표기만 끊긴다(이후 admin 전용)
        snap = db.list_snapshots(conn, page_id)[0]
        assert snap["authenticated"] == 1 and snap["authenticated_by"] is None


# ---- http 인증 캡처 거부 (중간 #1) ----


def test_auth_profile_rejects_http_target(client):
    token = _ext_token("arch@test.co")
    r = client.post(
        "/api/v1/auth-profiles",
        json={"url": "http://example.com/x",
              "storage_state": {"cookies": [{"name": "s", "value": "v",
                                             "domain": "example.com"}]}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400  # https 전용


def test_pipeline_blocks_http_authenticated_capture(tmp_db, monkeypatch):
    _seed()
    # https 승격 프로브를 막아(네트워크 차단) http 가 유지되게 한다
    monkeypatch.setattr(web_app.pipeline, "upgrade_http_to_https", lambda u: u)
    called = []
    monkeypatch.setattr(web_app.pipeline.capture, "capture",
                        lambda *a, **k: called.append(1))
    with pytest.raises(ValueError):
        web_app.pipeline.archive_url(
            "http://93.184.216.34/x", storage_state={"cookies": []},
        )
    assert called == []  # 캡처는 호출되지 않는다


# ---- 쿠키 스코프 강화 (높음 #11) ----


def test_cookie_scope_validation(client):
    token = _ext_token("arch@test.co")
    hdr = {"Authorization": f"Bearer {token}"}

    def post(cookies):
        return client.post(
            "/api/v1/auth-profiles",
            json={"url": "https://example.com/x", "storage_state": {"cookies": cookies}},
            headers=hdr,
        )

    # 빈/누락 도메인 거부
    assert post([{"name": "s", "value": "v"}]).status_code == 400
    assert post([{"name": "s", "value": "v", "domain": ""}]).status_code == 400
    # TLD(점 없는) 거부
    assert post([{"name": "s", "value": "v", "domain": "com"}]).status_code == 400
    # 대상 호스트 밖 거부
    assert post([{"name": "s", "value": "v", "domain": "evil.com"}]).status_code == 400
    # url 필드도 같은 규칙으로 검증(밖이면 거부)
    assert post([{"name": "s", "value": "v", "url": "https://evil.com/"}]).status_code == 400


def test_cookie_count_limit(client):
    token = _ext_token("arch@test.co")
    many = [{"name": f"c{i}", "value": "v", "domain": "example.com"} for i in range(60)]
    r = client.post(
        "/api/v1/auth-profiles",
        json={"url": "https://example.com/x", "storage_state": {"cookies": many}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 413


# ---- cross-origin 자원 차단 (높음 #2 / 중간 #3) ----


def test_same_origin_helper():
    assert capture._same_origin("https://a.com/x", "https://a.com/y") is True
    assert capture._same_origin("https://a.com:443/x", "https://a.com/y") is True
    assert capture._same_origin("http://a.com/x", "https://a.com/y") is False  # 스킴
    assert capture._same_origin("https://cdn.a.com/x", "https://a.com/y") is False  # 호스트


def test_block_cross_origin_route_aborts():
    class FakeReq:
        def __init__(self, url):
            self.url = url

    class FakeRoute:
        def __init__(self, url):
            self.request = FakeReq(url)
            self.action = None

        def abort(self):
            self.action = "abort"

        def continue_(self):
            self.action = "continue"

    handler = capture._block_cross_origin_route("https://example.com/page")
    same = FakeRoute("https://example.com/style.css")
    sibling = FakeRoute("https://cdn.example.com/x.png")
    downgrade = FakeRoute("http://example.com/x.png")
    third = FakeRoute("https://tracker.evil.com/p.gif")
    for r in (same, sibling, downgrade, third):
        handler(r)
    assert same.action == "continue"
    assert sibling.action == "abort"      # 형제 서브도메인 차단 (쿠키 누출 #2)
    assert downgrade.action == "abort"    # 스킴 다운그레이드 차단 (#3)
    assert third.action == "abort"        # 제3자 차단
