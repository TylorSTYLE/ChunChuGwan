"""춘추관 간 네트워크 이전(마이그레이션) 테스트.

- 이전 모드 = 스크래핑·스케줄·크롤 전면 중단 게이트
- 소스 Pull 엔드포인트(/api/migration/*) 토큰 인증·경로 검증
- 받는 쪽 파일 단위 Pull 의 내결함성(재시도·실패 목록·부분 종료)·마무리
"""
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from chunchugwan import (
    archive_worker, auth, backup, config, crawler, db, migration, pipeline,
    scheduler, storage,
)
from chunchugwan.web import app as web_app

URL = "https://example.com/post"


def _patch_root(monkeypatch, root):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", root)
    monkeypatch.setattr(config, "SITES_DIR", root / "sites")
    monkeypatch.setattr(config, "DB_PATH", root / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", root / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", root / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", root / "documents")
    monkeypatch.setattr(config, "RULES_PATH", root / "rules.json")


@pytest.fixture(autouse=True)
def _reset_pull_state():
    """모듈 전역 이전 상태를 테스트 간 격리한다."""
    migration._pull_state.clear()
    migration._pull_state.update({"status": "idle"})
    migration._pull_thread = None
    yield


def _seed_page(url: str = URL) -> None:
    domain, slug = url.split("/")[2], storage.url_to_slug(url)
    dir_name = "2026-06-01T00-00-00"
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, url, domain, slug)
        snap_dir = storage.page_dir(domain, slug) / dir_name
        snap_dir.mkdir(parents=True)
        (snap_dir / "content.md").write_text("본문", encoding="utf-8")
        db.insert_snapshot(
            conn, page_id, taken_at="2026-06-01T00:00:00+00:00", dir_name=dir_name,
            content_hash=storage.content_sha256("본문"), final_url=url,
            http_status=200, changed=1,
        )


def _enable_migration(token: str = "tok") -> None:
    with db.connect() as conn:
        db.set_migration_mode(conn, True, auth.hash_token(token))


# ----------------------------------------------------------------------------
# 1) 스크래핑 게이트
# ----------------------------------------------------------------------------


def test_archive_worker_gated_by_migration_mode(tmp_path, monkeypatch):
    _patch_root(monkeypatch, tmp_path / "a")
    config.ensure_dirs()
    with db.connect() as conn:
        db.enqueue_archive_job(conn, URL, source="cli")

    calls = []
    monkeypatch.setattr(
        pipeline, "archive_url",
        lambda url, **kw: calls.append(url) or pipeline.ArchiveOutcome(
            status="new", url=url, content_hash="h", snapshot_dir=None,
            taken_at=None, last_taken_at=None, http_status=200, title=None,
        ),
    )

    _enable_migration()
    assert archive_worker.process_next() is None  # 게이트로 처리 안 됨
    assert calls == []
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM archive_jobs WHERE url = ?", (URL,)).fetchone()
    assert row["status"] == "pending"  # 작업은 클레임되지 않고 남는다

    # 이전 모드 끄면 정상 처리된다
    with db.connect() as conn:
        db.set_migration_mode(conn, False)
    step = archive_worker.process_next(archive_fn=pipeline.archive_url)
    assert step is not None and calls == [URL]


def test_scheduler_gated_by_migration_mode(tmp_path, monkeypatch):
    _patch_root(monkeypatch, tmp_path / "a")
    config.ensure_dirs()
    _seed_page()
    scheduler.set_schedule(URL, 3600)
    # 다음 실행 시각을 과거로 당겨 due 로 만든다
    from datetime import datetime, timezone
    scheduler.set_next_run(URL, datetime(2000, 1, 1, tzinfo=timezone.utc))

    calls = []
    monkeypatch.setattr(
        pipeline, "archive_url",
        lambda url, **kw: calls.append(url) or pipeline.ArchiveOutcome(
            status="unchanged", url=url, content_hash="h", snapshot_dir=None,
            taken_at=None, last_taken_at=None, http_status=200, title=None,
        ),
    )

    _enable_migration()
    assert scheduler.run_due() == []
    assert calls == []

    with db.connect() as conn:
        db.set_migration_mode(conn, False)
    assert scheduler.run_due() != []
    assert calls == [URL]


def test_crawl_schedules_gated_by_migration_mode(tmp_path, monkeypatch):
    _patch_root(monkeypatch, tmp_path / "a")
    config.ensure_dirs()
    _enable_migration()
    # due 여부와 무관하게 게이트가 빈 결과를 반환한다
    assert crawler.run_due_schedules() == []


def test_api_archive_blocked_in_migration_mode(tmp_path, monkeypatch):
    _patch_root(monkeypatch, tmp_path / "a")
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    config.ensure_dirs()
    _enable_migration()
    client = TestClient(web_app.app)
    res = client.post("/api/v1/archive", json={"url": URL})
    assert res.status_code == 409


# ----------------------------------------------------------------------------
# 2) 소스 Pull 엔드포인트
# ----------------------------------------------------------------------------


@pytest.fixture
def source_client(tmp_path, monkeypatch):
    """이전 모드 + 토큰이 설정된 소스 위의 TestClient (AUTH off)."""
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    _patch_root(monkeypatch, tmp_path / "src")
    config.ensure_dirs()
    _seed_page()
    _enable_migration("tok")
    return TestClient(web_app.app)


def test_migration_endpoints_require_token(source_client):
    # 토큰 없음 → 401
    assert source_client.get("/api/migration/info").status_code == 401
    # 틀린 토큰 → 401
    assert source_client.get(
        "/api/migration/info", headers={"X-Migration-Token": "bad"}
    ).status_code == 401
    # 올바른 토큰 → 200
    res = source_client.get("/api/migration/info", headers={"X-Migration-Token": "tok"})
    assert res.status_code == 200
    assert res.json()["format_version"] == backup.FORMAT_VERSION


def test_migration_endpoints_blocked_when_mode_off(source_client):
    with db.connect() as conn:
        db.set_migration_mode(conn, False)
    # 모드가 꺼지면 토큰이 맞아도(무효화됨) 401
    assert source_client.get(
        "/api/migration/info", headers={"X-Migration-Token": "tok"}
    ).status_code == 401


def test_migration_manifest_and_file(source_client):
    h = {"X-Migration-Token": "tok"}
    man = source_client.get("/api/migration/manifest", headers=h).json()
    assert man["db"]["sha256"]
    # 시드한 스냅샷의 content.md 가 파일 목록에 있다
    paths = [f["path"] for f in man["files"]]
    assert any(p.endswith("content.md") for p in paths)
    # 파일 다운로드
    target = next(p for p in paths if p.endswith("content.md"))
    res = source_client.get("/api/migration/file", params={"path": target}, headers=h)
    assert res.status_code == 200 and res.content == "본문".encode()
    # DB 스트리밍
    assert source_client.get("/api/migration/db", headers=h).status_code == 200


def test_migration_file_path_traversal_rejected(source_client):
    h = {"X-Migration-Token": "tok"}
    for bad in ("../index.db", "/etc/passwd", "index.db", "cache/x"):
        res = source_client.get("/api/migration/file", params={"path": bad}, headers=h)
        assert res.status_code == 400, bad


# ----------------------------------------------------------------------------
# 3) 받는 쪽 finalize
# ----------------------------------------------------------------------------


def test_finalize_migration_replaces_root_and_clears_mode(tmp_path, monkeypatch):
    import shutil

    # 소스 DB(관리자 1명 + 이전 모드 on)를 만든다
    _patch_root(monkeypatch, tmp_path / "src")
    config.ensure_dirs()
    with db.connect() as conn:
        db.create_first_admin(conn, "a@b.com", auth.hash_password("password123"))
        db.set_migration_mode(conn, True, auth.hash_token("tok"))
    src_db = tmp_path / "src-snapshot.db"
    backup._consistent_db_copy(src_db)

    # 받는 쪽 루트로 전환하고 스테이징을 구성 (DB + 파일 1개)
    _patch_root(monkeypatch, tmp_path / "dst")
    config.ensure_dirs()
    staging = migration.receiver_staging_dir()
    (staging / "sites" / "example.com").mkdir(parents=True)
    (staging / "sites" / "example.com" / "f.txt").write_text("x", encoding="utf-8")
    shutil.copy(src_db, staging / "index.db")

    backup.finalize_migration(staging)

    with db.connect() as conn:
        assert db.count_users(conn) == 1            # 소스 계정이 옮겨졌다
        assert db.migration_mode_enabled(conn) is False  # 받는 쪽은 이전 모드 꺼짐
    assert (config.SITES_DIR / "example.com" / "f.txt").is_file()
    assert not staging.exists()                     # 스테이징 정리됨


# ----------------------------------------------------------------------------
# 4) 받는 쪽 Pull 워커 — 내결함성(재시도·실패 목록·부분 종료)
# ----------------------------------------------------------------------------


def _wait_terminal(timeout=5.0):
    """이전 상태가 진행 중이 아닐 때까지 대기."""
    deadline = time.time() + timeout
    active = ("connecting", "manifest", "downloading", "restoring")
    while time.time() < deadline:
        if migration.pull_status()["status"] not in active:
            return migration.pull_status()
        time.sleep(0.02)
    raise AssertionError("이전 워커가 끝나지 않았습니다")


def _source_fixture(tmp_path):
    """소스 데이터(DB 바이트 + 파일 2개)와 매니페스트를 만든다."""
    root = tmp_path / "src"
    sites = root / "sites" / "example.com"
    sites.mkdir(parents=True)
    (sites / "ok.txt").write_text("ok-data", encoding="utf-8")
    (sites / "bad.txt").write_text("bad-data", encoding="utf-8")
    # 관리자 1명을 가진 sqlite DB
    db_bytes_path = root / "index.db"
    _make_admin_db(db_bytes_path)
    files = {
        "sites/example.com/ok.txt": b"ok-data",
        "sites/example.com/bad.txt": b"bad-data",
    }
    manifest = {
        "format_version": backup.FORMAT_VERSION,
        "db": {"bytes": db_bytes_path.stat().st_size,
               "sha256": migration._sha256_file(db_bytes_path)},
        "files": [{"path": p, "bytes": len(b), "sha256": None}
                  for p, b in files.items()],
    }
    return db_bytes_path.read_bytes(), files, manifest


def _make_admin_db(path):
    """주어진 경로에 관리자 1명을 가진 정상 sqlite DB 를 만든다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # 임시로 그 경로를 DB 로 써서 스키마+사용자를 만든다
    old = config.DB_PATH
    config.DB_PATH = path
    db.invalidate_schema_cache()
    try:
        with db.connect() as conn:
            db.create_first_admin(conn, "a@b.com", auth.hash_password("password123"))
    finally:
        config.DB_PATH = old
        db.invalidate_schema_cache()
    # WAL 모드에서 커밋 데이터는 WAL 파일에만 존재 — read_bytes() 로 main file 을
    # 취하기 전에 checkpoint 로 main file 에 반영한다.
    # (프로덕션 발신측은 conn.backup() 으로 WAL 포함 일관 복사본을 만드는데,
    #  이 헬퍼는 raw bytes 를 쓰므로 checkpoint 가 필수다.)
    raw = sqlite3.connect(str(path))
    try:
        raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        raw.close()


def _install_mock_client(monkeypatch, db_bytes, files, manifest, fail_paths=()):
    """migration._client 를 MockTransport 기반 httpx 클라이언트로 대체한다."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/migration/info":
            return httpx.Response(200, json={"format_version": backup.FORMAT_VERSION,
                                             "counts": {}})
        if path == "/api/migration/manifest":
            return httpx.Response(200, json=manifest)
        if path == "/api/migration/db":
            return httpx.Response(200, content=db_bytes)
        if path == "/api/migration/file":
            rel = request.url.params.get("path")
            if rel in fail_paths:
                return httpx.Response(500, text="boom")
            return httpx.Response(200, content=files[rel])
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        migration, "_client",
        lambda token: httpx.Client(transport=transport, follow_redirects=True),
    )
    # 재시도 대기를 0 으로 줄여 테스트를 빠르게
    monkeypatch.setattr(migration, "_RETRY_BACKOFF_SECONDS", (0, 0, 0))


def test_pull_success_finalizes(tmp_path, monkeypatch):
    db_bytes, files, manifest = _source_fixture(tmp_path)
    _patch_root(monkeypatch, tmp_path / "dst")
    config.ensure_dirs()
    _install_mock_client(monkeypatch, db_bytes, files, manifest)

    assert migration.start_pull("http://src", "tok") is None
    state = _wait_terminal()
    assert state["status"] == "done"
    with db.connect() as conn:
        assert db.count_users(conn) == 1
    assert (config.SITES_DIR / "example.com" / "ok.txt").read_text() == "ok-data"


def test_pull_partial_then_finish(tmp_path, monkeypatch):
    db_bytes, files, manifest = _source_fixture(tmp_path)
    _patch_root(monkeypatch, tmp_path / "dst")
    config.ensure_dirs()
    _install_mock_client(monkeypatch, db_bytes, files, manifest,
                         fail_paths={"sites/example.com/bad.txt"})

    migration.start_pull("http://src", "tok")
    state = _wait_terminal()
    assert state["status"] == "partial"
    assert [f["path"] for f in state["failed"]] == ["sites/example.com/bad.txt"]

    # 무시하고 종료 → 부분 복원으로 서비스 시작
    assert migration.finish_pull() is None
    state = _wait_terminal()
    assert state["status"] == "done"
    with db.connect() as conn:
        assert db.count_users(conn) == 1
    assert (config.SITES_DIR / "example.com" / "ok.txt").is_file()
    assert not (config.SITES_DIR / "example.com" / "bad.txt").exists()  # 빠진 파일


def test_pull_partial_then_retry_succeeds(tmp_path, monkeypatch):
    db_bytes, files, manifest = _source_fixture(tmp_path)
    _patch_root(monkeypatch, tmp_path / "dst")
    config.ensure_dirs()
    # 처음엔 bad.txt 실패
    state_box = {"fail": {"sites/example.com/bad.txt"}}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/migration/info":
            return httpx.Response(200, json={"format_version": backup.FORMAT_VERSION,
                                             "counts": {}})
        if path == "/api/migration/manifest":
            return httpx.Response(200, json=manifest)
        if path == "/api/migration/db":
            return httpx.Response(200, content=db_bytes)
        if path == "/api/migration/file":
            rel = request.url.params.get("path")
            if rel in state_box["fail"]:
                return httpx.Response(500)
            return httpx.Response(200, content=files[rel])
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(migration, "_client",
                        lambda token: httpx.Client(transport=transport))
    monkeypatch.setattr(migration, "_RETRY_BACKOFF_SECONDS", (0, 0, 0))

    migration.start_pull("http://src", "tok")
    assert _wait_terminal()["status"] == "partial"

    # 실패 원인을 제거하고 전체 재시도 → 마무리
    state_box["fail"] = set()
    assert migration.retry_failed() is None
    state = _wait_terminal()
    assert state["status"] == "done"
    assert (config.SITES_DIR / "example.com" / "bad.txt").read_text() == "bad-data"


def test_pull_db_failure_is_hard_error(tmp_path, monkeypatch):
    db_bytes, files, manifest = _source_fixture(tmp_path)
    _patch_root(monkeypatch, tmp_path / "dst")
    config.ensure_dirs()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/migration/info":
            return httpx.Response(200, json={"format_version": backup.FORMAT_VERSION,
                                             "counts": {}})
        if path == "/api/migration/manifest":
            return httpx.Response(200, json=manifest)
        if path == "/api/migration/db":
            return httpx.Response(500)  # DB 전송 실패
        return httpx.Response(200, content=b"x")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(migration, "_client",
                        lambda token: httpx.Client(transport=transport))
    monkeypatch.setattr(migration, "_RETRY_BACKOFF_SECONDS", (0, 0, 0))

    migration.start_pull("http://src", "tok")
    state = _wait_terminal()
    assert state["status"] == "error"  # DB 는 부분 허용 안 함
    with db.connect() as conn:
        assert db.count_users(conn) == 0  # 받는 쪽은 아직 비어 있다


def test_status_never_exposes_token(tmp_path, monkeypatch):
    db_bytes, files, manifest = _source_fixture(tmp_path)
    _patch_root(monkeypatch, tmp_path / "dst")
    config.ensure_dirs()
    _install_mock_client(monkeypatch, db_bytes, files, manifest)
    migration.start_pull("http://src", "supersecret")
    _wait_terminal()
    assert "token" not in migration.pull_status()
