"""AI 자동 챌린지 해결 (ai_challenge.py) — _normalize 가드·solve 루프·캐스케이드."""
import pytest

from chunchugwan import ai_challenge, config, db

URL = "https://sd.test/article"
_CHALLENGE = "<html><body><div class='cf-turnstile'></div></body></html>"
_PASSED = "<html><body><article>통과 후 실제 본문</article></body></html>"


# ── _parse_json_object ───────────────────────────────────────────────────────

def test_parse_json_strips_markdown_fence():
    text = "```json\n{\"verdict\":\"success\"}\n```"
    assert ai_challenge._parse_json_object(text) == {"verdict": "success"}


def test_parse_json_takes_first_to_last_brace():
    text = "여기 결과입니다: {\"giveup\": true} 끝."
    assert ai_challenge._parse_json_object(text) == {"giveup": True}


def test_parse_json_returns_none_on_garbage():
    assert ai_challenge._parse_json_object("그냥 설명 문장") is None
    assert ai_challenge._parse_json_object("") is None
    assert ai_challenge._parse_json_object("{not json}") is None


# ── _normalize 가드 ──────────────────────────────────────────────────────────

def test_normalize_four_action_types_to_primitives():
    actions = [
        {"type": "click", "x": 10, "y": 20},
        {"type": "type", "text": "hello"},
        {"type": "key", "key": "Enter"},
        {"type": "drag", "from": {"x": 1, "y": 2}, "to": {"x": 3, "y": 4}},
    ]
    cmds = ai_challenge._normalize(actions, 1280, 800, 10)
    assert cmds == [
        {"kind": "click", "x": 10, "y": 20, "key": None, "delay_ms": 0},
        {"kind": "text", "x": None, "y": None, "key": "hello", "delay_ms": 0},
        {"kind": "key", "x": None, "y": None, "key": "Enter", "delay_ms": 0},
        {"kind": "down", "x": 1, "y": 2, "key": None, "delay_ms": 0},
        {"kind": "up", "x": 3, "y": 4, "key": None, "delay_ms": 0},
    ]


def test_normalize_rejects_non_whitelisted_type():
    cmds = ai_challenge._normalize(
        [{"type": "navigate", "url": "http://evil"}, {"type": "click", "x": 5, "y": 5}],
        1280, 800, 10,
    )
    assert [c["kind"] for c in cmds] == ["click"]  # navigate 거부, click 만 통과


def test_normalize_rejects_unsafe_key():
    cmds = ai_challenge._normalize(
        [{"type": "key", "key": "F1"}, {"type": "key", "key": "a"},
         {"type": "key", "key": "ArrowDown"}],
        1280, 800, 10,
    )
    assert [c["key"] for c in cmds] == ["a", "ArrowDown"]  # F1 거부, 정규화 보존


def test_normalize_clamps_coordinates():
    cmds = ai_challenge._normalize(
        [{"type": "click", "x": 99999, "y": -50}], 1280, 800, 10,
    )
    assert cmds[0]["x"] == 1279 and cmds[0]["y"] == 0


def test_normalize_clamps_delay():
    cmds = ai_challenge._normalize(
        [{"type": "click", "x": 0, "y": 0, "delay_ms": 99999}], 1280, 800, 10,
    )
    assert cmds[0]["delay_ms"] == 3000


def test_normalize_truncates_per_round_cap():
    actions = [{"type": "click", "x": i, "y": i} for i in range(20)]
    cmds = ai_challenge._normalize(actions, 1280, 800, 3)
    assert len(cmds) == 3  # 상한 3 초과분 절단


def test_normalize_non_list_returns_empty():
    assert ai_challenge._normalize(None, 1280, 800, 10) == []
    assert ai_challenge._normalize("nope", 1280, 800, 10) == []


# ── solve 루프 (모의 page + 모의 _call_llm) ──────────────────────────────────

class _MockPage:
    """ai_challenge.solve 가 쓰는 메서드만 흉내낸다."""

    def __init__(self, url=URL, content=_CHALLENGE):
        self.url = url
        self._content = content
        self.clicks = []
        self.keys = []
        self.shots = 0

    def set_viewport_size(self, vp):
        pass

    def content(self):
        return self._content

    def title(self):
        return "t"

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, **kw):
        self.shots += 1
        return b"\xff\xd8\xff\xe0jpegfake"

    class _Mouse:
        def __init__(self, p):
            self.p = p

        def click(self, x, y):
            self.p.clicks.append((x, y))

        def move(self, x, y):
            pass

        def down(self):
            pass

        def up(self):
            pass

    class _Kbd:
        def __init__(self, p):
            self.p = p

        def press(self, k):
            self.p.keys.append(("press", k))

        def type(self, k):
            self.p.keys.append(("type", k))

    @property
    def mouse(self):
        if not hasattr(self, "_m"):
            self._m = _MockPage._Mouse(self)
        return self._m

    @property
    def keyboard(self):
        if not hasattr(self, "_k"):
            self._k = _MockPage._Kbd(self)
        return self._k


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    with db.connect() as conn:  # 스키마 초기화
        conn.execute("SELECT 1")
    return tmp_path


def _stub_llm(session, responses):
    """세션의 _call_llm 을 큐 응답으로 대체 (action·verdict 교대 호출)."""
    it = iter(responses)
    session._call_llm = lambda cfg, prompt, ctx, shot: next(it)


_ACTION = '{"analysis":"게이트 클릭","actions":[{"type":"click","x":10,"y":20}],"giveup":false}'


def test_solve_passes_on_success_verdict(temp_db):
    with db.connect() as conn:
        db.set_setting(conn, db.AI_CHALLENGE_SUCCESS_RECHECK_KEY, "off")
    sess = ai_challenge.AIChallengeSession(1)
    page = _MockPage(content=_PASSED)
    _stub_llm(sess, [_ACTION, '{"verdict":"success"}'])
    raw_html, reason = sess.solve(page, "원래 사유")
    assert reason is None
    assert (10, 20) in page.clicks  # 액션이 재생됐다


def test_solve_giveup_cascades_to_human(temp_db):
    sess = ai_challenge.AIChallengeSession(1)
    page = _MockPage()
    _stub_llm(sess, ['{"giveup":true,"reason":"게이트 아님"}'])
    raw_html, reason = sess.solve(page, "차단 사유")
    assert reason == "차단 사유"  # giveup → C 로 캐스케이드
    assert page.clicks == []


def test_solve_fail_verdict_cascades(temp_db):
    sess = ai_challenge.AIChallengeSession(1)
    page = _MockPage()
    _stub_llm(sess, [_ACTION, '{"verdict":"fail"}'])
    raw_html, reason = sess.solve(page, "차단 사유")
    assert reason == "차단 사유"


def test_solve_exhausts_rounds(temp_db):
    with db.connect() as conn:
        db.set_setting(conn, db.AI_CHALLENGE_MAX_ROUNDS_KEY, "2")
    sess = ai_challenge.AIChallengeSession(1)
    page = _MockPage()
    # 매 라운드 continue → 2라운드 소진 → reason 유지
    _stub_llm(sess, [_ACTION, '{"verdict":"continue"}',
                     _ACTION, '{"verdict":"continue"}'])
    raw_html, reason = sess.solve(page, "차단 사유")
    assert reason == "차단 사유"
    assert page.shots == 4  # 라운드당 입력·판정 2장 × 2라운드


def test_solve_success_recheck_mismatch_continues_and_audits(temp_db):
    with db.connect() as conn:
        db.set_setting(conn, db.AI_CHALLENGE_MAX_ROUNDS_KEY, "1")
        db.set_setting(conn, db.AI_CHALLENGE_SUCCESS_RECHECK_KEY, "on")
    sess = ai_challenge.AIChallengeSession(7)
    page = _MockPage(content=_CHALLENGE)  # 마커가 남아있다
    _stub_llm(sess, [_ACTION, '{"verdict":"success","analysis":"통과로 보임"}'])
    raw_html, reason = sess.solve(page, "차단 사유")
    assert reason == "차단 사유"  # 교차확인 불일치 → 통과 확정 안 함 → 소진 → C
    with db.connect() as conn:
        logs = db.list_audit_logs(conn)
    assert any("교차확인" in row["message"] for row in logs)


def test_solve_llm_failure_treated_as_round_failure(temp_db):
    with db.connect() as conn:
        db.set_setting(conn, db.AI_CHALLENGE_MAX_ROUNDS_KEY, "1")
    sess = ai_challenge.AIChallengeSession(1)
    page = _MockPage()
    _stub_llm(sess, [None])  # 액션 호출이 None(타임아웃/비2xx) → 파싱 실패 → 계속
    raw_html, reason = sess.solve(page, "차단 사유")
    assert reason == "차단 사유"  # 캡처를 죽이지 않고 라운드 실패로만 처리, 소진→C
    assert page.clicks == []


def test_solve_aborts_on_loopback(temp_db):
    sess = ai_challenge.AIChallengeSession(1)
    page = _MockPage(url="http://127.0.0.1:9000/admin")
    called = []
    sess._call_llm = lambda *a: called.append(1) or _ACTION
    raw_html, reason = sess.solve(page, "사유")
    assert reason == "사유"      # 루프백 → 즉시 중단
    assert called == []          # LLM 호출 전에 끊긴다


# ── archive_worker._ai_session_for 게이트 ────────────────────────────────────

_ITEM = {"id": 5, "network_tag_id": None}


def _configure_ai(secret="x"):
    with db.connect() as conn:
        db.set_setting(conn, db.AI_CHALLENGE_ENABLED_KEY, "on")
        db.set_setting(conn, db.AI_CHALLENGE_BASE_URL_KEY, "https://api.test/v1")
        db.set_setting(conn, db.AI_CHALLENGE_MODEL_KEY, "gpt-4o")
        from chunchugwan import crypto
        db.set_setting(conn, db.AI_CHALLENGE_API_KEY_KEY, crypto.encrypt("sk-1"))


def test_ai_session_for_disabled_returns_none(temp_db):
    from chunchugwan import archive_worker
    assert archive_worker._ai_session_for(_ITEM) is None  # 미설정(off)


def test_ai_session_for_incomplete_returns_none(temp_db, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    from chunchugwan import archive_worker
    with db.connect() as conn:
        db.set_setting(conn, db.AI_CHALLENGE_ENABLED_KEY, "on")
        db.set_setting(conn, db.AI_CHALLENGE_BASE_URL_KEY, "https://api.test/v1")
        # model·api_key 누락 → 완비 게이트 미달
    assert archive_worker._ai_session_for(_ITEM) is None


def test_ai_session_for_complete_returns_session(temp_db, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    from chunchugwan import archive_worker
    _configure_ai()
    sess = archive_worker._ai_session_for(_ITEM)
    assert isinstance(sess, ai_challenge.AIChallengeSession)
    assert sess.job_id == 5
