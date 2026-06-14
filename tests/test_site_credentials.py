"""사이트 로그인 자격증명 — crypto·db·코어(credentials)·관리 화면 테스트."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, credentials, crypto, db, deletion, storage
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
    assert deletion.delete_site(site_id) is not None
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


def test_page_admin_only(client):
    sid = _sid()
    _login(client, "viewer@test.co", "password1234")
    assert client.get(f"/sites/{sid}/credentials").status_code == 403
    assert client.post(f"/sites/{sid}/credentials", data=_basic()).status_code == 403


def test_page_renders(client):
    _login_admin(client)
    r = client.get(f"/sites/{_sid()}/credentials")
    assert r.status_code == 200
    assert "example.com" in r.text
    assert 'name="kind"' in r.text          # 종류 셀렉터
    assert 'name="storage_state"' in r.text  # 세션 입력란
    assert 'value="jwt"' in r.text and 'name="token"' in r.text  # JWT 종류·입력란
    assert '<button type="submit" disabled>' not in r.text  # 키 설정 시 폼 활성


def test_create_http_basic(client):
    _login_admin(client)
    sid = _sid()
    r = client.post(f"/sites/{sid}/credentials", data=_basic(), follow_redirects=False)
    assert r.status_code == 303
    with db.connect() as conn:
        creds = db.list_site_credentials(conn, sid)
        assert len(creds) == 1 and creds[0]["kind"] == "http_basic"
        cid = creds[0]["id"]
        assert credentials.reveal(conn, cid) == {"username": "u", "password": "pw"}
        assert "pw" not in db.get_site_credential(conn, cid)["secret"]


def test_create_session(client):
    _login_admin(client)
    sid = _sid()
    state = '{"cookies": [{"name": "sid", "value": "abc", "domain": "ex.com", "path": "/"}], "origins": []}'
    r = client.post(
        f"/sites/{sid}/credentials",
        data={"label": "sess", "kind": "session", "storage_state": state},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with db.connect() as conn:
        cred = db.list_site_credentials(conn, sid)[0]
        assert cred["kind"] == "session"
        revealed = credentials.reveal(conn, cred["id"])
        assert revealed["storage_state"]["cookies"][0]["value"] == "abc"


def test_create_jwt(client):
    _login_admin(client)
    sid = _sid()
    r = client.post(
        f"/sites/{sid}/credentials",
        data={"label": "api", "kind": "jwt", "token": "eyJ.aaa.bbb"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with db.connect() as conn:
        cred = db.list_site_credentials(conn, sid)[0]
        assert cred["kind"] == "jwt"
        assert credentials.reveal(conn, cred["id"]) == {"token": "eyJ.aaa.bbb"}
        assert "eyJ.aaa.bbb" not in db.get_site_credential(conn, cred["id"])["secret"]


def test_create_duplicate_label_rejected(client):
    _login_admin(client)
    sid = _sid()
    client.post(f"/sites/{sid}/credentials", data=_basic(label="dup"), follow_redirects=False)
    r = client.post(f"/sites/{sid}/credentials", data=_basic(label="dup"), follow_redirects=False)
    assert "error=" in r.headers["location"]
    with db.connect() as conn:
        assert db.count_site_credentials(conn, sid) == 1


def test_create_rejects_bad_input(client):
    _login_admin(client)
    sid = _sid()
    bad_inputs = [
        _basic(label=""),                                   # 라벨 없음
        _basic(password=""),                                # 비번 없음
        {"label": "x", "kind": "bogus"},                    # 잘못된 종류
        {"label": "y", "kind": "session", "storage_state": "not json"},  # 잘못된 JSON
    ]
    for data in bad_inputs:
        r = client.post(f"/sites/{sid}/credentials", data=data, follow_redirects=False)
        assert "error=" in r.headers["location"], data
    with db.connect() as conn:
        assert db.count_site_credentials(conn, sid) == 0


def test_create_blocked_without_secret_key(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "")
    _login_admin(client)
    sid = _sid()
    r = client.post(f"/sites/{sid}/credentials", data=_basic(), follow_redirects=False)
    assert "error=" in r.headers["location"]
    with db.connect() as conn:
        assert db.count_site_credentials(conn, sid) == 0


def test_page_warns_and_disables_form_without_secret_key(client, monkeypatch):
    """키 미설정 시 경고 배너를 띄우고 등록 폼을 비활성화한다."""
    monkeypatch.setattr(config, "SECRET_KEY", "")
    _login_admin(client)
    html = client.get(f"/sites/{_sid()}/credentials").text
    assert "WCCG_SECRET_KEY" in html                       # 경고 배너
    assert "자격증명을 저장할 수 없습니다" in html
    assert '<button type="submit" disabled>' in html       # 등록 버튼 잠금
    assert 'id="cred-kind" disabled' in html               # 종류 셀렉터 잠금


def test_delete_credential(client):
    _login_admin(client)
    sid = _sid()
    client.post(f"/sites/{sid}/credentials", data=_basic(label="d"), follow_redirects=False)
    with db.connect() as conn:
        cid = db.list_site_credentials(conn, sid)[0]["id"]
    r = client.post(f"/sites/{sid}/credentials/{cid}/delete", follow_redirects=False)
    assert r.status_code == 303
    with db.connect() as conn:
        assert db.count_site_credentials(conn, sid) == 0


def test_delete_wrong_site_404(client):
    _login_admin(client)
    sid = _sid()
    client.post(f"/sites/{sid}/credentials", data=_basic(label="d"), follow_redirects=False)
    with db.connect() as conn:
        cid = db.list_site_credentials(conn, sid)[0]["id"]
    r = client.post(f"/sites/99999/credentials/{cid}/delete", follow_redirects=False)
    assert r.status_code == 404


# ---- 새 아카이빙 폼에서 자격증명 등록 ----


class _Outcome:
    status = "unchanged"


def _stub_archive(monkeypatch):
    """/archive 의 실제 캡처를 가로채고 호출 인자(url·credential_id 등)를 기록."""
    calls = []

    def fake(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _Outcome()

    monkeypatch.setattr(web_app.pipeline, "archive_url", fake)
    return calls


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


def test_archive_form_shows_credential_section_for_admin(client):
    _login_admin(client)
    html = client.get("/archive/new").text
    assert 'name="cred_existing_id"' in html and 'id="cred-select"' in html
    assert 'name="cred_kind"' in html
    assert 'name="cred_storage_state"' in html
    assert 'name="cred_token"' in html and 'value="jwt"' in html


def test_archive_form_hides_credential_section_for_archiver(client):
    with db.connect() as conn:
        db.create_user(
            conn, "arch@test.co", auth.hash_password("password1234"), role="archiver"
        )
    _login(client, "arch@test.co", "password1234")
    html = client.get("/archive/new").text
    assert 'name="cred_existing_id"' not in html


def test_archive_stores_credential_for_new_site(client, monkeypatch):
    calls = _stub_archive(monkeypatch)
    _login_admin(client)
    r = client.post("/archive", data=_archive_data(), follow_redirects=False)
    assert r.status_code == 303
    sid = _site_id_for("login.example.org")
    assert sid is not None                       # 자격증명이 사이트를 생성한다
    with db.connect() as conn:
        creds = db.list_site_credentials(conn, sid)
        assert len(creds) == 1
        assert creds[0]["label"] == "siteuser"   # 자동 라벨 = 사용자명
        assert credentials.reveal(conn, creds[0]["id"]) == {
            "username": "siteuser", "password": "sitepass"
        }
        new_id = creds[0]["id"]
    assert len(calls) == 1                        # 아카이빙도 트리거됨
    assert calls[0]["credential_id"] == new_id    # 새 자격증명이 페이지에 연결됨


def test_archive_stores_credential_for_existing_site(client, monkeypatch):
    _stub_archive(monkeypatch)
    _login_admin(client)
    r = client.post(
        "/archive",
        data=_archive_data(url="https://example.com/x", cred_label="admin"),
        follow_redirects=False,
    )
    assert r.status_code == 303
    with db.connect() as conn:
        assert db.get_site_credential_by_label(conn, _sid(), "admin") is not None


def test_archive_stores_session_credential(client, monkeypatch):
    _stub_archive(monkeypatch)
    _login_admin(client)
    state = '{"cookies": [{"name": "s", "value": "1", "domain": "sess.example.org", "path": "/"}], "origins": []}'
    r = client.post(
        "/archive",
        data={
            "url": "https://sess.example.org/", "cred_existing_id": "__new__",
            "cred_kind": "session", "cred_storage_state": state,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    sid = _site_id_for("sess.example.org")
    with db.connect() as conn:
        cred = db.list_site_credentials(conn, sid)[0]
        assert cred["kind"] == "session" and cred["label"] == "세션"


def test_archive_stores_jwt_credential(client, monkeypatch):
    _stub_archive(monkeypatch)
    _login_admin(client)
    r = client.post(
        "/archive",
        data={"url": "https://jwt.example.org/", "cred_existing_id": "__new__",
              "cred_kind": "jwt", "cred_token": "eyJ.x.y"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    sid = _site_id_for("jwt.example.org")
    with db.connect() as conn:
        cred = db.list_site_credentials(conn, sid)[0]
        assert cred["kind"] == "jwt" and cred["label"] == "JWT"   # 자동 라벨
        assert credentials.reveal(conn, cred["id"]) == {"token": "eyJ.x.y"}


def test_archive_credential_ignored_for_non_admin(client, monkeypatch):
    calls = _stub_archive(monkeypatch)
    with db.connect() as conn:
        db.create_user(
            conn, "arch@test.co", auth.hash_password("password1234"), role="archiver"
        )
    _login(client, "arch@test.co", "password1234")
    r = client.post("/archive", data=_archive_data(), follow_redirects=False)
    assert r.status_code == 303
    assert len(calls) == 1                        # 아카이빙은 진행
    assert _site_id_for("login.example.org") is None   # 자격증명 경로는 무시됨


def test_archive_without_credential_selection_skips(client, monkeypatch):
    calls = _stub_archive(monkeypatch)
    _login_admin(client)
    r = client.post(
        "/archive",
        data={"url": "https://nocred.example.org/", "cred_kind": "http_basic",
              "cred_username": "u", "cred_password": "p"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert len(calls) == 1
    assert calls[0].get("credential_id") is None         # 연결 안 함
    assert _site_id_for("nocred.example.org") is None  # 자격증명 미저장


def test_archive_invalid_credential_blocks_and_skips_archive(client, monkeypatch):
    calls = _stub_archive(monkeypatch)
    _login_admin(client)
    r = client.post(
        "/archive", data=_archive_data(cred_password=""), follow_redirects=False
    )
    assert r.status_code == 303
    assert "/archive/new?" in r.headers["location"]
    assert "error=" in r.headers["location"]
    assert calls == []                            # 자격증명 오류 시 아카이빙도 안 함
    assert _site_id_for("login.example.org") is None


def test_archive_credential_password_not_leaked_in_redirect(client, monkeypatch):
    _stub_archive(monkeypatch)
    _login_admin(client)
    r = client.post(
        "/archive",
        data=_archive_data(cred_username="", cred_password="topsecret"),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "topsecret" not in r.headers["location"]   # 비밀번호가 URL 에 실리지 않는다


def test_archive_duplicate_label_blocks(client, monkeypatch):
    _stub_archive(monkeypatch)
    _login_admin(client)
    client.post("/archive", data=_archive_data(cred_label="dup"), follow_redirects=False)
    r = client.post("/archive", data=_archive_data(cred_label="dup"), follow_redirects=False)
    assert "error=" in r.headers["location"]
    with db.connect() as conn:
        assert db.count_site_credentials(conn, _site_id_for("login.example.org")) == 1


def test_archive_credential_blocked_without_secret_key(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "")
    _stub_archive(monkeypatch)
    _login_admin(client)
    r = client.post("/archive", data=_archive_data(), follow_redirects=False)
    assert "error=" in r.headers["location"]
    assert _site_id_for("login.example.org") is None


# ---- 기존 자격증명 연결 (조회 엔드포인트 + 연결) ----


def test_archive_credentials_endpoint(client):
    _login_admin(client)
    sid = _sid()
    with db.connect() as conn:
        credentials.add(conn, sid, "관리자", "jwt", {"token": "eyJ.x.y"}, created_by=None)
    # www 변형도 같은 사이트(example.com)로 매핑된다
    body = client.get(
        "/archive/credentials", params={"url": "https://www.example.com/p"}
    ).json()
    assert body["site_key"] == "example.com"
    assert len(body["credentials"]) == 1
    item = body["credentials"][0]
    assert item["label"] == "관리자" and item["kind"] == "jwt"
    assert "secret" not in item and "token" not in item    # 비밀은 안 내려간다
    # 자격증명 없는 도메인·빈 url → 빈 목록
    assert client.get(
        "/archive/credentials", params={"url": "https://nope.test/"}
    ).json()["credentials"] == []
    assert client.get("/archive/credentials").json()["credentials"] == []


def test_archive_credentials_endpoint_admin_only(client):
    _login(client, "viewer@test.co", "password1234")
    r = client.get("/archive/credentials", params={"url": "https://example.com/"})
    assert r.status_code == 403


def test_archive_connect_existing_credential(client, monkeypatch):
    calls = _stub_archive(monkeypatch)
    _login_admin(client)
    sid = _sid()
    with db.connect() as conn:
        cid = credentials.add(conn, sid, "관리자", "http_basic",
                              {"username": "u", "password": "p"}, created_by=None)
    r = client.post(
        "/archive",
        data={"url": "https://example.com/x", "cred_existing_id": str(cid)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert len(calls) == 1
    assert calls[0]["credential_id"] == cid    # 연결된 자격증명이 아카이브로 전달됨
    # 새 자격증명을 만들지 않는다
    with db.connect() as conn:
        assert db.count_site_credentials(conn, sid) == 1


def test_archive_connect_wrong_domain_rejected(client, monkeypatch):
    calls = _stub_archive(monkeypatch)
    _login_admin(client)
    sid = _sid()
    with db.connect() as conn:
        cid = credentials.add(conn, sid, "x", "http_basic",
                              {"username": "u", "password": "p"}, created_by=None)
    # example.com 의 자격증명을 다른 도메인 아카이빙에 연결 시도 → 거부 + 아카이빙도 안 함
    r = client.post(
        "/archive",
        data={"url": "https://other.org/", "cred_existing_id": str(cid)},
        follow_redirects=False,
    )
    assert "error=" in r.headers["location"]
    assert calls == []


def test_archive_connect_nonexistent_credential_rejected(client, monkeypatch):
    calls = _stub_archive(monkeypatch)
    _login_admin(client)
    r = client.post(
        "/archive",
        data={"url": "https://example.com/x", "cred_existing_id": "99999"},
        follow_redirects=False,
    )
    assert "error=" in r.headers["location"]
    assert calls == []


def test_archive_connect_ignored_for_non_admin(client, monkeypatch):
    calls = _stub_archive(monkeypatch)
    sid = _sid()
    with db.connect() as conn:
        cid = credentials.add(conn, sid, "x", "http_basic",
                              {"username": "u", "password": "p"}, created_by=None)
        db.create_user(
            conn, "arch@test.co", auth.hash_password("password1234"), role="archiver"
        )
    _login(client, "arch@test.co", "password1234")
    # 아카이버가 연결 id 를 보내도 무시되고 아카이빙만 진행 (자격증명은 관리자 전용)
    r = client.post(
        "/archive",
        data={"url": "https://example.com/x", "cred_existing_id": str(cid)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert len(calls) == 1 and calls[0].get("credential_id") is None


def test_archive_crawl_threads_credential(client):
    """사이트 전체 아카이브(크롤)도 연결한 자격증명을 crawls.credential_id 에 싣는다."""
    _login_admin(client)
    sid = _sid()
    with db.connect() as conn:
        cid = credentials.add(conn, sid, "c", "http_basic",
                              {"username": "u", "password": "p"}, created_by=None)
    r = client.post(
        "/archive",
        data={"url": "https://example.com/sec/", "site": "on",
              "cred_existing_id": str(cid)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/crawls/" in r.headers["location"]
    with db.connect() as conn:
        crawl = db.find_running_crawl(conn, "https://example.com/sec/")
        assert crawl is not None and crawl["credential_id"] == cid


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


def test_create_session_credential_from_har(client):
    _login_admin(client)
    sid = _sid()
    har = _har([_har_entry("https://example.com/login", resp_cookies=[
        {"name": "sid", "value": "fromhar", "domain": "example.com", "path": "/"}])])
    r = client.post(
        f"/sites/{sid}/credentials",
        data={"label": "har-sess", "kind": "session"},
        files={"har_file": ("login.har", har, "application/json")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with db.connect() as conn:
        cred = db.list_site_credentials(conn, sid)[0]
        assert cred["kind"] == "session"
        revealed = credentials.reveal(conn, cred["id"])
        assert revealed["storage_state"]["cookies"][0]["value"] == "fromhar"


def test_create_session_har_overrides_pasted_json(client):
    """HAR 을 올리면 같이 보낸 storage_state JSON 은 무시된다 (HAR 우선)."""
    _login_admin(client)
    sid = _sid()
    har = _har([_har_entry("https://example.com/", resp_cookies=[
        {"name": "win", "value": "har", "domain": "example.com", "path": "/"}])])
    pasted = '{"cookies": [{"name": "lose", "value": "json", "domain": "example.com", "path": "/"}]}'
    r = client.post(
        f"/sites/{sid}/credentials",
        data={"label": "s", "kind": "session", "storage_state": pasted},
        files={"har_file": ("login.har", har, "application/json")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with db.connect() as conn:
        cred = db.list_site_credentials(conn, sid)[0]
        cookies = credentials.reveal(conn, cred["id"])["storage_state"]["cookies"]
        assert {c["name"] for c in cookies} == {"win"}


def test_create_session_har_without_cookies_rejected(client):
    _login_admin(client)
    sid = _sid()
    har = _har([_har_entry("https://example.com/login")])   # 쿠키 없는 HAR
    r = client.post(
        f"/sites/{sid}/credentials",
        data={"label": "x", "kind": "session"},
        files={"har_file": ("empty.har", har, "application/json")},
        follow_redirects=False,
    )
    assert "error=" in r.headers["location"]
    with db.connect() as conn:
        assert db.count_site_credentials(conn, sid) == 0


def test_create_session_har_drops_third_party_cookies(client):
    """업로드 경로도 대상 사이트(example.com) 도메인 쿠키만 저장한다."""
    _login_admin(client)
    sid = _sid()
    har = _har([
        _har_entry("https://example.com/login", resp_cookies=[
            {"name": "sid", "value": "mine", "domain": "example.com", "path": "/"}]),
        _har_entry("https://tracker.io/p", resp_cookies=[
            {"name": "ga", "value": "theirs", "domain": "tracker.io", "path": "/"}]),
    ])
    r = client.post(
        f"/sites/{sid}/credentials",
        data={"label": "scoped", "kind": "session"},
        files={"har_file": ("login.har", har, "application/json")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with db.connect() as conn:
        cred = db.list_site_credentials(conn, sid)[0]
        cookies = credentials.reveal(conn, cred["id"])["storage_state"]["cookies"]
        assert {c["name"] for c in cookies} == {"sid"}      # tracker.io 제외


def test_create_session_har_oversize_rejected(client, monkeypatch):
    """업로드 단계의 크기 상한(_read_har_upload)도 거부 → 저장 안 함."""
    monkeypatch.setattr(credentials, "MAX_HAR_BYTES", 10)
    _login_admin(client)
    sid = _sid()
    r = client.post(
        f"/sites/{sid}/credentials",
        data={"label": "big", "kind": "session"},
        files={"har_file": ("big.har", b"x" * 50, "application/json")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "error=" in r.headers["location"]
    with db.connect() as conn:
        assert db.count_site_credentials(conn, sid) == 0


def test_archive_stores_session_credential_from_har(client, monkeypatch):
    _stub_archive(monkeypatch)
    _login_admin(client)
    har = _har([_har_entry("https://harsess.example.org/login", resp_cookies=[
        {"name": "s", "value": "1", "domain": "harsess.example.org", "path": "/"}])])
    r = client.post(
        "/archive",
        data={"url": "https://harsess.example.org/", "cred_existing_id": "__new__",
              "cred_kind": "session"},
        files={"cred_har_file": ("login.har", har, "application/json")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    sid = _site_id_for("harsess.example.org")
    with db.connect() as conn:
        cred = db.list_site_credentials(conn, sid)[0]
        assert cred["kind"] == "session" and cred["label"] == "세션"
        revealed = credentials.reveal(conn, cred["id"])
        assert revealed["storage_state"]["cookies"][0]["value"] == "1"
