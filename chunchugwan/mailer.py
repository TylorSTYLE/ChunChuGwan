"""초대 메일 발송 — 표준 smtplib 사용. WCCG_SMTP_* 환경변수로 설정 (docs/AUTHENTICATION.md 참조)."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from . import config

_INVITE_BODY = """\
{inviter} 님이 춘추관(개인 웹 아카이브)에 초대했습니다.

부여될 권한: {role_label}

아래 링크에서 패스워드를 설정하면 가입이 완료됩니다.
링크는 {ttl_days}일 후 만료됩니다.

{invite_url}

본인이 요청하지 않았다면 이 메일을 무시하세요.
"""


def _connect() -> smtplib.SMTP:
    """설정된 TLS 모드로 SMTP 연결 (타임아웃 필수)."""
    if config.SMTP_TLS == "ssl":
        return smtplib.SMTP_SSL(
            config.SMTP_HOST, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT_SECONDS
        )
    smtp = smtplib.SMTP(
        config.SMTP_HOST, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT_SECONDS
    )
    if config.SMTP_TLS != "off":
        smtp.starttls()
    return smtp


def send_invite(
    to_email: str, invite_url: str, inviter_email: str, role_label: str
) -> None:
    """초대 메일 발송. 실패 시 smtplib.SMTPException/OSError 가 그대로 전파된다."""
    msg = EmailMessage()
    msg["Subject"] = "춘추관 초대"
    msg["From"] = config.SMTP_FROM
    msg["To"] = to_email
    msg.set_content(
        _INVITE_BODY.format(
            inviter=inviter_email,
            role_label=role_label,
            ttl_days=config.INVITE_TTL_DAYS,
            invite_url=invite_url,
        )
    )
    with _connect() as smtp:
        if config.SMTP_USER:
            smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.send_message(msg)
