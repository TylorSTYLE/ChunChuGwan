"""대시보드 라우트 테스트. 캡처 없이 fixture 데이터로 검증."""
import hashlib
import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from chunchugwan import archive_worker, config, crawler, db, documents, storage
from chunchugwan.web import app as web_app

GUIDE_BODY = b"%PDF-1.4 cas fixture"
GUIDE_SHA = hashlib.sha256(GUIDE_BODY).hexdigest()


class _Outcome:
    """process_next 가 읽는 최소 ArchiveOutcome 대체 (status 만 필요)."""

    status = "new"
    snapshot_id = 1
    page_links: list = []


def _stub_capture(monkeypatch, fake):
    """단발 아카이빙은 이제 archive_worker 가 큐를 소비해 실행한다 — 그 캡처
    함수(pipeline.archive_url)를 fake 로 교체한다. POST 후 _drain_archive_jobs()
    로 큐를 비워야 fake 가 호출된다."""
    monkeypatch.setattr(archive_worker.pipeline, "archive_url", fake)


def _drain_archive_jobs():
    """큐에 쌓인 단발 아카이빙 작업을 동기로 모두 처리 (테스트 전용)."""
    while archive_worker.process_next() is not None:
        pass


def _archive_jobs():
    """현재 큐에 있는 단발 아카이빙 작업 행 목록 (검증용)."""
    with db.connect() as conn:
        return conn.execute(
            "SELECT url, force, source, interval_seconds FROM archive_jobs ORDER BY id"
        ).fetchall()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """임시 아카이브(페이지 1개 + 스냅샷 2개 + check 1개) 위의 TestClient."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
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
    # — report 는 구형(files/), guide 는 신형(문서 CAS + snapshot_documents 행)
    snap1_dir = storage.page_dir(domain, slug) / dir_names[0]
    (snap1_dir / "files").mkdir()
    (snap1_dir / "files" / "report-12345678.pdf").write_bytes(b"%PDF-1.4 fixture")
    (snap1_dir / "files" / "unlisted.pdf").write_bytes(b"%PDF-1.4 manifest-bayuk")
    cas_file = documents.cas_path(GUIDE_SHA + ".pdf")
    cas_file.parent.mkdir(parents=True)
    cas_file.write_bytes(GUIDE_BODY)
    guide_entry = {
        "url": "https://example.com/files/guide.pdf",
        "file": "guide-aabbccdd.pdf", "bytes": len(GUIDE_BODY),
        "sha256": GUIDE_SHA, "content_type": "application/pdf",
    }
    with db.connect() as conn:
        db.insert_snapshot_documents(conn, 1, [guide_entry])
    storage.write_meta(snap1_dir, storage.SnapshotMeta(
        url=url, final_url=url, taken_at="2026-06-01T00:00:00+00:00",
        content_hash=storage.content_sha256(contents[0]), http_status=200,
        title="픽스처 글", documents=[{
            "url": "https://example.com/files/report.pdf",
            "file": "report-12345678.pdf", "bytes": 16,
            "sha256": "ab" * 32, "content_type": "application/pdf",
        }, guide_entry],
    ))

    # 픽스처는 finalize_snapshot 을 거치지 않고 파일을 두 단계로 쓰므로(스냅샷 행
    # 삽입 후 files/·meta.json 추가), 실제 파일·meta 기준으로 snapshots.bytes·title
    # 을 맞춘다 — production 의 캡처(저장 시점 기록)·import(파일 이동 후 백필)와 같은 결과.
    with db.connect() as conn:
        db.backfill_snapshot_bytes(conn)
        db.backfill_snapshot_titles(conn)

    web_app._active_jobs.clear()  # 다른 테스트의 진행 목록 잔재 제거
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def _fixture_total_bytes() -> int:
    """fixture 스냅샷 전체의 파일 용량 합 — 사이트 용량 표시 기대값."""
    with db.connect() as conn:
        rows = db.list_snapshot_dirs(conn)
    return sum(
        f["bytes"]
        for r in rows
        for f in storage.snapshot_files(
            storage.page_dir(r["domain"], r["slug"]) / r["dir_name"]
        )
    )


def _insert_log(status: str, *, started_at: str, error: str | None = None) -> int:
    """fixture 페이지(1)의 아카이브 로그 한 행 삽입 (실패 목록 테스트용)."""
    with db.connect() as conn:
        return db.insert_archive_log(
            conn, url="https://example.com/post", domain="example.com",
            page_id=1, source="web", status=status,
            started_at=started_at, duration_ms=100, error=error,
        )


def test_site_failed_jobs_cleared_after_success(client):
    """실패 이후 성공 실행이 생기면 (재시도 성공) 실패 목록에서 사라진다."""
    _insert_log("error", started_at="2026-06-03T00:00:00+00:00", error="boom")
    _insert_log("changed", started_at="2026-06-04T00:00:00+00:00")
    with db.connect() as conn:
        site = db.get_site_by_key(conn, "example.com")
    data = client.get(f"/api/web/sites/{site['id']}").json()
    assert data["failed_items"] == []


def _insert_failed_crawl_page(
    url: str, *, error: str = "CrawlError: 캡처 실패"
) -> tuple[int, int]:
    """example.com 크롤 1개 + failed 크롤 페이지 1개 삽입. (crawl_id, cp_id) 반환."""
    with db.connect() as conn:
        crawl_id = db.insert_crawl(
            conn, start_url="https://example.com/", scope_host="example.com",
            scope_path="/", max_pages=10, max_depth=2, delay_seconds=0, source="web",
        )
        db.insert_crawl_page(conn, crawl_id, url, 1)
        cp_id = conn.execute(
            "SELECT id FROM crawl_pages WHERE crawl_id = ? AND url = ?",
            (crawl_id, url),
        ).fetchone()["id"]
        db.fail_crawl_page(conn, cp_id, attempts=3, error=error, next_attempt_at=None)
        db.finish_crawl_if_done(conn, crawl_id)
    return crawl_id, cp_id


def test_site_failed_crawl_page_cleared_after_later_crawl_success(client):
    """이후 크롤에서 같은 URL 이 성공하면 (URL 별 최신 행) 목록에서 사라진다."""
    _insert_failed_crawl_page("https://example.com/broken")
    with db.connect() as conn:
        crawl2 = db.insert_crawl(
            conn, start_url="https://example.com/", scope_host="example.com",
            scope_path="/", max_pages=10, max_depth=2, delay_seconds=0, source="web",
        )
        db.insert_crawl_page(conn, crawl2, "https://example.com/broken", 1)
        cp2 = conn.execute(
            "SELECT id FROM crawl_pages WHERE crawl_id = ? AND url = ?",
            (crawl2, "https://example.com/broken"),
        ).fetchone()["id"]
        db.finish_crawl_page(conn, cp2, None)
        site = db.get_site_by_key(conn, "example.com")
    data = client.get(f"/api/web/sites/{site['id']}").json()
    urls = [item["url"] for item in data["failed_items"]]
    assert "https://example.com/broken" not in urls


def test_site_failed_crawl_page_cleared_after_direct_archive(client):
    """크롤 실패 후 직접 아카이빙이 성공한 URL(최신 로그가 성공)은 제외된다."""
    _insert_failed_crawl_page("https://example.com/broken")
    with db.connect() as conn:
        db.insert_archive_log(
            conn, url="https://example.com/broken", domain="example.com",
            source="web", status="changed",
            started_at="2026-06-05T00:00:00+00:00", duration_ms=100,
        )
        site = db.get_site_by_key(conn, "example.com")
    data = client.get(f"/api/web/sites/{site['id']}").json()
    urls = [item["url"] for item in data["failed_items"]]
    assert "https://example.com/broken" not in urls


def _seed_failed_pages(n: int) -> None:
    """서로 다른 URL 의 실패 로그 n개 삽입 (실패 목록 페이징 테스트용)."""
    with db.connect() as conn:
        for i in range(n):
            u = f"https://example.com/fail-{i:02d}"
            pid = db.get_or_create_page(conn, u, "example.com", storage.url_to_slug(u))
            db.insert_archive_log(
                conn, url=u, domain="example.com", page_id=pid, source="web",
                status="error", started_at=f"2026-06-03T00:00:{i:02d}+00:00",
                duration_ms=100, error=f"boom-{i}",
            )


def _insert_done_crawl(start_url: str = "https://example.com/docs/") -> int:
    """유효 옵션의 완료된 크롤 1개 삽입 (다시 아카이빙 테스트용)."""
    with db.connect() as conn:
        crawl_id = db.insert_crawl(
            conn, start_url=start_url, scope_host="example.com",
            scope_path="/docs/", max_pages=10, max_depth=2, delay_seconds=10,
            source="web",
        )
        conn.execute("UPDATE crawls SET status = 'done' WHERE id = ?", (crawl_id,))
    return crawl_id


def test_snapshot_document_download(client):
    res = client.get("/snapshot/1/doc/report-12345678.pdf")
    assert res.status_code == 200
    assert res.content == b"%PDF-1.4 fixture"
    # 브라우저 안에서 렌더링되지 않도록 항상 첨부파일 다운로드
    assert res.headers["content-type"].startswith("application/octet-stream")
    assert "attachment" in res.headers["content-disposition"]
    assert res.headers["content-security-policy"] == "sandbox"


def test_snapshot_document_served_from_cas(client):
    """files/ 에 없는 신형 문서는 snapshot_documents 행의 해시로 CAS 에서 서빙."""
    res = client.get("/snapshot/1/doc/guide-aabbccdd.pdf")
    assert res.status_code == 200
    assert res.content == GUIDE_BODY
    assert res.headers["content-type"].startswith("application/octet-stream")
    assert "attachment" in res.headers["content-disposition"]
    assert res.headers["content-security-policy"] == "sandbox"


def test_shotdiff_image(client):
    res = client.get("/diff/1/shotdiff?from=1&to=2")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"


def test_archive_without_interval_no_schedule(client, monkeypatch):
    _stub_capture(monkeypatch, lambda url, **kw: _Outcome())
    client.post(
        "/archive", data={"url": "https://example.com/post"}, follow_redirects=False
    )
    _drain_archive_jobs()
    with db.connect() as conn:
        assert db.get_schedule(conn, 1) is None


def _enter_needs_human(url: str) -> int:
    """needs_human(라이브 진입) 상태의 작업 1건을 만든다 — 테스트용."""
    with db.connect() as conn:
        db.enqueue_archive_job(conn, url, source="web")
        job = db.claim_due_archive_job(conn, "2099-01-01T00:00:00+00:00")
        db.mark_needs_human(conn, job["id"], token="tok", viewport_w=1280, viewport_h=800)
    return job["id"]


# needs_human 표시는 워커가 DB 에 기록하는 사실에만 의존한다 — serve 프로세스의
# WCCG_LIVE_CHALLENGE 설정과 무관하다 (워커·serve env 가 달라도 누락 안 되게).
# 아래 테스트는 모두 serve LIVE_CHALLENGE 를 켜지 않은 기본 off 상태로 검증한다.


def test_header_and_banner_absent_when_no_jobs(client):
    # 대기 0건이면 헤더 '사람 확인' 메뉴도 전역 배너도 렌더하지 않는다
    html = client.get("/").text
    assert "/archive/needs-human" not in html
    assert 'id="needs-human-banner"' not in html


def test_period_starts_boundaries():
    now = datetime(2026, 6, 11, 15, 30, 45, tzinfo=timezone.utc)  # 목요일
    starts = web_app._period_starts(now)
    assert starts["today"] == "2026-06-11T00:00:00+00:00"
    assert starts["week"] == "2026-06-08T00:00:00+00:00"  # 월요일 자정
    assert starts["month"] == "2026-06-01T00:00:00+00:00"
    assert starts["year"] == "2026-01-01T00:00:00+00:00"
    assert starts["recent"] == "2026-06-10T15:30:45+00:00"


def test_schedule_next_run_uses_user_timezone():
    """사용자 타임존(Asia/Seoul)으로 로컬 시각을 UTC 로 환산한다."""
    import zoneinfo
    from datetime import datetime, timezone as _utc

    tz = zoneinfo.ZoneInfo("Asia/Seoul")
    dt_local = datetime(2099, 1, 2, 12, 0)
    dt_utc = dt_local.replace(tzinfo=tz).astimezone(_utc.utc)
    assert dt_utc.isoformat() == "2099-01-02T03:00:00+00:00"


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
    assert page.headers["content-security-policy"] == (
        "sandbox allow-top-navigation-by-user-activation"
    )

    shot = client.get("/snapshot/1/file/screenshot")
    assert shot.status_code == 200
    assert shot.headers["content-type"] == "image/webp"
    # 구형 별칭(.png)도 같은 논리 이름으로 해석된다
    assert client.get("/snapshot/1/file/screenshot.png").status_code == 200
    # 스냅샷 2는 구형(PNG) 그대로 서빙
    shot2 = client.get("/snapshot/2/file/screenshot")
    assert shot2.headers["content-type"] == "image/png"


def test_resource_route_serves_gzipped_css(client, monkeypatch):
    from chunchugwan import config, resources

    monkeypatch.setattr(config, "RESOURCE_MIN_BYTES", 16)
    css = "body { color: #abc; margin: 0; }"
    out, names = resources.externalize_style_blocks(f"<style>{css}</style>")
    assert len(names) == 1

    # gzip 저장된 CSS 는 Content-Encoding 으로 서빙 (httpx 가 투명 해제)
    res = client.get(f"/resource/{names[0]}")
    assert res.status_code == 200
    assert res.text == css
    assert res.headers.get("content-encoding") == "gzip"
    assert res.headers["content-type"].startswith("text/css")
    assert res.headers["content-security-policy"] == "sandbox"


# ── 링크 리졸버: /goto · /crawl/{id}/goto 가 정식 중첩 경로로 보내는지 ──
# (C2 컷오버 회귀 방지 — 구형 /snapshot/{id} 로 가면 SPA 가 못 그려 깨졌다.)

_CANON_RE = re.compile(r"^/archive/sites/\d+/page/1/snapshot/2$")


def test_goto_redirects_to_canonical_snapshot(client):
    """단일 페이지 리졸버 — URL 의 최신 스냅샷(=2) 정식 경로로 302."""
    res = client.get(
        "/goto", params={"url": "https://example.com/post"},
        follow_redirects=False,
    )
    assert res.status_code == 302
    assert _CANON_RE.match(res.headers["location"]), res.headers["location"]


def test_goto_archive_miss_returns_guidance(client):
    """아카이브에 없는 링크 — 라이브로 안 새고 안내 화면(스크립트 없음) 404."""
    res = client.get(
        "/goto", params={"url": "https://nope.example/x"},
        follow_redirects=False,
    )
    assert res.status_code == 404
    assert "<script" not in res.text.lower()
    assert "nope.example" in res.text  # 원본 링크 안내


def test_crawl_goto_redirects_to_canonical_snapshot(client):
    """크롤 리졸버도 정식 중첩 경로로 302 (구형 /snapshot/{id} 아님)."""
    with db.connect() as conn:
        crawl_id = db.insert_crawl(
            conn, start_url="https://example.com/", scope_host="example.com",
            scope_path="/", max_pages=10, max_depth=2, delay_seconds=0, source="web",
        )
    res = client.get(
        f"/crawl/{crawl_id}/goto", params={"url": "https://example.com/post"},
        follow_redirects=False,
    )
    assert res.status_code == 302
    assert _CANON_RE.match(res.headers["location"]), res.headers["location"]


