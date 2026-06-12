"""대시보드 라우트 테스트. 캡처 없이 fixture 데이터로 검증."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from chunchugwan import config, db, storage
from chunchugwan.web import app as web_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """임시 아카이브(페이지 1개 + 스냅샷 2개 + check 1개) 위의 TestClient."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "AUTH_ENABLED", False)  # 인증은 test_auth.py 에서 검증

    url = "https://example.com/post"
    domain, slug = "example.com", storage.url_to_slug(url)
    contents = ["첫 줄\n둘째 줄", "첫 줄\n둘째 줄 수정됨\n셋째 줄"]
    dir_names = ["2026-06-01T00-00-00", "2026-06-02T00-00-00"]

    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, url, domain, slug)
        for i, (text, dir_name) in enumerate(zip(contents, dir_names)):
            snap_dir = storage.page_dir(domain, slug) / dir_name
            snap_dir.mkdir(parents=True)
            (snap_dir / "content.md").write_text(text, encoding="utf-8")
            (snap_dir / "page.html").write_text(
                "<html><body><script>alert(1)</script>본문</body></html>", encoding="utf-8"
            )
            Image.new("RGB", (8, 8), (255 - i * 255,) * 3).save(snap_dir / "screenshot.png")
            db.insert_snapshot(
                conn, page_id,
                taken_at=f"2026-06-0{i + 1}T00:00:00+00:00", dir_name=dir_name,
                content_hash=storage.content_sha256(text),
                final_url=url, http_status=200, changed=1,
            )
        db.insert_check(conn, page_id, storage.content_sha256(contents[1]))

    # 첫 스냅샷에는 함께 저장된 문서 파일 + documents 목록을 가진 meta.json
    snap1_dir = storage.page_dir(domain, slug) / dir_names[0]
    (snap1_dir / "files").mkdir()
    (snap1_dir / "files" / "report-12345678.pdf").write_bytes(b"%PDF-1.4 fixture")
    (snap1_dir / "files" / "unlisted.pdf").write_bytes(b"%PDF-1.4 manifest-bayuk")
    storage.write_meta(snap1_dir, storage.SnapshotMeta(
        url=url, final_url=url, taken_at="2026-06-01T00:00:00+00:00",
        content_hash=storage.content_sha256(contents[0]), http_status=200,
        title="픽스처 글", documents=[{
            "url": "https://example.com/files/report.pdf",
            "file": "report-12345678.pdf", "bytes": 16,
            "sha256": "ab" * 32, "content_type": "application/pdf",
        }],
    ))

    web_app._active_jobs.clear()  # 다른 테스트의 진행 목록 잔재 제거
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def test_index(client):
    res = client.get("/archives")
    assert res.status_code == 200
    assert "https://example.com/post" in res.text
    # 새 아카이빙 폼은 목록에서 별도 메뉴(/archive/new)로 분리됐다
    assert 'action="/archive"' not in res.text
    assert 'href="/archive/new"' in res.text  # 헤더 메뉴


def test_root_serves_dashboard(client):
    """첫 페이지(/)는 현황 화면이고, 목록은 /archives 에 있다."""
    res = client.get("/")
    assert res.status_code == 200
    assert "현황" in res.text
    assert 'href="/archives"' in res.text  # 헤더 메뉴의 목록 링크


def test_timeline(client):
    res = client.get("/page/1")
    assert res.status_code == 200
    assert "2026-06-01T00:00:00+00:00" in res.text
    assert "변경 없음 확인 기록" in res.text


def test_timeline_404(client):
    assert client.get("/page/999").status_code == 404


def test_snapshot_view_sandboxed_iframe(client):
    res = client.get("/snapshot/1")
    assert res.status_code == 200
    assert 'sandbox=""' in res.text
    assert 'sandbox="allow' not in res.text  # iframe에 allow-* 토큰 금지


def test_theme_toggle_present(client):
    """모든 화면(base.html)에 테마 변수·토글·기억(localStorage) 스크립트가 있다."""
    res = client.get("/")
    assert res.status_code == 200
    assert 'data-theme="dark"' in res.text  # 다크 테마 변수 블록
    assert "prefers-color-scheme" in res.text  # 시스템 기본 설정 따름
    assert 'id="theme-toggle"' in res.text  # 헤더 토글 버튼
    assert "wccg-theme" in res.text  # localStorage 키 (사용자 선택 기억)


def test_time_toggle_present(client):
    """모든 화면(base.html)에 시간 표시(로컬/UTC) 토글·변환 스크립트가 있다."""
    res = client.get("/")
    assert res.status_code == 200
    assert 'id="time-toggle"' in res.text  # 헤더 토글 버튼
    assert "wccg-time" in res.text  # localStorage 키 (사용자 선택 기억)
    assert "time.ts" in res.text  # 변환 대상 셀렉터


def test_timestamps_rendered_as_time_elements(client):
    """타임스탬프는 <time class="ts" datetime=UTC ISO> 로 렌더링된다 (JS 토글용)."""
    res = client.get("/page/1")
    assert (
        '<time class="ts" data-fmt="datetime" '
        'datetime="2026-06-01T00:00:00+00:00">2026-06-01 00:00:00</time>'
    ) in res.text


def test_ts_filter():
    """ts 필터 — UTC 정규화, date 포맷, 빈 값/비정상 입력 처리."""
    from chunchugwan.web.templating import ts

    assert ts("2026-06-01T12:34:56+00:00") == (
        '<time class="ts" data-fmt="datetime" '
        'datetime="2026-06-01T12:34:56+00:00">2026-06-01 12:34:56</time>'
    )
    # 타임존 없는 값은 UTC 로 간주
    assert 'datetime="2026-06-01T12:34:56+00:00"' in ts("2026-06-01T12:34:56")
    # date 포맷 — 날짜만 표시
    assert ts("2026-06-01T12:34:56+00:00", "date") == (
        '<time class="ts" data-fmt="date" '
        'datetime="2026-06-01T12:34:56+00:00">2026-06-01</time>'
    )
    assert ts(None) == "-"
    assert ts("") == "-"
    assert ts("이상한 값") == "이상한 값"  # 파싱 불가 시 원문 유지


def test_snapshot_file_whitelist(client):
    ok = client.get("/snapshot/1/file/content.md")
    assert ok.status_code == 200 and "첫 줄" in ok.text
    page = client.get("/snapshot/1/file/page.html")
    assert page.status_code == 200
    assert page.headers["content-security-policy"] == "sandbox"
    assert client.get("/snapshot/1/file/meta.json").status_code == 404
    assert client.get("/snapshot/1/file/..%2F..%2Findex.db").status_code == 404


def test_snapshot_view_lists_documents(client):
    res = client.get("/snapshot/1")
    assert res.status_code == 200
    assert "첨부 문서" in res.text
    assert "/snapshot/1/doc/report-12345678.pdf" in res.text
    # 문서가 없는 스냅샷(meta 없음)에는 섹션 자체가 안 보인다
    res2 = client.get("/snapshot/2")
    assert res2.status_code == 200
    assert "첨부 문서" not in res2.text


def test_snapshot_document_download(client):
    res = client.get("/snapshot/1/doc/report-12345678.pdf")
    assert res.status_code == 200
    assert res.content == b"%PDF-1.4 fixture"
    # 브라우저 안에서 렌더링되지 않도록 항상 첨부파일 다운로드
    assert res.headers["content-type"].startswith("application/octet-stream")
    assert "attachment" in res.headers["content-disposition"]
    assert res.headers["content-security-policy"] == "sandbox"


def test_snapshot_document_rejects_unlisted_names(client):
    """meta.json 의 documents 목록에 없는 이름은 파일이 있어도 404."""
    assert client.get("/snapshot/1/doc/unlisted.pdf").status_code == 404
    assert client.get("/snapshot/1/doc/..%2Fmeta.json").status_code == 404
    # meta.json 자체가 없는 스냅샷도 404
    assert client.get("/snapshot/2/doc/report-12345678.pdf").status_code == 404


def test_diff_default_latest_two(client):
    res = client.get("/diff/1")
    assert res.status_code == 200
    assert "+2줄" in res.text and "-1줄" in res.text
    assert "둘째 줄 수정됨" in res.text


def test_diff_shows_pixel_ratio(client):
    res = client.get("/diff/1")
    assert res.status_code == 200
    assert "변경 픽셀 100.00%" in res.text  # 흰색 → 검은색


def test_shotdiff_image(client):
    res = client.get("/diff/1/shotdiff?from=1&to=2")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"


def test_diff_bad_range(client):
    assert client.get("/diff/1?from=2&to=1").status_code == 400


def test_rearchive_triggers_pipeline(client, monkeypatch):
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": calls.append((url, force)),
    )
    res = client.post("/page/1/rearchive", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/page/1?queued=1"
    assert calls == [("https://example.com/post", False)]


def test_rearchive_force(client, monkeypatch):
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": calls.append((url, force)),
    )
    res = client.post(
        "/page/1/rearchive", data={"force": "1"}, follow_redirects=False
    )
    assert res.status_code == 303
    assert calls == [("https://example.com/post", True)]


def test_rearchive_unknown_page(client):
    assert client.post("/page/999/rearchive").status_code == 404


def test_archive_new_url_triggers_pipeline(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": calls.append(url),
    )
    res = client.post(
        "/archive",
        data={"url": "https://example.com/new?utm_source=x"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"].startswith("/archives?queued=")
    # 정규화된 URL(트래킹 파라미터 제거)로 파이프라인 호출
    assert calls == ["https://example.com/new"]


def test_archive_invalid_url_rejected(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": calls.append(url),
    )
    res = client.post("/archive", data={"url": "ftp://example.com/x"}, follow_redirects=False)
    assert res.status_code == 303
    # 에러는 새 아카이빙 폼으로 돌아가 입력값을 유지한 채 보여준다
    assert res.headers["location"].startswith("/archive/new?error=")
    assert "url=ftp" in res.headers["location"]
    assert calls == []


def test_archive_new_form_page(client):
    """새 아카이빙 화면 — URL 입력과 자동 재아카이빙 주기 선택지를 제공한다."""
    res = client.get("/archive/new")
    assert res.status_code == 200
    assert 'action="/archive"' in res.text
    assert 'name="interval"' in res.text
    assert "사용 안 함" in res.text
    for label in ("1시간", "12시간", "1일", "1주일", "1개월"):
        assert f"{label}마다" in res.text


def test_archive_with_interval_sets_schedule(client, monkeypatch):
    """주기를 함께 등록하면 아카이빙 완료 후 스케줄이 생성된다."""
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": None,
    )
    res = client.post(
        "/archive",
        data={"url": "https://example.com/post", "interval": "3600"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"].startswith("/archives?queued=")
    with db.connect() as conn:
        sched = db.get_schedule(conn, 1)
    assert sched is not None and sched["interval_seconds"] == 3600


def test_archive_with_custom_interval_and_run_at(client, monkeypatch):
    """직접 입력 주기(2일) + 실행 시각이 스케줄에 반영된다."""
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": None,
    )
    res = client.post(
        "/archive",
        data={
            "url": "https://example.com/post", "interval": "custom",
            "custom_value": "2", "custom_unit": "d", "run_at": "09:00",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    with db.connect() as conn:
        sched = db.get_schedule(conn, 1)
    assert sched["interval_seconds"] == 2 * 86400
    assert sched["run_at_time"] == "09:00"


def test_archive_rejects_run_at_for_hourly_interval(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": calls.append(url),
    )
    res = client.post(
        "/archive",
        data={"url": "https://example.com/post", "interval": "3600", "run_at": "09:00"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"].startswith("/archive/new?error=")
    assert calls == []


def test_archive_without_interval_no_schedule(client, monkeypatch):
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": None,
    )
    client.post(
        "/archive", data={"url": "https://example.com/post"}, follow_redirects=False
    )
    with db.connect() as conn:
        assert db.get_schedule(conn, 1) is None


def test_archive_rejects_out_of_range_interval(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": calls.append(url),
    )
    res = client.post(
        "/archive",
        data={"url": "https://example.com/post", "interval": "60"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"].startswith("/archive/new?error=")
    assert calls == []
    with db.connect() as conn:
        assert db.get_schedule(conn, 1) is None


def test_archive_interval_skipped_when_page_missing(client, monkeypatch):
    """신규 URL 아카이빙이 실패하면 pages 행이 없어 주기 등록도 건너뛴다."""
    def boom(url, force=False, source="cli"):
        raise RuntimeError("캡처 실패")

    monkeypatch.setattr(web_app.pipeline, "archive_url", boom)
    res = client.post(
        "/archive",
        data={"url": "https://example.com/brand-new", "interval": "3600"},
        follow_redirects=False,
    )
    assert res.status_code == 303  # 백그라운드 실패가 응답을 깨지 않는다
    assert client.get("/archive/active").json() == {"active": []}
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 0


def test_index_shows_queued_banner(client):
    res = client.get("/archives?queued=https%3A%2F%2Fexample.com%2Fnew")
    assert res.status_code == 200
    assert "백그라운드에서 시작" in res.text


def test_archive_registers_active_job_and_clears_on_finish(client, monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": seen.append(
            sorted(web_app._active_snapshot())
        ),
    )
    res = client.post(
        "/archive", data={"url": "https://example.com/new"}, follow_redirects=False
    )
    assert res.status_code == 303
    # 파이프라인 실행 중에는 진행 목록에 있고, 끝나면 비워진다
    assert seen == [["https://example.com/new"]]
    assert client.get("/archive/active").json() == {"active": []}


def test_active_job_cleared_even_on_failure(client, monkeypatch):
    def boom(url, force=False, source="cli"):
        raise RuntimeError("캡처 실패")

    monkeypatch.setattr(web_app.pipeline, "archive_url", boom)
    client.post("/archive", data={"url": "https://example.com/new"}, follow_redirects=False)
    assert client.get("/archive/active").json() == {"active": []}


def test_index_shows_active_jobs(client):
    web_app._register_job("https://example.com/post")       # 기존 페이지 재아카이빙
    web_app._register_job("https://example.com/brand-new")  # 아직 pages 행 없는 신규 URL
    res = client.get("/archives")
    assert res.status_code == 200
    assert res.text.count("아카이빙 중") == 2
    assert "https://example.com/brand-new" in res.text
    # 진행 중인 페이지에는 재아카이빙 버튼을 숨긴다
    assert "재아카이빙" not in res.text


def test_archive_active_endpoint_sorted(client):
    web_app._register_job("https://b.example.com/x")
    web_app._register_job("https://a.example.com/x")
    assert client.get("/archive/active").json() == {
        "active": ["https://a.example.com/x", "https://b.example.com/x"]
    }


def test_archive_duplicate_url_not_requeued(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": calls.append(url),
    )
    web_app._register_job("https://example.com/new")  # 이미 진행 중인 상태
    res = client.post(
        "/archive", data={"url": "https://example.com/new"}, follow_redirects=False
    )
    assert res.status_code == 303
    assert calls == []


def test_period_starts_boundaries():
    now = datetime(2026, 6, 11, 15, 30, 45, tzinfo=timezone.utc)  # 목요일
    starts = web_app._period_starts(now)
    assert starts["today"] == "2026-06-11T00:00:00+00:00"
    assert starts["week"] == "2026-06-08T00:00:00+00:00"  # 월요일 자정
    assert starts["month"] == "2026-06-01T00:00:00+00:00"
    assert starts["year"] == "2026-01-01T00:00:00+00:00"
    assert starts["recent"] == "2026-06-10T15:30:45+00:00"


def test_dashboard_overview(client):
    res = client.get("/dashboard")
    assert res.status_code == 200
    assert 'id="stat-pages">1</div>' in res.text
    assert 'id="stat-snapshots">2</div>' in res.text
    assert "용량 트렌드" in res.text
    # 최근 아카이브 목록에 fixture 페이지가 보인다
    assert "https://example.com/post" in res.text


def test_dashboard_period_counts(client):
    # 지금 시각 스냅샷을 추가하면 오늘/이번 주/최근 24시간 집계에 모두 포함된다
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        db.insert_snapshot(
            conn, 1, taken_at=now.isoformat(timespec="seconds"), dir_name="now-dir",
            content_hash="h", final_url="https://example.com/post", changed=1,
        )
    # fixture 스냅샷(고정 과거 시각)이 현재 기간에 걸치는지는 실행 시점에 따라
    # 달라지므로, 라우트와 같은 경계 계산으로 기대값을 구한다
    starts = web_app._period_starts(now)
    fixture_times = ["2026-06-01T00:00:00+00:00", "2026-06-02T00:00:00+00:00"]
    expected_week = 1 + sum(1 for t in fixture_times if t >= starts["week"])
    expected_recent = 1 + sum(1 for t in fixture_times if t >= starts["recent"])

    res = client.get("/dashboard")
    assert res.status_code == 200
    assert 'id="stat-snapshots">3</div>' in res.text
    assert f'id="stat-week">{expected_week}</div>' in res.text
    assert f'id="stat-recent">{expected_recent}</div>' in res.text


def test_dashboard_total_bytes_sums_snapshot_files(client):
    # fixture 스냅샷 2개의 파일(content.md, page.html, screenshot.png) 합계
    expected = 0
    for dir_name in ("2026-06-01T00-00-00", "2026-06-02T00-00-00"):
        snap_dir = storage.page_dir(
            "example.com", storage.url_to_slug("https://example.com/post")
        ) / dir_name
        expected += sum(f["bytes"] for f in storage.snapshot_files(snap_dir))
    res = client.get("/dashboard")
    assert f'id="stat-bytes">{web_app.templates.env.filters["filesize"](expected)}</div>' in res.text


def test_rearchive_duplicate_not_requeued(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": calls.append(url),
    )
    web_app._register_job("https://example.com/post")
    res = client.post("/page/1/rearchive", follow_redirects=False)
    assert res.status_code == 303
    assert calls == []


def test_schedule_set_and_shown(client):
    res = client.post("/page/1/schedule", data={"interval": "3600"}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/page/1"

    timeline = client.get("/page/1")
    assert "자동 재아카이빙" in timeline.text
    assert "1시간" in timeline.text and "다음 실행" in timeline.text

    index = client.get("/archives")
    assert "1시간" in index.text  # 목록의 '자동' 컬럼

    with db.connect() as conn:
        sched = db.get_schedule(conn, 1)
    assert sched["interval_seconds"] == 3600


def test_schedule_set_custom_interval(client):
    """직접 입력 주기 — custom_value × 단위가 초로 변환되어 등록된다."""
    res = client.post(
        "/page/1/schedule",
        data={"interval": "custom", "custom_value": "2", "custom_unit": "h"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    with db.connect() as conn:
        sched = db.get_schedule(conn, 1)
    assert sched["interval_seconds"] == 2 * 3600

    # 90분도 직접 입력 가능
    client.post(
        "/page/1/schedule",
        data={"interval": "custom", "custom_value": "90", "custom_unit": "m"},
    )
    with db.connect() as conn:
        assert db.get_schedule(conn, 1)["interval_seconds"] == 5400

    # 프리셋에 없는 주기는 목록·타임라인에서 직접 입력으로 프리필된다
    for path in ("/schedules", "/page/1"):
        res = client.get(path)
        assert res.status_code == 200
        assert 'value="90"' in res.text  # custom_value 프리필
        assert "1시간 30분" in res.text  # 주기 라벨


def test_schedule_set_with_run_at(client):
    res = client.post(
        "/page/1/schedule",
        data={"interval": "86400", "run_at": "09:00"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    with db.connect() as conn:
        sched = db.get_schedule(conn, 1)
    assert sched["run_at_time"] == "09:00"

    res = client.get("/schedules")
    assert "1일 · 09:00" in res.text

    # 시간 단위 주기에 실행 시각은 400
    res = client.post(
        "/page/1/schedule", data={"interval": "3600", "run_at": "09:00"}
    )
    assert res.status_code == 400


def test_schedule_rejects_bad_custom_interval(client):
    # 범위 밖 (30분 < 최소 1시간)
    res = client.post(
        "/page/1/schedule",
        data={"interval": "custom", "custom_value": "30", "custom_unit": "m"},
    )
    assert res.status_code == 400
    # 숫자가 아닌 값·잘못된 단위
    res = client.post(
        "/page/1/schedule",
        data={"interval": "custom", "custom_value": "abc", "custom_unit": "h"},
    )
    assert res.status_code == 400
    res = client.post(
        "/page/1/schedule",
        data={"interval": "custom", "custom_value": "2", "custom_unit": "w"},
    )
    assert res.status_code == 400
    with db.connect() as conn:
        assert db.get_schedule(conn, 1) is None


def test_schedule_rejects_out_of_range_interval(client):
    assert client.post("/page/1/schedule", data={"interval": "60"}).status_code == 400
    assert (
        client.post("/page/1/schedule", data={"interval": str(31 * 86400)}).status_code
        == 400
    )
    with db.connect() as conn:
        assert db.get_schedule(conn, 1) is None


def test_schedule_accepts_one_month_interval(client):
    res = client.post(
        "/page/1/schedule", data={"interval": str(30 * 86400)}, follow_redirects=False
    )
    assert res.status_code == 303
    with db.connect() as conn:
        assert db.get_schedule(conn, 1)["interval_seconds"] == 30 * 86400
    assert "1개월" in client.get("/schedules").text


def test_schedule_delete(client):
    client.post("/page/1/schedule", data={"interval": "86400"})
    res = client.post("/page/1/schedule/delete", follow_redirects=False)
    assert res.status_code == 303
    with db.connect() as conn:
        assert db.get_schedule(conn, 1) is None


def test_schedule_unknown_page(client):
    assert client.post("/page/999/schedule", data={"interval": "3600"}).status_code == 404
    assert client.post("/page/999/schedule/delete").status_code == 404
    assert (
        client.post(
            "/page/999/schedule/next-run", data={"next_run": "2099-01-01T00:00"}
        ).status_code
        == 404
    )


def test_schedule_next_run_update(client):
    client.post("/page/1/schedule", data={"interval": "3600"})
    res = client.post(
        "/page/1/schedule/next-run",
        data={"next_run": "2099-01-02T03:04", "tz_offset": "0"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/page/1"
    with db.connect() as conn:
        sched = db.get_schedule(conn, 1)
    assert sched["next_run_at"] == "2099-01-02T03:04:00+00:00"
    assert sched["interval_seconds"] == 3600  # 주기는 그대로


def test_schedule_next_run_applies_tz_offset(client):
    """tz_offset(분)으로 브라우저 로컬 시각을 UTC 로 환산한다 (KST = -540)."""
    client.post("/page/1/schedule", data={"interval": "3600"})
    client.post(
        "/page/1/schedule/next-run",
        data={"next_run": "2099-01-02T12:00", "tz_offset": "-540"},
    )
    with db.connect() as conn:
        assert db.get_schedule(conn, 1)["next_run_at"] == "2099-01-02T03:00:00+00:00"


def test_schedule_next_run_errors(client):
    # 스케줄 미등록 → 400
    assert (
        client.post(
            "/page/1/schedule/next-run", data={"next_run": "2099-01-01T00:00"}
        ).status_code
        == 400
    )
    client.post("/page/1/schedule", data={"interval": "3600"})
    # 잘못된 시각 형식 → 400, 기존 값 유지
    with db.connect() as conn:
        before = db.get_schedule(conn, 1)["next_run_at"]
    assert (
        client.post(
            "/page/1/schedule/next-run", data={"next_run": "not-a-date"}
        ).status_code
        == 400
    )
    with db.connect() as conn:
        assert db.get_schedule(conn, 1)["next_run_at"] == before


def test_schedule_next_run_redirects_back_to_schedules(client):
    client.post("/page/1/schedule", data={"interval": "3600"})
    res = client.post(
        "/page/1/schedule/next-run",
        data={"next_run": "2099-01-01T00:00", "next": "/schedules"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/schedules"


def test_schedules_page_empty(client):
    res = client.get("/schedules")
    assert res.status_code == 200
    assert "등록된 자동 재아카이빙이 없습니다" in res.text


def test_schedules_page_lists_registered(client):
    client.post("/page/1/schedule", data={"interval": "86400"})
    res = client.get("/schedules")
    assert res.status_code == 200
    assert "/page/1" in res.text  # 타임라인 링크
    assert "1일" in res.text  # 주기 라벨
    assert "주기 변경" in res.text and "해제" in res.text


def test_schedule_set_redirects_back_to_schedules(client):
    res = client.post(
        "/page/1/schedule",
        data={"interval": "3600", "next": "/schedules"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/schedules"


def test_schedule_delete_redirects_back_to_schedules(client):
    client.post("/page/1/schedule", data={"interval": "3600"})
    res = client.post(
        "/page/1/schedule/delete", data={"next": "/schedules"}, follow_redirects=False
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/schedules"
    with db.connect() as conn:
        assert db.get_schedule(conn, 1) is None


def test_schedule_next_param_rejects_unknown_path(client):
    """next 는 알려진 경로만 허용 — 외부 URL 로의 열린 리다이렉트 방지."""
    res = client.post(
        "/page/1/schedule",
        data={"interval": "3600", "next": "https://evil.example/"},
        follow_redirects=False,
    )
    assert res.headers["location"] == "/page/1"


# ---- 압축 저장 형태 (gzip HTML · WebP 스크린샷 · 공유 자원) ----


def _first_snap_dir():
    return storage.page_dir(
        "example.com", storage.url_to_slug("https://example.com/post")
    ) / "2026-06-01T00-00-00"


def test_snapshot_file_serves_compressed_forms(client):
    from chunchugwan import resources

    resources.compact_snapshot_dir(_first_snap_dir())  # 스냅샷 1만 압축 형태로

    page = client.get("/snapshot/1/file/page.html")
    assert page.status_code == 200
    assert "본문" in page.text  # Content-Encoding: gzip — 클라이언트가 풀어준다
    assert page.headers["content-security-policy"] == "sandbox"

    shot = client.get("/snapshot/1/file/screenshot")
    assert shot.status_code == 200
    assert shot.headers["content-type"] == "image/webp"
    # 구형 별칭(.png)도 같은 논리 이름으로 해석된다
    assert client.get("/snapshot/1/file/screenshot.png").status_code == 200
    # 스냅샷 2는 구형(PNG) 그대로 서빙
    shot2 = client.get("/snapshot/2/file/screenshot")
    assert shot2.headers["content-type"] == "image/png"


def test_diff_works_across_webp_and_png(client):
    from chunchugwan import resources

    resources.compact_snapshot_dir(_first_snap_dir())  # WebP(신규) vs PNG(구형) 비교
    res = client.get("/diff/1")
    assert res.status_code == 200
    assert "변경 픽셀 100.00%" in res.text


def test_resource_route(client):
    import base64

    from chunchugwan import resources

    data = b"R" * 5000
    html = f'<img src="data:image/png;base64,{base64.b64encode(data).decode()}">'
    out, count = resources.externalize_data_uris(html)
    assert count == 1
    name = out.split("/resource/", 1)[1].split('"', 1)[0]

    res = client.get(f"/resource/{name}")
    assert res.status_code == 200
    assert res.content == data
    assert res.headers["content-type"] == "image/png"
    assert res.headers["content-security-policy"] == "sandbox"
    assert "immutable" in res.headers["cache-control"]

    assert client.get("/resource/..%2Findex.db").status_code == 404
    assert client.get(f"/resource/{'a' * 64}.html").status_code == 404  # 문서 타입 금지
    assert client.get(f"/resource/{'a' * 64}.png").status_code == 404   # 없는 자원
