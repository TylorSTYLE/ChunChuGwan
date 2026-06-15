"""사람 보조 챌린지 해결 — 라이브 세션 (최후 수단).

A(자동 통과 대기)로도 안 풀린 인터랙티브 챌린지(클릭/입력이 필요한 Turnstile,
그림 찾기, 문자 입력 등)를 사람이 대시보드에서 직접 조작해 통과시킨다.

worker 의 캡처 스레드가 살아있는 patchright page 를 붙든 채(= 그 작업의 큐
진행을 멈춘 채) 이 세션을 돌린다. dashboard 와는 HTTP 채널 없이 SQLite +
`./archive` 볼륨으로만 조율한다:
- 화면(worker→대시보드): page 스크린샷을 cache/live/{token}.jpg 에 주기적으로
  원자 교체(덮어쓰기). 대시보드가 토큰으로 그 파일을 폴링해 보여준다.
- 입력(대시보드→worker): 사람의 클릭/키를 live_commands 테이블에 INSERT 하면
  worker 가 seq 순으로 꺼내 page.mouse/page.keyboard 로 재생한다(타이밍·드래그
  재현, CDP Input 이라 isTrusted=true).

challenge_reason 이 사라지면(통과) 살아있는 page 로 이어서 정상 캡처한다. 하드
타임아웃·사람 취소·사설/루프백 이동이면 실패로 떨어진다(기존 챌린지 실패 경로).

주의: 봇 차단의 진짜 요인(데이터센터 IP 평판·Xvfb 핑거프린트)은 사람 클릭으로
못 고치므로 통과가 보장되지 않는다. 봇월이 아닌 인터랙티브 페이지(로그인·동의
클릭 등)엔 확실히 듣는 최후 수단이다.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import capture, config, db, netcheck

logger = logging.getLogger(__name__)


def shot_path(token: str) -> Path:
    """라이브 스크린샷 파일 경로 (worker 가 쓰고 대시보드가 읽는다 — 같은 볼륨)."""
    return config.CACHE_DIR / "live" / f"{token}.jpg"


class LiveChallengeSession:
    """한 작업(job)의 라이브 챌린지 해결 세션. capture 가 page 를 넘겨 solve 한다."""

    def __init__(self, job_id: int, *, network_tag_id: str | None = None) -> None:
        self.job_id = job_id
        self.network_tag_id = network_tag_id
        self.token = secrets.token_urlsafe(16)

    def solve(self, page, reason: str) -> tuple[str, str | None]:
        """챌린지가 풀릴 때까지 사람 입력을 중계한다. 풀리면 (갱신된 raw_html,
        None), 타임아웃/취소/사설이동이면 (raw_html, reason) 반환."""
        vw, vh = config.LIVE_VIEWPORT_W, config.LIVE_VIEWPORT_H
        try:
            page.set_viewport_size({"width": vw, "height": vh})
        except Exception as e:
            logger.warning("라이브 뷰포트 설정 실패(계속): %s", e)
        path = shot_path(self.token)
        path.parent.mkdir(parents=True, exist_ok=True)
        with db.connect() as conn:
            db.mark_needs_human(conn, self.job_id, token=self.token,
                                viewport_w=vw, viewport_h=vh)
        logger.warning(
            "사람 확인 필요 — 라이브 세션 진입: job %d · %s (대시보드에서 처리)",
            self.job_id, page.url,
        )
        deadline = time.monotonic() + config.LIVE_CHALLENGE_TIMEOUT_SECONDS
        last_shot = 0.0
        raw_html = page.content()
        try:
            while time.monotonic() < deadline:
                # 1) SSRF 가드 — 사람 클릭으로 루프백/미태그 사설로 이동하면 중단
                host = urlsplit(page.url).hostname or ""
                kind = netcheck.classify_host(host)
                if kind == netcheck.LOOPBACK or (
                    kind == netcheck.PRIVATE and not self.network_tag_id
                ):
                    logger.warning("라이브 세션이 사설/루프백으로 이동 — 중단: %s", page.url)
                    return page.content(), reason
                # 2) 취소·작업 소멸 확인
                with db.connect() as conn:
                    job = db.get_archive_job(conn, self.job_id)
                if job is None or job["live_cancel"]:
                    logger.info("라이브 세션 취소: job %d", self.job_id)
                    return page.content(), reason
                # 2-1) 사람이 '확인 완료' → 챌린지 판정과 무관하게 현재 페이지로 강제 진행.
                #      잔여 위젯/마커로 자동 판정(challenge_reason)이 안 풀려도 사람이
                #      실제로 통과시켰다고 보고 캡처를 이어간다.
                if job["live_force_solve"]:
                    logger.info("사람 확인 완료(강제 진행): job %d", self.job_id)
                    self._write_shot(page, path)
                    return page.content(), None
                # 3) 화면 갱신 (간격마다)
                now = time.monotonic()
                if now - last_shot >= config.LIVE_SHOT_INTERVAL_MS / 1000:
                    self._write_shot(page, path)
                    last_shot = now
                # 4) 입력 명령 재생 (클릭·키·드래그, 순서·타이밍 유지)
                with db.connect() as conn:
                    cmds = db.claim_live_commands(conn, self.token)
                for cmd in cmds:
                    self._replay(page, cmd, vw, vh)
                # 5) 통과 판정
                raw_html = page.content()
                if capture.challenge_reason(raw_html, None, page.url, page.title()) is None:
                    logger.info("라이브 세션 통과 — 캡처 진행: job %d", self.job_id)
                    self._write_shot(page, path)
                    return raw_html, None
                page.wait_for_timeout(config.LIVE_POLL_INTERVAL_MS)
            logger.warning("라이브 세션 시간 초과 — 차단으로 처리: job %d", self.job_id)
            return page.content(), reason
        finally:
            with db.connect() as conn:
                db.clear_needs_human(conn, self.job_id)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_shot(self, page, path: Path) -> None:
        """뷰포트 스크린샷을 원자 교체로 쓴다 (대시보드가 폴링)."""
        try:
            data = page.screenshot(type="jpeg", quality=70, full_page=False)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning("라이브 스크린샷 실패(계속): %s", e)

    def _replay(self, page, cmd, vw: int, vh: int) -> None:
        """입력 명령 하나를 재생 (좌표는 뷰포트로 클램프, CDP Input → isTrusted)."""
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
            logger.warning("라이브 입력 재생 실패(%s): %s", kind, e)
