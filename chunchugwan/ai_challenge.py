"""AI 자동 챌린지 해결 — 비전 LLM 세션 (B 단계).

자동 통과 대기(A)로도 안 풀린 '양성 인터스티셜'(동의 / 연령 확인 / "계속하려면
클릭" 같은 사람 확인 게이트)을, 비전 분석 가능한 OpenAI 호환 LLM 으로 스크린샷을
판독해 마우스/키보드 입력을 대신 수행함으로써 통과시킨다. 못 풀면 사람 개입
(C, live_challenge)으로 캐스케이드한다 — 이 세션이 reason 을 그대로 돌려주면
capture 가 다음 단계(live_session)로 넘긴다.

live_challenge.LiveChallengeSession 과 대칭 구조다(같은 반환 계약: 통과 시
(raw_html, None), 실패/소진 시 (raw_html, reason)). 입력 재생은 공용
input_replay.replay 를 재사용하고, LLM 출력은 신뢰 불가 입력으로 취급해
_normalize 가드(타입 화이트리스트·좌표 클램프·액션 수 상한·키명 화이트리스트)를
통과한 primitive 만 재생한다.

라운드 루프는 2단계다: ① 스크린샷 → 액션 계획(LLM) → 입력 재생, ② 대기 →
스크린샷 → 통과 판정(LLM). 라운드 간 컨텍스트는 이미지를 누적하지 않고 매 호출
새 스크린샷 1장 + 직전 시도 텍스트 요약만 전달한다.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from . import capture, config, db, input_replay, netcheck

logger = logging.getLogger(__name__)

# 허용 액션 타입 (그 외는 거부). type·key 는 키보드 입력, click·drag 는 마우스.
_ALLOWED_ACTION_TYPES = {"click", "type", "key", "drag"}

# 안전한 특수 키명 화이트리스트 — 소문자 키 → Playwright 정규 키명. 단일
# 영숫자(a, A, 1 등)는 그대로 통과시킨다. 그 외 키 입력은 폭발 반경을 줄이려고
# 거부한다(조합키·시스템 단축키 등으로 의도치 않은 동작을 막는다).
_KEY_CANON = {
    "enter": "Enter", "tab": "Tab", "escape": "Escape", "esc": "Escape",
    "backspace": "Backspace", "delete": "Delete", "space": "Space",
    "arrowup": "ArrowUp", "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
    "home": "Home", "end": "End", "pageup": "PageUp", "pagedown": "PageDown",
}

# type 액션의 입력 텍스트 상한 (폭발 반경 제한).
_MAX_TYPE_LEN = 500


def _safe_key(value: object) -> str | None:
    """LLM 이 준 키명을 안전한 Playwright 키명으로 정규화 (불허면 None)."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if len(v) == 1 and v.isalnum():
        return v
    return _KEY_CANON.get(v.lower())


def _clamp_int(value: object, lo: int, hi: int, default: int = 0) -> int:
    """임의 값을 정수화 + [lo, hi] 클램프 (변환 실패면 default)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _parse_json_object(text: str | None) -> dict | None:
    """LLM 응답 텍스트에서 JSON 객체 하나를 뽑아 파싱 (실패면 None).

    마크다운 펜스(```json ... ```)를 떼고 첫 '{' ~ 마지막 '}' 구간만 취해
    json.loads 한다 — 모델이 설명 문장이나 펜스를 덧붙여도 견딘다.
    """
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        # 첫 줄(``` 또는 ```json) 제거 + 마지막 펜스 제거
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(s[start:end + 1])
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _normalize(actions: object, vw: int, vh: int, max_actions: int) -> list[dict]:
    """LLM 액션 목록을 재생 가능한 primitive cmd(dict) 목록으로 검증·정규화.

    화이트리스트 타입만 통과시키고, 좌표는 정수화 후 [0, vw-1]/[0, vh-1] 로
    클램프, 키는 안전 키명만 허용, 라운드당 액션 수는 max_actions 로 절단한다.
    primitive cmd 는 input_replay.replay 가 읽는 {kind,x,y,key,delay_ms} 형태다.
    """
    if not isinstance(actions, list):
        return []
    cmds: list[dict] = []
    for action in actions[:max_actions]:
        if not isinstance(action, dict):
            continue
        atype = action.get("type")
        if atype not in _ALLOWED_ACTION_TYPES:
            continue  # 비화이트리스트 타입 거부
        delay = _clamp_int(action.get("delay_ms"), 0, 3000, 0)
        if atype == "click":
            cmds.append({
                "kind": "click",
                "x": _clamp_int(action.get("x"), 0, vw - 1),
                "y": _clamp_int(action.get("y"), 0, vh - 1),
                "key": None, "delay_ms": delay,
            })
        elif atype == "type":
            text = action.get("text")
            if not isinstance(text, str) or not text:
                continue
            cmds.append({
                "kind": "text", "x": None, "y": None,
                "key": text[:_MAX_TYPE_LEN], "delay_ms": delay,
            })
        elif atype == "key":
            key = _safe_key(action.get("key"))
            if key is None:
                continue
            cmds.append({
                "kind": "key", "x": None, "y": None,
                "key": key, "delay_ms": delay,
            })
        elif atype == "drag":
            frm = action.get("from") or {}
            to = action.get("to") or {}
            if not isinstance(frm, dict) or not isinstance(to, dict):
                continue
            cmds.append({
                "kind": "down",
                "x": _clamp_int(frm.get("x"), 0, vw - 1),
                "y": _clamp_int(frm.get("y"), 0, vh - 1),
                "key": None, "delay_ms": delay,
            })
            cmds.append({
                "kind": "up",
                "x": _clamp_int(to.get("x"), 0, vw - 1),
                "y": _clamp_int(to.get("y"), 0, vh - 1),
                "key": None, "delay_ms": 0,
            })
    return cmds


def _render(template: str, **ctx: object) -> str:
    """프롬프트 템플릿의 {token} 을 컨텍스트 값으로 치환.

    템플릿엔 출력 예시 JSON 의 중괄호가 섞여 있어 str.format 은 못 쓴다 —
    알려진 토큰만 순차 .replace 한다(미지의 {...} 는 그대로 둔다)."""
    out = template
    for key, value in ctx.items():
        out = out.replace("{" + key + "}", str(value))
    return out


class AIChallengeSession:
    """한 작업(job)의 AI 챌린지 해결 세션. capture 가 page 를 넘겨 solve 한다."""

    def __init__(self, job_id: int, *, network_tag_id: str | None = None) -> None:
        self.job_id = job_id
        self.network_tag_id = network_tag_id

    def solve(self, page, reason: str) -> tuple[str, str | None]:
        """양성 게이트를 LLM 으로 통과시킨다. 통과 시 (갱신된 raw_html, None),
        소진/실패/사설이동이면 (raw_html, reason) 반환 → capture 가 C 로 넘긴다."""
        with db.connect() as conn:
            cfg = db.ai_challenge_settings(conn)
        vw, vh = config.LIVE_VIEWPORT_W, config.LIVE_VIEWPORT_H
        try:
            page.set_viewport_size({"width": vw, "height": vh})
        except Exception as e:
            logger.warning("AI 챌린지 뷰포트 설정 실패(계속): %s", e)
        logger.info(
            "AI 자동 챌린지 해결 진입: job %d · %s (최대 %d 라운드)",
            self.job_id, page.url, cfg["max_rounds"],
        )
        last_attempt = "(없음)"
        for round_index in range(1, cfg["max_rounds"] + 1):
            # 1) SSRF 가드 — 클릭으로 루프백/미태그 사설로 이동했으면 중단
            host = urlsplit(page.url).hostname or ""
            kind = netcheck.classify_host(host)
            if kind == netcheck.LOOPBACK or (
                kind == netcheck.PRIVATE and not self.network_tag_id
            ):
                logger.warning("AI 챌린지가 사설/루프백으로 이동 — 중단: %s", page.url)
                return page.content(), reason

            ctx = self._context(page, round_index, cfg["max_rounds"], vw, vh)

            # 2) 입력 단계 — 스크린샷 판독 → 액션 계획
            shot = self._screenshot(page)
            if shot is None:
                return page.content(), reason
            action_prompt = _render(cfg["action_prompt"], last_attempt=last_attempt, **ctx)
            plan = _parse_json_object(self._call_llm(cfg, action_prompt, ctx, shot))
            if plan is None:
                logger.info("AI 챌린지 라운드 %d: 액션 계획 파싱 실패 — 다음 라운드", round_index)
                last_attempt = "(직전 라운드 응답을 해석하지 못함)"
                continue
            if plan.get("giveup"):
                logger.info("AI 챌린지 포기(giveup): job %d · %s", self.job_id,
                            plan.get("reason") or "")
                return page.content(), reason
            cmds = _normalize(plan.get("actions"), vw, vh, cfg["max_actions"])
            for cmd in cmds:
                input_replay.replay(page, cmd, vw, vh)

            # 3) 판정 단계 — 잠시 대기 후 스크린샷 → 통과 판정
            try:
                page.wait_for_timeout(cfg["verdict_delay_ms"])
            except Exception:
                return page.content(), reason
            ctx = self._context(page, round_index, cfg["max_rounds"], vw, vh)
            actions_taken = _summarize_actions(cmds)
            shot2 = self._screenshot(page)
            if shot2 is None:
                return page.content(), reason
            verdict_prompt = _render(
                cfg["verdict_prompt"], last_attempt=last_attempt,
                actions_taken=actions_taken, **ctx,
            )
            v = _parse_json_object(self._call_llm(cfg, verdict_prompt, ctx, shot2)) or {}
            verdict = v.get("verdict")
            if verdict not in ("success", "continue", "fail"):
                verdict = "continue"  # 화이트리스트 밖·파싱 실패 → 보수적으로 계속
            analysis = str(v.get("analysis") or "")

            if verdict == "success":
                if cfg["success_recheck"] and capture.challenge_reason(
                    page.content(), None, page.url, page.title()
                ) is not None:
                    # 모델은 통과라 했으나 마커가 남아 있다 — 교차확인 불일치.
                    # 통과로 확정하지 않고 계속한다(오탐으로 차단 페이지를 정상으로
                    # 둔갑시키지 않게). 감사 로그에 경고를 남긴다.
                    logger.warning(
                        "AI 챌린지 통과 판정이 challenge_reason 교차확인과 불일치 — "
                        "계속 진행: job %d · %s", self.job_id, page.url,
                    )
                    self._audit_recheck_mismatch(round_index, analysis)
                    last_attempt = analysis or "(통과로 봤으나 교차확인 불일치)"
                    continue
                logger.info("AI 챌린지 통과 — 캡처 진행: job %d", self.job_id)
                return page.content(), None
            if verdict == "fail":
                logger.info("AI 챌린지 통과 불가(fail) — 사람 개입으로: job %d", self.job_id)
                return page.content(), reason
            last_attempt = analysis or "(계속 — 추가 입력 필요)"

        logger.info("AI 챌린지 라운드 소진 — 사람 개입으로: job %d", self.job_id)
        return page.content(), reason

    def _context(self, page, round_index: int, max_rounds: int,
                 vw: int, vh: int) -> dict:
        """프롬프트 치환·요약에 쓰는 런타임 컨텍스트."""
        try:
            title = page.title()
        except Exception:
            title = ""
        return {
            "viewport_w": vw, "viewport_h": vh,
            "url": page.url, "title": title,
            "round_index": round_index, "max_rounds": max_rounds,
        }

    def _screenshot(self, page) -> str | None:
        """뷰포트 스크린샷(JPEG)을 base64 문자열로. 실패면 None(라운드 포기)."""
        try:
            data = page.screenshot(type="jpeg", quality=70, full_page=False)
        except Exception as e:
            logger.warning("AI 챌린지 스크린샷 실패 — 중단: %s", e)
            return None
        return base64.b64encode(data).decode("ascii")

    def _call_llm(self, cfg: dict, system_prompt: str, ctx: dict,
                  shot_b64: str) -> str | None:
        """OpenAI 호환 /chat/completions 로 스크린샷+프롬프트를 보내 응답 텍스트.

        비2xx·타임아웃·네트워크 오류·예기치 못한 응답 형태는 모두 None 으로
        흡수한다(예외 전파 금지) — 해당 라운드만 실패로 처리되고 캡처 파이프라인은
        죽지 않는다. base_url 은 끝의 '/' 를 떼고 /chat/completions 를 붙인다.
        """
        endpoint = cfg["base_url"].rstrip("/") + "/chat/completions"
        user_text = (
            f"라운드 {ctx['round_index']}/{ctx['max_rounds']} · "
            f"뷰포트 {ctx['viewport_w']}×{ctx['viewport_h']} · "
            f"현재 URL: {ctx['url']} · 제목: {ctx['title']}"
        )
        payload = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{shot_b64}"}},
                ]},
            ],
        }
        headers = {"Authorization": f"Bearer {cfg['api_key']}"}
        try:
            resp = httpx.post(endpoint, json=payload, headers=headers,
                              timeout=cfg["request_timeout"])
        except httpx.HTTPError as e:
            logger.warning("AI 챌린지 LLM 호출 실패(네트워크/타임아웃): %s", e)
            return None
        if resp.status_code // 100 != 2:
            logger.warning("AI 챌린지 LLM 비2xx 응답: %d %s",
                           resp.status_code, resp.text[:200])
            return None
        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            logger.warning("AI 챌린지 LLM 응답 형태 예외: %s", e)
            return None

    def _audit_recheck_mismatch(self, round_index: int, analysis: str) -> None:
        """교차확인 불일치 경고를 audit_logs 에 남긴다(웹 요청 밖이라 직접 기록)."""
        message = (
            f"AI 챌린지 통과 판정이 교차확인과 불일치 (job {self.job_id}, "
            f"라운드 {round_index}): {analysis[:200]}"
        )
        try:
            with db.connect() as conn:
                db.insert_audit_log(
                    conn,
                    created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    actor="시스템(AI 챌린지)",
                    action="admin",
                    message=message,
                )
        except Exception as e:  # 감사 로그 실패가 캡처를 막지 않게
            logger.warning("AI 챌린지 교차확인 경고 audit 기록 실패: %s", e)


def _summarize_actions(cmds: list[dict]) -> str:
    """판정 프롬프트에 넣을 '방금 수행한 동작' 한 줄 요약."""
    if not cmds:
        return "(없음)"
    parts = []
    for c in cmds:
        if c["kind"] in ("click", "down", "up"):
            parts.append(f"{c['kind']}({c['x']},{c['y']})")
        elif c["kind"] == "key":
            parts.append(f"key({c['key']})")
        elif c["kind"] == "text":
            parts.append(f"type({c['key'][:20]!r})")
    return ", ".join(parts)
