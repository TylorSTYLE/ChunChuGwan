"""사이트 로그인 자격증명 — crypto·db·코어(credentials)·관리 화면 테스트."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from chunchugwan import (
    archive_worker, auth, config, credentials, crypto, db, deletion, storage,
)
from chunchugwan.web import app as web_app

URL = "https://example.com/post"
SITE_KEY = "example.com"


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """임시 아카이브 DB 환경 + 자격증명 암호화 키 설정."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret-key")


def _make_site(conn) -> int:
    """example.com 사이트(+페이지 1개)를 만들고 site_id 반환."""
    domain, slug = "example.com", storage.url_to_slug(URL)
    db.get_or_create_page(conn, URL, domain, slug)
    return db.get_site_by_key(conn, SITE_KEY)["id"]


def _sid() -> int:
    with db.connect() as conn:
        return db.get_site_by_key(conn, SITE_KEY)["id"]


# ---- crypto 계층 ----


def test_crypto_roundtrip(tmp_db):
    ct = crypto.encrypt("비밀 secret")
    assert ct != "비밀 secret"               # 평문이 아니다
    assert crypto.decrypt(ct) == "비밀 secret"


def test_crypto_missing_key(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "")
    assert crypto.is_configured() is False
    with pytest.raises(crypto.SecretKeyMissing):
        crypto.encrypt("x")


def test_crypto_wrong_key_fails(tmp_db, monkeypatch):
    ct = crypto.encrypt("x")
    monkeypatch.setattr(config, "SECRET_KEY", "different")
    with pytest.raises(crypto.SecretDecryptError):
        crypto.decrypt(ct)


# ---- db 계층 ----


def test_db_crud(tmp_db):
    with db.connect() as conn:
        site_id = _make_site(conn)
        cid = db.create_site_credential(
            conn, site_id, "admin", "http_basic", "ENC", created_by=None
        )
        row = db.get_site_credential(conn, cid)
        assert row["label"] == "admin" and row["secret"] == "ENC"
        lst = db.list_site_credentials(conn, site_id)
        assert len(lst) == 1
        assert "secret" not in lst[0].keys()    # 목록은 암호문을 노출하지 않는다
        assert db.count_site_credentials(conn, site_id) == 1
        assert db.get_site_credential_by_label(conn, site_id, "admin")["id"] == cid
        assert db.delete_site_credential(conn, cid) is True
        assert db.delete_site_credential(conn, cid) is False


def test_db_unique_label(tmp_db):
    with db.connect() as conn:
        site_id = _make_site(conn)
        db.create_site_credential(conn, site_id, "dup", "http_basic", "E", created_by=None)
        with pytest.raises(sqlite3.IntegrityError):
            db.create_site_credential(
                conn, site_id, "dup", "session", "E2", created_by=None
            )


def test_prune_empty_site_clears_credentials(tmp_db):
    """페이지·크롤이 없는 사이트를 prune 할 때 자격증명도 정리된다 (FK 안전)."""
    with db.connect() as conn:
        site_id = db.get_or_create_site(conn, SITE_KEY)
        db.create_site_credential(conn, site_id, "c", "http_basic", "E", created_by=None)
        assert db.prune_site_if_empty(conn, site_id) is True   # FK 위반 없이
        assert db.get_site(conn, site_id) is None
        assert db.count_site_credentials(conn, site_id) == 0


def test_delete_site_with_credentials(tmp_db):
    """자격증명이 있는 사이트도 deletion.delete_site 가 FK 오류 없이 지운다."""
    with db.connect() as conn:
        site_id = _make_site(conn)
        db.create_site_credential(conn, site_id, "c", "http_basic", "E", created_by=None)
    assert deletion.delete_site(site_id, hard=True) is not None
    with db.connect() as conn:
        assert db.get_site(conn, site_id) is None
        assert db.count_site_credentials(conn, site_id) == 0


def _page_credential(conn, page_id):
    return conn.execute(
        "SELECT credential_id FROM pages WHERE id = ?", (page_id,)
    ).fetchone()["credential_id"]


def test_get_or_create_page_links_and_updates_credential(tmp_db):
    """get_or_create_page 가 credential_id 를 신규 저장하고, 재호출 시 갱신한다."""
    slug = storage.url_to_slug(URL)
    with db.connect() as conn:
        site_id = db.get_or_create_site(conn, SITE_KEY)
        c1 = credentials.add(conn, site_id, "a", "http_basic",
                             {"username": "u", "password": "p"}, created_by=None)
        c2 = credentials.add(conn, site_id, "b", "jwt", {"token": "t.t.t"}, created_by=None)
        pid = db.get_or_create_page(conn, URL, "example.com", slug, credential_id=c1)
        assert _page_credential(conn, pid) == c1
        # 같은 URL 재호출 + 다른 credential_id → 갱신 (재아카이빙으로 연결 변경)
        assert db.get_or_create_page(conn, URL, "example.com", slug, credential_id=c2) == pid
        assert _page_credential(conn, pid) == c2
        # credential_id 안 주면 기존 연결 유지
        db.get_or_create_page(conn, URL, "example.com", slug)
        assert _page_credential(conn, pid) == c2


def test_delete_credential_nulls_page_link(tmp_db):
    """자격증명을 삭제하면 연결한 페이지의 credential_id 가 NULL 이 된다 (FK 안전)."""
    slug = storage.url_to_slug(URL)
    with db.connect() as conn:
        site_id = db.get_or_create_site(conn, SITE_KEY)
        cid = credentials.add(conn, site_id, "a", "jwt", {"token": "t.t.t"}, created_by=None)
        pid = db.get_or_create_page(conn, URL, "example.com", slug, credential_id=cid)
        assert db.delete_site_credential(conn, cid) is True
        assert _page_credential(conn, pid) is None


# ---- 코어 모듈 (credentials) ----


def test_build_payload_http_basic(tmp_db):
    p = credentials.build_payload("http_basic", {"username": " u ", "password": "pw"})
    assert p == {"username": "u", "password": "pw"}    # 사용자명은 trim
    for bad in ({"username": "", "password": "p"}, {"username": "u", "password": ""}):
        with pytest.raises(credentials.CredentialError):
            credentials.build_payload("http_basic", bad)


def test_build_payload_session(tmp_db):
    state = '{"cookies": [{"name": "s", "value": "1", "domain": "ex.com", "path": "/"}], "origins": []}'
    p = credentials.build_payload("session", {"storage_state": state})
    assert p["storage_state"]["cookies"][0]["name"] == "s"
    for bad in ("", "not json", '{"no": "cookies"}', "[]"):
        with pytest.raises(credentials.CredentialError):
            credentials.build_payload("session", {"storage_state": bad})


def test_build_payload_session_normalizes_cookies(tmp_db):
    # path 가 빠진 쿠키는 "/" 로 채운다 (브라우저/확장 내보내기 흔한 누락)
    state = '{"cookies": [{"name": "s", "value": "1", "domain": "ex.com"}]}'
    p = credentials.build_payload("session", {"storage_state": state})
    assert p["storage_state"]["cookies"][0]["path"] == "/"
    # domain·url 둘 다 없는 쿠키는 명확한 오류로 거부 (Playwright 가 거부하는 형태)
    bad = '{"cookies": [{"name": "s", "value": "1"}]}'
    with pytest.raises(credentials.CredentialError):
        credentials.build_payload("session", {"storage_state": bad})


def test_build_payload_jwt(tmp_db):
    p = credentials.build_payload("jwt", {"token": "  eyJ.aaa.bbb  "})
    assert p == {"token": "eyJ.aaa.bbb"}              # 앞뒤 공백 제거
    for bad in ({"token": ""}, {"token": "has space"}, {"token": "a\nb"}):
        with pytest.raises(credentials.CredentialError):
            credentials.build_payload("jwt", bad)      # 빈 값·내부 공백·줄바꿈 거부


def test_build_payload_bad_kind(tmp_db):
    with pytest.raises(credentials.CredentialError):
        credentials.build_payload("nope", {})


def test_validate_label(tmp_db):
    assert credentials.validate_label("ok") is None
    assert credentials.validate_label("") is not None
    assert credentials.validate_label("x" * 51) is not None


def test_add_and_reveal_roundtrip(tmp_db):
    with db.connect() as conn:
        site_id = _make_site(conn)
        payload = {"username": "u", "password": "secret-pw"}
        cid = credentials.add(
            conn, site_id, "admin", "http_basic", payload, created_by=None
        )
        stored = db.get_site_credential(conn, cid)["secret"]
        assert "secret-pw" not in stored          # 평문이 저장되지 않는다
        assert credentials.reveal(conn, cid) == payload
        assert credentials.reveal(conn, 999_999) is None


# ---- 관리 화면 (웹) ----


@pytest.fixture
def client(tmp_db):
    """관리자 + 보기 전용 + example.com 사이트가 있는 TestClient."""
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(
            conn, "viewer@test.co", auth.hash_password("password1234"), role="viewer"
        )
        _make_site(conn)
    web_app._active_jobs.clear()
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def _login(client, email, pw):
    return client.post(
        "/login", data={"email": email, "password": pw}, follow_redirects=False
    )


def _login_admin(client):
    return _login(client, "boss@test.co", "bosspass1234")


def _basic(label="admin", username="u", password="pw"):
    return {"label": label, "kind": "http_basic", "username": username, "password": password}


# ---- 새 아카이빙 폼에서 자격증명 등록 ----


class _Outcome:
    status = "unchanged"


def _stub_archive(monkeypatch):
    """캡처(pipeline.archive_url)를 가로채 호출 인자(url·credential_id 등)를 기록.

    아카이빙은 이제 archive_worker 가 큐를 소비해 실행하므로, POST 후 _drain()
    으로 큐를 비워야 fake 가 호출된다.
    """
    calls = []

    def fake(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _Outcome()

    monkeypatch.setattr(archive_worker.pipeline, "archive_url", fake)
    return calls


def _drain():
    """큐에 쌓인 단발 아카이빙 작업을 동기로 모두 처리 (테스트 전용)."""
    while archive_worker.process_next() is not None:
        pass


def _archive_data(**over):
    data = {
        "url": "https://login.example.org/app",
        "cred_existing_id": "__new__",
        "cred_kind": "http_basic",
        "cred_username": "siteuser",
        "cred_password": "sitepass",
    }
    data.update(over)
    return data


def _site_id_for(site_key):
    with db.connect() as conn:
        row = db.get_site_by_key(conn, site_key)
        return row["id"] if row else None


# ---- 기존 자격증명 연결 (조회 엔드포인트 + 연결) ----


# ---- HAR → storage_state 변환 (코어) ----


def _har_entry(url, *, req_cookies=None, resp_cookies=None, req_headers=None):
    """HAR entry 한 개를 만든다 (request/response cookies·headers)."""
    return {
        "request": {"url": url, "cookies": req_cookies or [], "headers": req_headers or []},
        "response": {"cookies": resp_cookies or [], "headers": []},
    }


def _har(entries):
    """HAR(JSON 문자열)을 만든다."""
    return json.dumps({"log": {"version": "1.2", "entries": entries}})


def test_har_extracts_cookies_with_attrs():
    har = _har([_har_entry(
        "https://app.example.com/login",
        resp_cookies=[{"name": "sid", "value": "abc", "domain": "app.example.com",
                       "path": "/", "httpOnly": True, "secure": True}],
    )])
    state = credentials.storage_state_from_har(har)
    assert state["origins"] == []
    c = state["cookies"][0]
    assert c["name"] == "sid" and c["value"] == "abc"
    assert c["domain"] == "app.example.com" and c["path"] == "/"
    assert c["httpOnly"] is True and c["secure"] is True


def test_har_uses_request_host_when_cookie_has_no_domain():
    har = _har([_har_entry(
        "https://app.example.com/x", req_cookies=[{"name": "sid", "value": "v"}]
    )])
    c = credentials.storage_state_from_har(har)["cookies"][0]
    assert c["domain"] == "app.example.com" and c["path"] == "/"


def test_har_cookie_header_fallback_when_array_empty():
    har = _har([_har_entry(
        "https://h.example.com/a",
        req_headers=[{"name": "Cookie", "value": "a=1; b=2"}],
    )])
    names = {c["name"]: c["value"] for c in credentials.storage_state_from_har(har)["cookies"]}
    assert names == {"a": "1", "b": "2"}


def test_har_latest_value_wins_and_cleared_dropped():
    har = _har([
        _har_entry("https://e.com/1", resp_cookies=[
            {"name": "s", "value": "old", "domain": "e.com", "path": "/"}]),
        _har_entry("https://e.com/2", resp_cookies=[
            {"name": "s", "value": "new", "domain": "e.com", "path": "/"}]),
        _har_entry("https://e.com/3", resp_cookies=[
            {"name": "gone", "value": "x", "domain": "e.com", "path": "/"}]),
        _har_entry("https://e.com/4", resp_cookies=[
            {"name": "gone", "value": "", "domain": "e.com", "path": "/"}]),
    ])
    cookies = {c["name"]: c["value"] for c in credentials.storage_state_from_har(har)["cookies"]}
    assert cookies == {"s": "new"}        # 마지막 값 채택 + 빈 값(삭제) 제외


def test_har_parses_expires_and_samesite():
    har = _har([_har_entry("https://e.com/", resp_cookies=[
        {"name": "s", "value": "1", "domain": "e.com", "path": "/",
         "expires": "2030-01-01T00:00:00.000Z", "sameSite": "lax"}])])
    c = credentials.storage_state_from_har(har)["cookies"][0]
    assert c["expires"] > 0 and c["sameSite"] == "Lax"


def test_har_result_passes_build_payload():
    """HAR 결과 storage_state 가 build_payload(session) 검증을 그대로 통과한다."""
    har = _har([_har_entry("https://e.com/", resp_cookies=[
        {"name": "s", "value": "1", "domain": "e.com", "path": "/"}])])
    state = credentials.storage_state_from_har(har)
    payload = credentials.build_payload("session", {"storage_state": json.dumps(state)})
    assert payload["storage_state"]["cookies"][0]["value"] == "1"


def test_har_rejects_bad_input():
    for bad in ("not json", "{}", '{"log": {}}', '{"log": {"entries": []}}',
                _har([_har_entry("https://e.com/")])):   # 쿠키 없음
        with pytest.raises(credentials.CredentialError):
            credentials.storage_state_from_har(bad)


def test_har_rejects_oversize(monkeypatch):
    monkeypatch.setattr(credentials, "MAX_HAR_BYTES", 10)
    with pytest.raises(credentials.CredentialError):
        credentials.storage_state_from_har(b"x" * 11)


def test_har_scopes_cookies_to_site_base_domain():
    """site_host 를 주면 그 등록 도메인(서브도메인 포함) 쿠키만 남기고
    무관한 서드파티 쿠키는 버린다."""
    har = _har([
        _har_entry("https://app.example.com/login", resp_cookies=[
            {"name": "sid", "value": "1", "domain": "app.example.com", "path": "/"}]),
        _har_entry("https://cdn.example.com/a", resp_cookies=[
            {"name": "cdn", "value": "1", "domain": "cdn.example.com", "path": "/"}]),
        _har_entry("https://analytics.tracker.io/p", resp_cookies=[
            {"name": "ga", "value": "1", "domain": "analytics.tracker.io", "path": "/"}]),
    ])
    state = credentials.storage_state_from_har(har, site_host="app.example.com")
    assert {c["name"] for c in state["cookies"]} == {"sid", "cdn"}   # tracker.io 제외


def test_har_scope_without_matching_cookie_errors():
    har = _har([_har_entry("https://other.io/x", resp_cookies=[
        {"name": "x", "value": "1", "domain": "other.io", "path": "/"}])])
    with pytest.raises(credentials.CredentialError):
        credentials.storage_state_from_har(har, site_host="example.com")


def test_har_expires_naive_treated_as_utc():
    """타임존 없는 expires 는 서버 로컬이 아니라 UTC 로 해석한다 (결정적)."""
    from datetime import datetime, timezone
    har = _har([_har_entry("https://e.com/", resp_cookies=[
        {"name": "s", "value": "1", "domain": "e.com", "path": "/",
         "expires": "2030-01-01T00:00:00"}])])
    c = credentials.storage_state_from_har(har)["cookies"][0]
    assert c["expires"] == datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()


# ---- HAR 업로드로 세션 자격증명 등록 (웹) ----


