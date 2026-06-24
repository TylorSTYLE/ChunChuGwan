"""입력 명령 재생 — 사람(라이브)·AI 챌린지 해결이 공유하는 공용 실행기.

좌표는 뷰포트로 클램프하고 patchright/Playwright 의 page.mouse/page.keyboard 로
재생한다. CDP Input 경로라 isTrusted=true 이벤트가 된다. 명령(cmd)은 sqlite Row
(live_challenge) 또는 평범한 dict(ai_challenge 가 정규화한 primitive) 둘 다 받는다
— 둘 다 `kind/x/y/key/delay_ms` 키를 첨자(subscript)로 읽을 수 있다.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def replay(page, cmd, vw: int, vh: int) -> None:
    """입력 명령 하나를 재생 (좌표는 뷰포트로 클램프, CDP Input → isTrusted).

    지원 kind: click / move / down / up / key / text. delay_ms 가 있으면 해당
    명령 실행 전 그만큼 대기한다(비정상적으로 큰 간격은 3000ms 로 상한).
    """
    delay = cmd["delay_ms"] or 0
    if delay:
        page.wait_for_timeout(min(delay, 3000))  # 비정상적으로 큰 간격은 상한
    x = max(0, min(cmd["x"] if cmd["x"] is not None else 0, vw - 1))
    y = max(0, min(cmd["y"] if cmd["y"] is not None else 0, vh - 1))
    kind = cmd["kind"]
    try:
        if kind == "click":
            page.mouse.click(x, y)
        elif kind == "move":
            page.mouse.move(x, y)
        elif kind == "down":
            page.mouse.move(x, y)
            page.mouse.down()
        elif kind == "up":
            page.mouse.move(x, y)
            page.mouse.up()
        elif kind == "key":
            page.keyboard.press(cmd["key"] or "")
        elif kind == "text":
            page.keyboard.type(cmd["key"] or "")
    except Exception as e:
        logger.warning("입력 재생 실패(%s): %s", kind, e)
