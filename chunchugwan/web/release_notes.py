"""업데이트(릴리스) 안내 노트 — 로그인 후 1회 표시용 번들 콘텐츠.

표시 내용은 **GitHub Release 기준**이다. 릴리스 시 CI(`release.yml`)가 그 버전의
릴리즈 노트(generate-notes)를 받아 `parse_github_notes` 로 변환(수정자 `@user`·원본
링크 제거, PR 번호/URL 만 유지)한 뒤 `scripts/gen_release_notes.py` 로 옆의
``release_notes.json`` 에 써넣어 이미지에 동봉한다. 대시보드는 런타임에 외부를
호출하지 않고 이 JSON 만 읽는다(오프라인·자가호스팅 원칙 유지).

키는 앱 버전(``pyproject.version`` == ``__version__``), 값은 ``{"items": [...]}``.
각 item 은 ``{"text": 변경 요약, "pr": PR번호|None, "url": PR URL|None}``.
표시 여부·"한 번 봤음" 추적은 프론트(localStorage)와 ``web_api_routes`` 의 ``/me`` 가
담당한다 — 이 모듈은 콘텐츠 보유와 GitHub 노트 파싱만 맡는다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

JSON_PATH = Path(__file__).with_name("release_notes.json")

# GitHub generate-notes 의 한 줄: "* <제목> by @<작성자> in <…/pull/N>".
# 제목 뒤 "by @user in URL" 은 선택(수동 추가 항목은 없을 수 있음)이며, 있으면
# 작성자·URL·PR 번호를 따로 잡는다. "## …" 헤더·"**Full Changelog**"·머리말은
# "* " 로 시작하지 않거나 이 패턴에 안 맞아 자연히 걸러진다.
_BULLET = re.compile(
    r"^\*\s+(?P<title>.+?)"
    r"(?:\s+by\s+@(?P<author>[A-Za-z0-9\-\[\]]+)"
    r"\s+in\s+(?P<url>https?://\S+?/pull/(?P<num>\d+)))?"
    r"\s*$"
)


def parse_github_notes(body: str) -> list[dict]:
    """GitHub Release 본문(markdown)에서 표시용 항목 목록을 추출한다.

    "What's Changed" 의 ``* …`` 글머리만 취하고 수정자(`@user`)·원본 링크는 버리되
    PR 번호와 PR URL 은 남긴다(프론트가 ``#번호`` 링크로 렌더). 봇(`@…[bot]`)이 올린
    항목(릴리스 PR 등)과 헤더·Full Changelog·머리말 줄은 제외한다.
    """
    items: list[dict] = []
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.startswith("*"):
            continue
        m = _BULLET.match(line)
        if m is None:
            continue
        author = m.group("author")
        if author and author.endswith("[bot]"):
            continue  # 릴리스 PR 등 봇 항목 제외
        num = m.group("num")
        items.append(
            {
                "text": m.group("title").strip(),
                "pr": int(num) if num else None,
                "url": m.group("url") if num else None,
            }
        )
    return items


def _load() -> dict:
    """번들 JSON 로드 — 없거나 깨졌으면 빈 dict(노트 미표시)."""
    try:
        return json.loads(JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def note_for(version: str) -> dict | None:
    """해당 버전의 노트(version·items) — 없거나 항목이 비면 None.

    version 정규화: ``0.6.1+local.1`` 같은 PEP 440 로컬/빌드 메타데이터는 ``+`` 앞
    base 버전으로도 매칭한다. 반환 ``version`` 은 base 라 프론트가 이 값으로
    localStorage "봤음" 키를 안정적으로 잡는다. UI 제목은 라우트(`/me`)가 로케일에
    맞춰 덧붙인다(이 모듈은 언어 중립 — 항목 본문은 PR 제목 원문).
    """
    base = version.split("+", 1)[0]
    data = _load()
    entry = data.get(version) or data.get(base)
    if not entry:
        return None
    items = entry.get("items") or []
    if not items:
        return None
    return {"version": base, "items": [dict(i) for i in items]}
