"""아카이빙 중 DB 쓰기 락 점유 회귀 방지.

파이프라인이 문서 다운로드·압축·파일 확정·검색 색인 같은 느린 I/O 를 DB
트랜잭션 안에서 하면, 그동안 쓰기 락을 쥐어 다른 워커·serve 의 폴링이
'database is locked' (sqlite3.OperationalError, busy_timeout 초과)로 죽는다.
실제 운영에서 worker 의 crawler/archive_worker 폴링이 이 오류로 떨어졌다.

검증은 '락이 잡혔는지'를 별도 연결로 떠보는 대신(동시 writer 가 있으면
불안정) 구조적으로 한다:
- 느린 작업(압축) 시점에 파이프라인이 DB 연결을 하나도 쥐고 있지 않아야 한다.
- 검색 색인은 스냅샷 기록과 분리된 새 연결에서 돌아야 한다(진입 시 그 연결에
  열린 트랜잭션이 없어야 한다 — 본문 추출 동안 쓰기 락을 안 잡는다는 뜻).
"""
import contextlib
import threading

import pytest

from chunchugwan import config, db, pipeline, resources, searchindex


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    """임시 아카이브 루트."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    return tmp_path


def _fake_capture(monkeypatch, body: str = "본문"):
    def fake(url, out_dir, remove_selectors=(), link_rewriter=None, session=None,
             resource_fallback=None, **kwargs):
        from chunchugwan import capture
        html = f"<html><body>{body}</body></html>"
        (out_dir / "page.html").write_text(html, encoding="utf-8")
        (out_dir / "raw.html").write_text(html, encoding="utf-8")
        return capture.CaptureResult(
            final_url=url, http_status=200, title="제목",
            raw_html=html, content_html=html,
        )

    monkeypatch.setattr(pipeline.capture, "capture", fake)


def test_no_db_connection_held_during_slow_ops(archive_env, monkeypatch):
    """느린 작업(자원 압축) 중에는 파이프라인이 DB 연결을 쥐고 있지 않는다.

    압축이 트랜잭션 밖에서 돌아야 그 사이 다른 워커가 DB 에 쓸 수 있다.
    파이프라인이 여는 연결 수를 (현재 스레드 한정으로) 세어, 압축 시점에 0
    인지 본다. 회귀하면 압축이 페이지/스냅샷 INSERT 와 같은 트랜잭션 안에
    있어 그 순간 연결 수가 1 이 된다.
    """
    _fake_capture(monkeypatch)
    real_connect = db.connect
    main_ident = threading.get_ident()
    state = {"active": 0, "during_compact": None}

    @contextlib.contextmanager
    def counting_connect():
        # 다른 스레드(누수된 백그라운드 워커 등)의 연결은 세지 않는다 — 결정성 확보
        same = threading.get_ident() == main_ident
        if same:
            state["active"] += 1
        try:
            with real_connect() as conn:
                yield conn
        finally:
            if same:
                state["active"] -= 1

    monkeypatch.setattr(pipeline.db, "connect", counting_connect)

    real_compact = resources.compact_snapshot_dir

    def probing_compact(tmp_dir, final_url):
        state["during_compact"] = state["active"]
        return real_compact(tmp_dir, final_url)

    monkeypatch.setattr(pipeline.resources, "compact_snapshot_dir", probing_compact)

    outcome = pipeline.archive_url("https://example.com/lock-check")
    assert outcome.status == "new"
    assert state["during_compact"] == 0, (
        "느린 작업(압축) 중 파이프라인이 DB 연결/트랜잭션을 쥐고 있었다"
    )
    # 락 분리가 저장을 깨지 않는지도 확인
    with db.connect() as conn:
        snaps = conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()
        log = conn.execute(
            "SELECT status FROM archive_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert snaps["n"] == 1
    assert log["status"] == "new"


def test_indexing_runs_outside_snapshot_transaction(archive_env, monkeypatch):
    """검색 색인은 스냅샷 기록 트랜잭션과 분리된 연결에서 돈다.

    같은 연결/트랜잭션이면 큰 문서 본문 추출(doctext) 동안 스냅샷 INSERT 의
    쓰기 락이 잡힌 채로 있다. 색인 진입 시 그 연결에 열린 트랜잭션이 없어야
    한다 = 직전에 쓰기를 한 연결이 아니라 새 연결이라는 뜻.
    """
    _fake_capture(monkeypatch)
    real_index = searchindex.index_snapshot
    seen = {}

    def probing_index(conn, snapshot_id):
        seen["in_txn_at_entry"] = conn.in_transaction
        return real_index(conn, snapshot_id)

    monkeypatch.setattr(pipeline.searchindex, "index_snapshot", probing_index)

    outcome = pipeline.archive_url("https://example.com/index-lock")
    assert outcome.status == "new"
    assert seen.get("in_txn_at_entry") is False, (
        "색인이 스냅샷 기록 트랜잭션 안에서 실행됐다 (별도 연결이어야 한다)"
    )
