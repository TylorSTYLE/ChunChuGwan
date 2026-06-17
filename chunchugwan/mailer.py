"""초대 메일 발송 — 표준 smtplib 사용.

SMTP 설정은 시스템 메뉴(`/system/smtp-settings`, DB `settings` 테이블)에서
등록·변경하거나 `WCCG_SMTP_*` 환경변수로 둘 수 있다. 둘 다 있으면 DB 값이
우선하고, 없는 항목만 환경변수로 폴백한다 (`resolve_config`). 로그인 비밀번호는
외부 SMTP 서버에 replay 해야 하므로 대칭 암호화(crypto)한 암호문으로 저장한다
(CLAUDE.md 원칙 6 예외 — docs/AUTHENTICATION.md 참조).
"""

from __future__ import annotations

import logging
import smtplib
import sqlite3
from dataclasses import dataclass
from email.message import EmailMessage

from . import config, crypto, db

logger = logging.getLogger(__name__)

# 허용하는 TLS 모드 (폼·검증·resolve 가 공유).
SMTP_TLS_MODES = ("starttls", "ssl", "off")

_INVITE_BODY = """\
{inviter} 님이 춘추관(개인 웹 아카이브)에 초대했습니다.

부여될 권한: {role_label}

아래 링크에서 패스워드를 설정하면 가입이 완료됩니다.
링크는 {ttl_days}일 후 만료됩니다.

{invite_url}

본인이 요청하지 않았다면 이 메일을 무시하세요.
"""

_TEST_BODY = """\
춘추관(개인 웹 아카이브) SMTP 설정 테스트 메일입니다.

이 메일을 받았다면 메일 발송 설정이 올바릅니다.
"""

_VERIFY_BODY = """\
춘추관(개인 웹 아카이브) 이메일 본인 인증 코드입니다.

인증 코드: {code}

이 코드는 {ttl_minutes}분 후 만료됩니다.
대시보드의 인증 화면에 코드를 입력하면 인증이 완료됩니다.

본인이 요청하지 않았다면 이 메일을 무시하세요.
"""


@dataclass(frozen=True)
class SmtpConfig:
    """발송에 필요한 SMTP 설정 한 벌 (DB·환경변수에서 해석된 결과)."""

    host: str
    port: int
    user: str
    password: str
    sender: str  # From 헤더 주소 (비면 user 로 폴백한 값)
    tls: str  # SMTP_TLS_MODES 중 하나

    @property
    def enabled(self) -> bool:
        """발송 가능 여부 — 호스트가 있어야 메일을 보낸다."""
        return bool(self.host)


def _setting_or(conn: sqlite3.Connection, key: str, fallback: str) -> str:
    """DB 설정 값을 반환하되, 저장된 적 없으면(None) 환경변수 폴백.

    빈 문자열("")은 관리자가 폼에서 비워 명시적으로 지운 값이므로 그대로
    존중한다 (환경변수로 되돌아가지 않는다).
    """
    val = db.get_setting(conn, key)
    return fallback if val is None else val


def _resolve_password(conn: sqlite3.Connection) -> str:
    """SMTP 비밀번호 해석 — DB 암호문 복호화, 없으면 환경변수 폴백.

    키 미설정·복호화 실패는 메일 발송 자체를 막지 않도록 경고만 남기고
    환경변수 값으로 폴백한다 (graceful — 원칙 6 의 자격증명 처리와 동일).
    """
    enc = db.get_setting(conn, db.SMTP_PASSWORD_KEY)
    if enc is None:
        return config.SMTP_PASSWORD  # UI 로 저장한 적 없음 — 환경변수 폴백
    if enc == "":
        return ""  # 명시적으로 지움
    try:
        return crypto.decrypt(enc)
    except (crypto.SecretKeyMissing, crypto.SecretDecryptError) as e:
        logger.warning("SMTP 비밀번호 복호화 실패 — 환경변수로 폴백: %s", e)
        return config.SMTP_PASSWORD


def resolve_config(conn: sqlite3.Connection) -> SmtpConfig:
    """현재 유효한 SMTP 설정을 해석 — DB(시스템 메뉴) 우선, 환경변수 폴백."""
    host = _setting_or(conn, db.SMTP_HOST_KEY, config.SMTP_HOST)
    try:
        port = int(_setting_or(conn, db.SMTP_PORT_KEY, str(config.SMTP_PORT)))
    except (TypeError, ValueError):
        port = config.SMTP_PORT
    user = _setting_or(conn, db.SMTP_USER_KEY, config.SMTP_USER)
    tls = _setting_or(conn, db.SMTP_TLS_KEY, config.SMTP_TLS)
    if tls not in SMTP_TLS_MODES:
        tls = "starttls"
    sender = _setting_or(conn, db.SMTP_FROM_KEY, config.SMTP_FROM) or user
    return SmtpConfig(
        host=host, port=port, user=user,
        password=_resolve_password(conn), sender=sender, tls=tls,
    )


def mail_enabled(conn: sqlite3.Connection) -> bool:
    """메일 발송 설정이 채워졌는지 (호스트 존재 여부)."""
    return resolve_config(conn).enabled


def _connect(cfg: SmtpConfig) -> smtplib.SMTP:
    """설정된 TLS 모드로 SMTP 연결 (타임아웃 필수)."""
    if cfg.tls == "ssl":
        return smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=config.SMTP_TIMEOUT_SECONDS)
    smtp = smtplib.SMTP(cfg.host, cfg.port, timeout=config.SMTP_TIMEOUT_SECONDS)
    if cfg.tls != "off":
        smtp.starttls()
    return smtp


def _send(cfg: SmtpConfig, msg: EmailMessage) -> None:
    """메시지 발송 — From 주입 + (사용자 있으면) 로그인."""
    msg["From"] = cfg.sender
    with _connect(cfg) as smtp:
        if cfg.user:
            smtp.login(cfg.user, cfg.password)
        smtp.send_message(msg)


def send_invite(
    cfg: SmtpConfig,
    to_email: str,
    invite_url: str,
    inviter_email: str,
    role_label: str,
) -> None:
    """초대 메일 발송. 실패 시 smtplib.SMTPException/OSError 가 그대로 전파된다."""
    msg = EmailMessage()
    msg["Subject"] = "춘추관 초대"
    msg["To"] = to_email
    msg.set_content(
        _INVITE_BODY.format(
            inviter=inviter_email,
            role_label=role_label,
            ttl_days=config.INVITE_TTL_DAYS,
            invite_url=invite_url,
        )
    )
    _send(cfg, msg)


def send_test(cfg: SmtpConfig, to_email: str) -> None:
    """SMTP 설정 확인용 테스트 메일 발송 (실패 시 예외 전파)."""
    msg = EmailMessage()
    msg["Subject"] = "춘추관 SMTP 테스트"
    msg["To"] = to_email
    msg.set_content(_TEST_BODY)
    _send(cfg, msg)


def send_verification_code(
    cfg: SmtpConfig, to_email: str, code: str, ttl_minutes: int
) -> None:
    """이메일 본인 인증 코드 발송. 실패 시 smtplib.SMTPException/OSError 전파."""
    msg = EmailMessage()
    msg["Subject"] = "춘추관 이메일 인증 코드"
    msg["To"] = to_email
    msg.set_content(_VERIFY_BODY.format(code=code, ttl_minutes=ttl_minutes))
    _send(cfg, msg)
