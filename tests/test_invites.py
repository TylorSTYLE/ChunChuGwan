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


# ---- 수락 (공개 경로) ----


# ---- 메일 발송 ----


