"""라이브 챌린지 세션 루프 (live_challenge.py) — 모의 page 로 검증."""
import time

import pytest

from chunchugwan import config, db, live_challenge, storage

URL = "https://sd.test/article"
_CHALLENGE = "<html><body><div class='cf-turnstile'></div></body></html>"
_PASSED = "<html><body><article>통과 후 실제 본문</article></body></html>"


@pytest.fixture
def job(tmp_path, monkeypatch):
    """임시 아카이브 + needs_human 진입 직전의 archive_jobs 작업 1건(claim된 상태)."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "LIVE_CHALLENGE_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(config, "LIVE_POLL_INTERVAL_MS", 300)
    monkeypatch.setattr(config, "LIVE_SHOT_INTERVAL_MS", 0)  # 매 반복 스크린샷
    with db.connect() as conn:
        db.get_or_create_page(conn, URL, "sd.test", storage.url_to_slug(URL))
        db.enqueue_archive_job(conn, URL, source="cli")
        j = db.claim_due_archive_job(conn, "2099-01-01T00:00:00+00:00")
    return j["id"]


class _MockPage:
    """sync Playwright page 의 라이브 세션이 쓰는 메서드만 흉내낸다."""

    def __init__(self, url=URL, challenge_iters=1, on_wait=None):
        self.url = url
        self._content_calls = 0
        self._challenge_iters = challenge_iters
        self._on_wait = on_wait
        self.clicks = []
        self.keys = []
        self.viewport = None
        self.shots = 0

    def set_viewport_size(self, vp):
        self.viewport = vp

    def content(self):
        self._content_calls += 1
        # 첫 호출(루프 진입 전) 포함 challenge_iters 번까지 챌린지, 이후 통과
        return _CHALLENGE if self._content_calls <= self._challenge_iters else _PASSED

    def title(self):
        return "t"

    def wait_for_timeout(self, ms):
        if self._on_wait:
            self._on_wait()
        time.sleep(min(ms, 30) / 1000)

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


def test_solve_passes_when_challenge_clears(job):
    sess = live_challenge.LiveChallengeSession(job)
    page = _MockPage(challenge_iters=2)   # 2번까지 챌린지, 이후 통과
    raw_html, reason = sess.solve(page, "원래 사유")
    assert reason is None
    assert "통과 후 실제 본문" in raw_html
    # 통과 후 라이브 상태가 정리된다 (clear_needs_human)
    with db.connect() as conn:
        assert db.get_archive_job(conn, job)["needs_human_at"] is None
    assert not live_challenge.shot_path(sess.token).exists()


def test_solve_replays_queued_commands(job):
    sess = live_challenge.LiveChallengeSession(job)
    with db.connect() as conn:  # 세션 토큰 앞으로 클릭·키 명령을 미리 큐잉
        db.enqueue_live_command(conn, sess.token, kind="click", x=50, y=60)
        db.enqueue_live_command(conn, sess.token, kind="text", key="hello", delay_ms=10)
    page = _MockPage(challenge_iters=2)
    sess.solve(page, "사유")
    assert (50, 60) in page.clicks
    assert ("type", "hello") in page.keys


def test_solve_times_out_and_keeps_reason(job):
    sess = live_challenge.LiveChallengeSession(job)
    page = _MockPage(challenge_iters=10_000)  # 영영 안 풀림
    t0 = time.monotonic()
    raw_html, reason = sess.solve(page, "차단 사유")
    assert reason == "차단 사유"            # 시간 초과 → 사유 유지(상위가 실패 처리)
    assert time.monotonic() - t0 >= 2       # 타임아웃(2s)까지 기다렸다
    with db.connect() as conn:
        assert db.get_archive_job(conn, job)["needs_human_at"] is None  # 정리됨


def test_solve_aborts_on_loopback_navigation(job):
    sess = live_challenge.LiveChallengeSession(job)
    page = _MockPage(url="http://127.0.0.1:9000/admin", challenge_iters=10_000)
    raw_html, reason = sess.solve(page, "사유")
    assert reason == "사유"                  # 루프백 이동 → 즉시 중단(저장 없음)
    assert page.clicks == []                 # 명령 재생 전에 끊긴다


def test_solve_aborts_on_cancel(job):
    sess = live_challenge.LiveChallengeSession(job)

    def cancel_now():
        with db.connect() as conn:
            db.set_live_cancel(conn, job)

    page = _MockPage(challenge_iters=10_000, on_wait=cancel_now)
    raw_html, reason = sess.solve(page, "사유")
    assert reason == "사유"                  # 사람이 취소 → 중단
