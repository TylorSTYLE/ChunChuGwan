"""사이트 로그인 자격증명 — crypto·db·코어(credentials)·관리 화면 테스트."""
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


# ---- 코어 모듈 (credentials) ----


def test_build_payload_http_basic(tmp_db):
    p = credentials.build_payload("http_basic", {"username": " u ", "password": "pw"})
    assert p == {"username": "u", "password": "pw"}    # 사용자명은 trim
    for bad in ({"username": "", "password": "p"}, {"username": "u", "password": ""}):
        with pytest.raises(credentials.CredentialError):
            credentials.build_payload("http_basic", bad)


def test_build_payload_session(tmp_db):
    state = '{"cookies": [{"name": "s", "value": "1"}], "origins": []}'
    p = credentials.build_payload("session", {"storage_state": state})
    assert p["storage_state"]["cookies"][0]["name"] == "s"
    for bad in ("", "not json", '{"no": "cookies"}', "[]"):
        with pytest.raises(credentials.CredentialError):
            credentials.build_payload("session", {"storage_state": bad})


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
    state = '{"cookies": [{"name": "sid", "value": "abc"}], "origins": []}'
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
