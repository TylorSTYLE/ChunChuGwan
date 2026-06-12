"""아카이브 실행 로그 — db 레이어, 파이프라인 기록, /logs 대시보드 테스트."""
import json

import pytest
from fastapi.testclient import TestClient

from chunchugwan import capture, config, db, pipeline, storage
from chunchugwan.web import app as web_app


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    """임시 아카이브 루트 (인증 off)."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    return tmp_path


def _seed_logs() -> int:
    """페이지 1개 + 로그 2건(성공/실패)을 넣고 page_id 반환."""
    with db.connect() as conn:
        page_id = db.get_or_create_page(
            conn, "https://a.com/x", "a.com", "x-12345678"
        )
        db.insert_archive_log(
            conn, url="https://a.com/x", domain="a.com", page_id=page_id,
            snapshot_id=None, source="cli", status="new",
            started_at="2026-06-11T00:00:00+00:00", duration_ms=1200,
            http_status=200, content_hash="ab" * 32,
            steps=json.dumps([{"step": "capture", "ms": 900, "detail": "http 200"}]),
        )
        db.insert_archive_log(
            conn, url="https://b.com/y", domain="b.com", source="web",
            status="error", started_at="2026-06-11T00:01:00+00:00",
            duration_ms=300, error="CaptureError: boom",
        )
    return page_id


# ---- db 레이어 ----


def test_insert_and_filter_logs(archive_env):
    page_id = _seed_logs()
    with db.connect() as conn:
        logs = db.list_archive_logs(conn)
        assert [r["status"] for r in logs] == ["error", "new"]  # 최신 순

        assert [r["domain"] for r in db.list_archive_logs(conn, domain="a.com")] == ["a.com"]
        assert db.list_archive_logs(conn, status="error")[0]["error"] == "CaptureError: boom"
        assert db.list_archive_logs(conn, page_id=page_id)[0]["page_id"] == page_id
        assert db.list_archive_logs(conn, limit=1)[0]["status"] == "error"
        assert db.list_log_domains(conn) == ["a.com", "b.com"]


def test_list_logs_date_filter_and_paging(archive_env):
    with db.connect() as conn:
        for i, day in enumerate(("2026-06-01", "2026-06-05", "2026-06-10")):
            db.insert_archive_log(
                conn, url=f"https://a.com/{i}", domain="a.com", source="cli",
                status="new", started_at=f"{day}T12:00:00+00:00", duration_ms=10,
            )
    with db.connect() as conn:
        assert db.count_archive_logs(conn) == 3
        # 하한·상한 모두 해당 날짜 포함
        assert [r["url"] for r in db.list_archive_logs(conn, date_from="2026-06-05")] \
            == ["https://a.com/2", "https://a.com/1"]
        assert [r["url"] for r in db.list_archive_logs(conn, date_to="2026-06-05")] \
            == ["https://a.com/1", "https://a.com/0"]
        assert [r["url"] for r in db.list_archive_logs(
            conn, date_from="2026-06-05", date_to="2026-06-05"
        )] == ["https://a.com/1"]
        assert db.count_archive_logs(
            conn, date_from="2026-06-02", date_to="2026-06-10"
        ) == 2
        # offset 페이징 (최신 순)
        assert [r["url"] for r in db.list_archive_logs(conn, limit=1, offset=1)] \
            == ["https://a.com/1"]


def test_insert_log_rejects_unknown_column(archive_env):
    with db.connect() as conn:
        with pytest.raises(ValueError):
            db.insert_archive_log(
                conn, url="https://a.com", status="new",
                started_at="2026-06-11T00:00:00+00:00", bogus=1,
            )


# ---- 파이프라인 기록 ----


def _fake_capture(html: str):
    def fake(url, out_dir, remove_selectors=(), link_rewriter=None):
        return capture.CaptureResult(
            final_url=url, http_status=200, title="제목",
            raw_html=html, content_html=html,
        )
    return fake


def test_pipeline_writes_success_log(archive_env, monkeypatch):
    monkeypatch.setattr(
        pipeline.capture, "capture",
        _fake_capture("<html><body><p>본문 텍스트</p></body></html>"),
    )
    outcome = pipeline.archive_url("https://example.com/post")
    assert outcome.status == "new"

    with db.connect() as conn:
        logs = db.list_archive_logs(conn)
    assert len(logs) == 1
    log = logs[0]
    assert log["status"] == "new"
    assert log["source"] == "cli"
    assert log["domain"] == "example.com"
    assert log["http_status"] == 200
    assert log["content_hash"] == outcome.content_hash
    assert log["snapshot_id"] is not None
    steps = json.loads(log["steps"])
    assert [s["step"] for s in steps] == [
        "normalize", "capture", "extract", "hash", "compress", "store"
    ]

    # 같은 내용 재실행 → unchanged 로그 (스냅샷 없음)
    pipeline.archive_url("https://example.com/post")
    with db.connect() as conn:
        logs = db.list_archive_logs(conn)
    assert len(logs) == 2
    assert logs[0]["status"] == "unchanged"
    assert logs[0]["snapshot_id"] is None
    assert json.loads(logs[0]["steps"])[-1]["step"] == "decide"


def _https_only_fails(html: str):
    """https 는 CaptureError, http 는 성공하는 가짜 capture (HTTP 전용 사이트 흉내)."""
    def fake(url, out_dir, remove_selectors=(), link_rewriter=None):
        if url.startswith("https://"):
            raise capture.CaptureError(f"{url} 캡처 실패: Timeout 30000ms exceeded.")
        return capture.CaptureResult(
            final_url=url, http_status=200, title="제목",
            raw_html=html, content_html=html,
        )
    return fake


def test_pipeline_falls_back_to_http_when_scheme_inferred(archive_env, monkeypatch):
    monkeypatch.setattr(
        pipeline.capture, "capture",
        _https_only_fails("<html><body><p>본문</p></body></html>"),
    )
    # 스킴 생략 입력 → https 실패 후 http 로 재시도해 성공
    outcome = pipeline.archive_url("example.com/post")
    assert outcome.status == "new"
    assert outcome.url == "http://example.com/post"

    with db.connect() as conn:
        log = db.list_archive_logs(conn)[0]
    assert log["status"] == "new"
    assert log["url"] == "http://example.com/post"
    steps = json.loads(log["steps"])
    assert [s["step"] for s in steps] == [
        "normalize", "capture", "capture", "extract", "hash", "compress", "store"
    ]
    assert "http 로 재시도" in steps[1]["detail"]


def test_pipeline_http_fallback_for_explicit_https_on_connect_error(
    archive_env, monkeypatch
):
    html = "<html><body><p>본문</p></body></html>"

    def fake(url, out_dir, remove_selectors=(), link_rewriter=None):
        if url.startswith("https://"):
            raise capture.CaptureConnectError(f"{url} 캡처 실패: 연결 불가")
        return capture.CaptureResult(
            final_url=url, http_status=200, title="제목",
            raw_html=html, content_html=html,
        )

    monkeypatch.setattr(pipeline.capture, "capture", fake)
    # 명시적 https 라도 서버 연결 자체가 안 되면 http 로 폴백 (HTTP 전용 사이트)
    outcome = pipeline.archive_url("https://example.com/post")
    assert outcome.status == "new"
    assert outcome.url == "http://example.com/post"

    with db.connect() as conn:
        log = db.list_archive_logs(conn)[0]
    assert log["status"] == "new"
    assert log["url"] == "http://example.com/post"
    assert "http 로 재시도" in json.loads(log["steps"])[1]["detail"]


def test_pipeline_no_http_fallback_for_explicit_https(archive_env, monkeypatch):
    monkeypatch.setattr(
        pipeline.capture, "capture", _https_only_fails("<html></html>")
    )
    # 사용자가 명시한 https 는 폴백하지 않고 그대로 실패
    with pytest.raises(capture.CaptureError):
        pipeline.archive_url("https://example.com/post")

    with db.connect() as conn:
        log = db.list_archive_logs(conn)[0]
    assert log["status"] == "error"
    assert log["url"] == "https://example.com/post"


def test_pipeline_writes_error_log(archive_env, monkeypatch):
    def boom(url, out_dir, remove_selectors=(), link_rewriter=None):
        raise capture.CaptureError("페이지 로드 실패")

    monkeypatch.setattr(pipeline.capture, "capture", boom)
    with pytest.raises(capture.CaptureError):
        pipeline.archive_url("https://example.com/post", source="web")

    with db.connect() as conn:
        logs = db.list_archive_logs(conn)
    assert len(logs) == 1
    log = logs[0]
    assert log["status"] == "error"
    assert log["source"] == "web"
    assert log["page_id"] is None  # 캡처 실패 — 페이지 생성 전
    assert "CaptureError" in log["error"] and "페이지 로드 실패" in log["error"]


def test_pipeline_logs_invalid_url(archive_env):
    with pytest.raises(ValueError):
        pipeline.archive_url("ftp://not-a-url")
    with db.connect() as conn:
        logs = db.list_archive_logs(conn)
    assert len(logs) == 1
    assert logs[0]["status"] == "error"
    assert logs[0]["url"] == "ftp://not-a-url"  # 정규화 실패 시 입력 원본


# ---- /logs 대시보드 ----


@pytest.fixture
def client(archive_env):
    _seed_logs()
    return TestClient(web_app.app)


def test_logs_page(client):
    res = client.get("/logs")
    assert res.status_code == 200
    assert "https://a.com/x" in res.text
    assert "https://b.com/y" in res.text
    assert "CaptureError: boom" in res.text  # 상세 펼침 영역
    assert "capture" in res.text             # 단계 기록


def test_logs_filter_by_domain(client):
    res = client.get("/logs?domain=a.com")
    assert res.status_code == 200
    assert "https://a.com/x" in res.text
    assert "https://b.com/y" not in res.text


def test_logs_filter_by_status(client):
    res = client.get("/logs?status=error")
    assert res.status_code == 200
    assert "https://b.com/y" in res.text
    assert "https://a.com/x" not in res.text


def test_logs_filter_by_page(client):
    res = client.get("/logs?page_id=1")
    assert res.status_code == 200
    assert "https://a.com/x" in res.text
    assert "https://b.com/y" not in res.text


def test_logs_ignores_invalid_status(client):
    res = client.get("/logs?status=evil")
    assert res.status_code == 200
    assert "https://a.com/x" in res.text and "https://b.com/y" in res.text


def test_logs_filter_by_date(client):
    # 시드 로그 2건 모두 2026-06-11 — 범위 안이면 표시, 밖이면 비움
    res = client.get("/logs?date_from=2026-06-11&date_to=2026-06-11")
    assert res.status_code == 200
    assert "https://a.com/x" in res.text and "https://b.com/y" in res.text

    res = client.get("/logs?date_from=2026-06-12")
    assert "https://a.com/x" not in res.text and "https://b.com/y" not in res.text
    assert "조건에 맞는 로그가 없습니다" in res.text

    res = client.get("/logs?date_to=2026-06-10")
    assert "https://a.com/x" not in res.text and "https://b.com/y" not in res.text

    # from > to 이면 맞바꿔 적용
    res = client.get("/logs?date_from=2026-06-12&date_to=2026-06-10")
    assert "https://a.com/x" in res.text and "https://b.com/y" in res.text


def test_logs_ignores_invalid_date(client):
    res = client.get("/logs?date_from=junk&date_to=2026-13-99")
    assert res.status_code == 200
    assert "https://a.com/x" in res.text and "https://b.com/y" in res.text


def test_logs_pagination(client):
    # 시드 2건 + 9건 추가 = 11건 → limit=10 이면 2페이지
    with db.connect() as conn:
        for i in range(9):
            db.insert_archive_log(
                conn, url=f"https://b.com/p{i}", domain="b.com", source="cli",
                status="new", started_at=f"2026-06-11T01:00:0{i}+00:00",
                duration_ms=10,
            )
    res = client.get("/logs?limit=10")
    assert res.status_code == 200
    # 1페이지는 최신 10건 — 가장 오래된 a.com/x 는 2페이지로 밀린다
    assert "https://b.com/y" in res.text and "https://a.com/x" not in res.text
    assert "총 11건" in res.text and "1/2 페이지" in res.text
    assert "/logs?limit=10&amp;page=2" in res.text  # 다음 링크에 필터 유지

    res = client.get("/logs?limit=10&page=2")
    assert "https://a.com/x" in res.text and "https://b.com/y" not in res.text

    # 범위 밖 페이지 번호는 마지막 페이지로 보정
    res = client.get("/logs?limit=10&page=99")
    assert "https://a.com/x" in res.text


def test_logs_limit_select(client):
    # 기본값 25 — 페이징 링크에 limit 을 붙이지 않고, 25줄이 선택된 상태
    res = client.get("/logs")
    assert res.status_code == 200
    assert '<option value="25" selected>25줄</option>' in res.text

    # 허용 목록(10/25/50/100/200) 밖의 값은 기본값 25 로 보정
    res = client.get("/logs?limit=7")
    assert res.status_code == 200
    assert '<option value="25" selected>25줄</option>' in res.text
    assert "https://a.com/x" in res.text and "https://b.com/y" in res.text

    res = client.get("/logs?limit=200")
    assert '<option value="200" selected>200줄</option>' in res.text


# ---- 저장 파일 목록/용량 표시 ----


def _fake_capture_with_files(html: str):
    """캡처 산출물(raw/page/screenshot)을 실제로 기록하는 fake."""
    def fake(url, out_dir, remove_selectors=(), link_rewriter=None):
        (out_dir / "raw.html").write_text(html, encoding="utf-8")
        (out_dir / "page.html").write_text(html * 2, encoding="utf-8")
        (out_dir / "screenshot.png").write_bytes(b"\x89PNG" + b"0" * 2048)
        return capture.CaptureResult(
            final_url=url, http_status=200, title="제목",
            raw_html=html, content_html=html,
        )
    return fake


def test_list_logs_includes_snapshot_dir_info(archive_env, monkeypatch):
    monkeypatch.setattr(
        pipeline.capture, "capture",
        _fake_capture_with_files("<html><body><p>본문 텍스트</p></body></html>"),
    )
    pipeline.archive_url("https://example.com/post")
    pipeline.archive_url("https://example.com/post")  # unchanged — 스냅샷 없음

    with db.connect() as conn:
        logs = db.list_archive_logs(conn)
    assert logs[0]["status"] == "unchanged"
    assert logs[0]["snap_dir_name"] is None
    new_log = logs[1]
    assert new_log["status"] == "new"
    assert new_log["snap_domain"] == "example.com"
    assert new_log["snap_dir_name"]
    snap_dir = (
        storage.page_dir(new_log["snap_domain"], new_log["snap_slug"])
        / new_log["snap_dir_name"]
    )
    # 파이프라인 압축 변환 후 — HTML 은 gzip, 스크린샷은 가짜 PNG 라
    # WebP 디코딩이 실패해 원본 PNG 가 유지된다 (폴백 검증)
    files = storage.snapshot_files(snap_dir)
    assert [f["name"] for f in files] == [
        "page.html.gz", "raw.html.gz", "content.md", "screenshot.png", "meta.json"
    ]
    assert all(f["bytes"] > 0 for f in files)


def test_snapshot_files_missing_dir(tmp_path):
    assert storage.snapshot_files(tmp_path / "없는-디렉토리") == []


def test_logs_page_shows_files_and_sizes(archive_env, monkeypatch):
    monkeypatch.setattr(
        pipeline.capture, "capture",
        _fake_capture_with_files("<html><body><p>본문 텍스트</p></body></html>"),
    )
    pipeline.archive_url("https://example.com/post")
    res = TestClient(web_app.app).get("/logs")
    assert res.status_code == 200
    for name in ("page.html.gz", "raw.html.gz", "content.md", "screenshot.png", "meta.json"):
        assert name in res.text
    assert "2.0 KB" in res.text  # screenshot.png (2052 B)
    assert "합계 (5개)" in res.text
    with db.connect() as conn:
        snap_id = db.list_archive_logs(conn)[0]["snapshot_id"]
    assert f"/snapshot/{snap_id}/file/page.html" in res.text  # 보기 링크 (논리 이름)
    assert f"/snapshot/{snap_id}/file/raw.html" not in res.text  # 서빙 비허용 파일


def test_filesize_filter():
    from chunchugwan.web.templating import filesize

    assert filesize(None) == "-"
    assert filesize(0) == "0 B"
    assert filesize(532) == "532 B"
    assert filesize(1024) == "1.0 KB"
    assert filesize(1024 * 1024 * 2) == "2.0 MB"
    assert filesize(1024 ** 3 * 5) == "5.0 GB"
