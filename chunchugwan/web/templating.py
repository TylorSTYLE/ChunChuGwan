"""공용 Jinja2 템플릿 인스턴스 (app/auth_routes 양쪽에서 사용)."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates


def _auth_context(request: Request) -> dict:
    """미들웨어가 적재한 로그인 사용자와 메뉴/버튼 노출 여부를 모든 템플릿에 주입."""
    from . import permissions

    user = getattr(request.state, "user", None)
    return {
        "user": user,
        "system_allowed": permissions.system_allowed(user),
        "can_archive": permissions.can_archive(user),
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


templates = Jinja2Templates(
    directory=Path(__file__).parent / "templates",
    context_processors=[_auth_context],
)
templates.env.filters["filesize"] = filesize
