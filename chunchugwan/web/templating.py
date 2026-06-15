"""공용 Jinja2 템플릿 인스턴스 (app/auth_routes 양쪽에서 사용)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape


def _auth_context(request: Request) -> dict:
    """미들웨어가 적재한 로그인 사용자와 메뉴/버튼 노출 여부를 모든 템플릿에 주입."""
    from . import permissions
    from .. import config

    user = getattr(request.state, "user", None)
    admin = permissions.system_allowed(user)
    # 사람 보조(라이브 챌린지) 기능이 켜진 관리자에게만 '사람 확인' 메뉴를 띄운다.
    # 대기 건수는 작은 인덱스 조회 — 기능 off(기본)면 질의하지 않는다.
    needs_human = 0
    if admin and config.LIVE_CHALLENGE:
        from .. import db

        try:
            with db.connect() as conn:
                needs_human = len(db.list_needs_human_jobs(conn))
        except Exception:
            needs_human = 0
    return {
        "user": user,
        "system_allowed": admin,
        "can_archive": permissions.can_archive(user),
        "can_delete": permissions.can_delete(user),
        "can_view_logs": permissions.can_view_logs(user),
        "can_search": permissions.can_search(user),
        "live_challenge_enabled": config.LIVE_CHALLENGE,
        "needs_human_count": needs_human,
    }


def _tz_context(request: Request) -> dict:
    """로그인 사용자의 타임존(IANA)을 모든 템플릿에 주입. 미로그인 시 UTC."""
    user = getattr(request.state, "user", None)
    try:
        tz = (user["timezone"] if user is not None else None) or "UTC"
    except (IndexError, KeyError):
        tz = "UTC"
    return {"user_timezone": tz}


def _i18n_context(request: Request) -> dict:
    """요청 로케일의 번역 함수(`_`)를 모든 템플릿에 주입."""
    from . import i18n

    locale = getattr(request.state, "locale", i18n.DEFAULT_LOCALE)
    return {
        "_": i18n.gettext_for(locale),
        "locale": locale,
    }


def filesize(num: int | float | None) -> str:
    """바이트 수를 사람이 읽는 단위로 (예: 532 B, 1.4 KB, 2.0 MB)."""
    if num is None:
        return "-"
    size = float(num)
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KB", "MB", "GB"):
        size /= 1024
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} GB"


def ts(value: str | None, fmt: str = "datetime") -> Markup | str:
    """UTC ISO 타임스탬프를 <time class="ts"> 요소로 출력 (fmt: datetime | date).

    텍스트는 UTC 기준으로 렌더링하고, base.html 의 JS 가 사용자 타임존으로 변환한다.
    JS 미동작 환경에서는 UTC 텍스트가 그대로 남는다.
    """
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    text = dt.strftime("%Y-%m-%d" if fmt == "date" else "%Y-%m-%d %H:%M:%S")
    return Markup(
        f'<time class="ts" data-fmt="{fmt}" datetime="{dt.isoformat()}">{text}</time>'
    )


def highlight(text: str | None, terms) -> Markup | str:
    """검색 스니펫에서 매치어를 <mark> 로 강조 (HTML 이스케이프 후 삽입).

    텍스트는 모두 escape 해 주입을 막고, 매치 구간만 <mark> 로 감싼다.
    terms 는 검색 토큰 목록 — 대소문자 무시로 부분일치를 강조한다.
    """
    if not text:
        return ""
    tokens = [t for t in (terms or []) if t]
    if not tokens:
        return escape(text)
    pattern = re.compile("|".join(re.escape(t) for t in tokens), re.IGNORECASE)
    out: list = []
    last = 0
    for m in pattern.finditer(text):
        out.append(escape(text[last:m.start()]))
        out.append(Markup("<mark>") + escape(m.group(0)) + Markup("</mark>"))
        last = m.end()
    out.append(escape(text[last:]))
    return Markup("").join(out)


templates = Jinja2Templates(
    directory=Path(__file__).parent / "templates",
    context_processors=[_auth_context, _i18n_context, _tz_context],
)
templates.env.filters["filesize"] = filesize
templates.env.filters["ts"] = ts
templates.env.filters["highlight"] = highlight
