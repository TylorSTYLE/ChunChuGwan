"""공용 Jinja2 템플릿 인스턴스 (app/auth_routes 양쪽에서 사용)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup


def _auth_context(request: Request) -> dict:
    """미들웨어가 적재한 로그인 사용자와 메뉴/버튼 노출 여부를 모든 템플릿에 주입."""
    from . import permissions

    user = getattr(request.state, "user", None)
    return {
        "user": user,
        "system_allowed": permissions.system_allowed(user),
        "can_archive": permissions.can_archive(user),
        "can_delete": permissions.can_delete(user),
        "can_view_logs": permissions.can_view_logs(user),
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


templates = Jinja2Templates(
    directory=Path(__file__).parent / "templates",
    context_processors=[_auth_context, _i18n_context, _tz_context],
)
templates.env.filters["filesize"] = filesize
templates.env.filters["ts"] = ts
