"""이메일 초대 — DB 계층, 발급/취소(관리자 전용), 수락 가입 플로우, 메일 발송 테스트."""
import re
import smtplib
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, mailer
from chunchugwan.web import app as web_app


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """임시 아카이브 DB 환경 (인증은 기본값 on)."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture
def client(tmp_db):
    """최초 관리자 + 보기 전용 사용자가 등록된 TestClient."""
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(conn, "viewer@test.co", auth.hash_password("password1234"))
    return TestClient(web_app.app)


def _login(client, email: str, password: str = "password1234"):
    return client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )


def _invite(client, email: str, role: str = "viewer"):
    """관리자로 초대를 발급하고 redirect 응답 반환."""
    return client.post(
        "/system/users/invite", data={"email": email, "role": role},
        follow_redirects=False,
    )


def _token_from_location(location: str) -> str:
    """redirect 의 notice/error 메시지에 포함된 초대 링크에서 토큰 추출."""
    m = re.search(r"/invite/([A-Za-z0-9_-]+)", unquote(location))
    assert m, f"초대 링크 없음: {location}"
    return m.group(1)


# ---- DB 계층 ----


def test_create_invite_and_get_by_token(tmp_db):
    with db.connect() as conn:
        h = auth.hash_token("tok")
        iid = db.create_invite(conn, "a@b.co", h, "archiver", None, ttl_seconds=60)
        inv = db.get_invite_by_token(conn, h)
        assert inv["id"] == iid and inv["role"] == "archiver"
        assert db.get_invite_by_token(conn, auth.hash_token("other")) is None


def test_create_invite_replaces_existing(tmp_db):
    """같은 이메일 재초대 — 이전 토큰은 무효, 새 토큰만 유효."""
    with db.connect() as conn:
        old, new = auth.hash_token("old"), auth.hash_token("new")
        db.create_invite(conn, "a@b.co", old, "viewer", None, ttl_seconds=60)
        db.create_invite(conn, "A@B.CO", new, "admin", None, ttl_seconds=60)
        assert db.get_invite_by_token(conn, old) is None
        assert db.get_invite_by_token(conn, new)["role"] == "admin"
        assert len(db.list_invites(conn)) == 1


def test_create_invite_rejects_blocked_role(tmp_db):
    with db.connect() as conn:
        with pytest.raises(ValueError):
            db.create_invite(
                conn, "a@b.co", auth.hash_token("t"), "blocked", None, ttl_seconds=60
            )


def test_expired_invite_not_returned_and_cleaned(tmp_db):
    with db.connect() as conn:
        h = auth.hash_token("t")
        db.create_invite(conn, "a@b.co", h, "viewer", None, ttl_seconds=-1)
        assert db.get_invite_by_token(conn, h) is None
        assert db.list_invites(conn) == []
        db.delete_expired_invites(conn)
        assert conn.execute("SELECT COUNT(*) AS c FROM invites").fetchone()["c"] == 0


# ---- 발급 (관리자 전용) ----


def test_invite_requires_admin(client):
    _login(client, "viewer@test.co")
    assert _invite(client, "new@test.co").status_code == 403


def test_invite_link_shown_when_mail_off(client):
    _login(client, "boss@test.co", "bosspass1234")
    res = _invite(client, "new@test.co", role="archiver")
    assert res.status_code == 303 and "notice=" in res.headers["location"]
    _token_from_location(res.headers["location"])
    with db.connect() as conn:
        invites = db.list_invites(conn)
    assert len(invites) == 1 and invites[0]["role"] == "archiver"
    assert invites[0]["inviter_email"] == "boss@test.co"
    # 화면에도 대기 중 초대가 보인다
    assert "new@test.co" in client.get("/system/users").text


def test_invite_rejects_existing_email_and_bad_input(client):
    _login(client, "boss@test.co", "bosspass1234")
    res = _invite(client, "viewer@test.co")
    assert res.status_code == 303 and "error=" in res.headers["location"]
    res = _invite(client, "not-an-email")
    assert res.status_code == 303 and "error=" in res.headers["location"]
    assert _invite(client, "new@test.co", role="blocked").status_code == 400
    with db.connect() as conn:
        assert db.list_invites(conn) == []


def test_invite_cancel(client):
    _login(client, "boss@test.co", "bosspass1234")
    token = _token_from_location(_invite(client, "new@test.co").headers["location"])
    with db.connect() as conn:
        iid = db.list_invites(conn)[0]["id"]
    res = client.post(f"/system/users/invite/{iid}/delete", follow_redirects=False)
    assert res.status_code == 303
    assert client.post("/system/users/invite/999/delete").status_code == 404
    # 취소된 링크는 무효
    client.cookies.clear()
    assert client.get(f"/invite/{token}").status_code == 404


# ---- 수락 (공개 경로) ----


def test_invite_accept_flow(client):
    _login(client, "boss@test.co", "bosspass1234")
    token = _token_from_location(
        _invite(client, "new@test.co", role="archiver").headers["location"]
    )
    client.cookies.clear()

    page = client.get(f"/invite/{token}")
    assert page.status_code == 200 and "new@test.co" in page.text

    res = client.post(
        f"/invite/{token}", data={"password": "newpass1234"}, follow_redirects=False
    )
    assert res.status_code == 303
    with db.connect() as conn:
        user = db.get_user_by_email(conn, "new@test.co")
        assert user["role"] == "archiver" and user["is_founder"] == 0
        assert db.list_invites(conn) == []  # 1회용 — 가입과 함께 삭제
    # 즉시 로그인된 세션으로 접근 가능
    assert client.get("/").status_code == 200
    # 같은 토큰 재사용 불가
    client.cookies.clear()
    assert client.get(f"/invite/{token}").status_code == 404


def test_invite_page_invalid_token(client):
    assert client.get("/invite/no-such-token").status_code == 404
    assert client.post(
        "/invite/no-such-token", data={"password": "newpass1234"}
    ).status_code == 404


def test_invite_accept_rejects_short_password(client):
    _login(client, "boss@test.co", "bosspass1234")
    token = _token_from_location(_invite(client, "new@test.co").headers["location"])
    client.cookies.clear()
    res = client.post(f"/invite/{token}", data={"password": "short"})
    assert res.status_code == 400
    with db.connect() as conn:
        assert db.get_user_by_email(conn, "new@test.co") is None
        assert len(db.list_invites(conn)) == 1  # 초대는 유효하게 남는다


def test_invite_accept_conflicts_with_signed_up_email(client):
    """초대 후 같은 이메일이 일반 가입한 경우 — 초대 수락은 거부, 초대 삭제."""
    _login(client, "boss@test.co", "bosspass1234")
    token = _token_from_location(_invite(client, "new@test.co").headers["location"])
    client.cookies.clear()
    client.post("/signup", data={"email": "new@test.co", "password": "password1234"})
    client.cookies.clear()
    res = client.post(f"/invite/{token}", data={"password": "newpass1234"})
    assert res.status_code == 400 and "이미 가입된 이메일" in res.text
    with db.connect() as conn:
        assert db.list_invites(conn) == []
        # 일반 가입의 초기 권한(기본 pending) 그대로 — 초대 권한이 덮어쓰지 않는다
        assert db.get_user_by_email(conn, "new@test.co")["role"] == "pending"


def test_reinvite_invalidates_old_link(client):
    _login(client, "boss@test.co", "bosspass1234")
    old = _token_from_location(_invite(client, "new@test.co").headers["location"])
    new = _token_from_location(
        _invite(client, "new@test.co", role="admin").headers["location"]
    )
    client.cookies.clear()
    assert client.get(f"/invite/{old}").status_code == 404
    assert client.get(f"/invite/{new}").status_code == 200


# ---- 메일 발송 ----


def test_invite_sends_mail_when_configured(client, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.test")
    sent = {}

    def fake_send(to_email, invite_url, inviter_email, role_label):
        sent.update(to=to_email, url=invite_url, inviter=inviter_email, role=role_label)

    monkeypatch.setattr(mailer, "send_invite", fake_send)
    _login(client, "boss@test.co", "bosspass1234")
    res = _invite(client, "new@test.co", role="archiver")
    assert res.status_code == 303
    assert "메일을 보냈습니다" in unquote(res.headers["location"])
    assert sent["to"] == "new@test.co" and sent["inviter"] == "boss@test.co"
    assert sent["role"] == "아카이브" and "/invite/" in sent["url"]


def test_invite_mail_failure_falls_back_to_link(client, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.test")

    def fail_send(*args, **kwargs):
        raise smtplib.SMTPException("connection refused")

    monkeypatch.setattr(mailer, "send_invite", fail_send)
    _login(client, "boss@test.co", "bosspass1234")
    res = _invite(client, "new@test.co")
    assert res.status_code == 303 and "error=" in res.headers["location"]
    # 발송은 실패해도 초대는 만들어졌고 링크가 안내된다
    token = _token_from_location(res.headers["location"])
    client.cookies.clear()
    assert client.get(f"/invite/{token}").status_code == 200
