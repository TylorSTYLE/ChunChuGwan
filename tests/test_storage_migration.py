"""로컬↔S3 blob 마이그레이션 엔진 — 매니페스트·검증·재시도·0실패 완료·일시중지·API.

실제 S3 없이 moto 로 검증한다. 로컬 기본 동작은 다른 테스트가 보장한다.
"""
import hashlib

import pytest
from fastapi.testclient import TestClient

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from chunchugwan import (  # noqa: E402
    auth, blobstore, config, db, scheduler, storage_migration,
)
from chunchugwan.web import app as web_app  # noqa: E402

BUCKET = "wccg-migrate-test"


def _cas(root, sub, content: bytes, ext: str) -> str:
    """CAS 파일(name=sha256+ext)을 root/sub 아래에 쓰고 상대 POSIX 경로 반환."""
    name = hashlib.sha256(content).hexdigest() + ext
    d = root / sub / name[:2]
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(content)
    return f"{sub}/{name[:2]}/{name}"


def _seed_local_blobs(root):
    """로컬 blob 트리(sites 스냅샷 + resource/document CAS)를 만든다."""
    sd = root / "sites" / "example.com" / "slug-abcdef12" / "2026-06-01T00-00-00"
    sd.mkdir(parents=True)
    (sd / "content.md").write_text("body text", encoding="utf-8")
    (sd / "meta.json").write_text('{"url":"https://example.com/"}', encoding="utf-8")
    res_rel = _cas(root, "resources", b"resource-bytes-1234", ".png")
    doc_rel = _cas(root, "documents", b"%PDF-1.4 document body", ".pdf")
    return {"res_rel": res_rel, "doc_rel": doc_rel}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """tmp 아카이브 루트/DB + 짧은 재시도 백오프 + 엔진 상태 격리."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "BLOB_CACHE_DIR", tmp_path / "blobcache")
    monkeypatch.setattr(config, "_blob_store", None)
    monkeypatch.setattr(storage_migration, "_RETRY_BACKOFF_SECONDS", (0, 0, 0))
    monkeypatch.setattr(storage_migration, "_state", {"status": "idle"})
    monkeypatch.setattr(storage_migration, "_thread", None)
    yield tmp_path
    monkeypatch.setattr(config, "_blob_store", None)


@pytest.fixture
def s3_env(env, monkeypatch):
    """env + moto S3 버킷 + S3 자격증명."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        monkeypatch.setattr(config, "S3_ENDPOINT_URL", "")
        monkeypatch.setattr(config, "S3_BUCKET", BUCKET)
        monkeypatch.setattr(config, "S3_REGION", "us-east-1")
        monkeypatch.setattr(config, "S3_ACCESS_KEY_ID", "k")
        monkeypatch.setattr(config, "S3_SECRET_ACCESS_KEY", "s")
        monkeypatch.setattr(config, "S3_FORCE_PATH_STYLE", True)
        monkeypatch.setattr(config, "S3_PREFIX", "")
        yield client, env


def _join(timeout=30):
    t = storage_migration._thread
    if t is not None:
        t.join(timeout)


def _s3_backend():
    return blobstore.S3BlobStore(**config.s3_settings())


# ---- 엔진 코어 단위 ----


def test_build_manifest_lists_all_blobs(env):
    _seed_local_blobs(env)
    manifest = storage_migration.build_manifest(blobstore.LocalBlobStore())
    paths = {e["path"] for e in manifest}
    assert any(p.endswith("content.md") for p in paths)
    assert any(p.endswith("meta.json") for p in paths)
    assert any(p.startswith("resources/") for p in paths)
    assert any(p.startswith("documents/") for p in paths)
    # CAS 항목은 sha256(=파일명)을 미리 채운다
    cas = next(e for e in manifest if e["path"].startswith("resources/"))
    assert cas["sha256"] and len(cas["sha256"]) == 64


def test_copy_file_idempotent_skip(env):
    """대상에 이미 존재+크기 일치면 read 없이 스킵 (멱등)."""
    local = blobstore.LocalBlobStore()
    rel = _cas(env, "resources", b"abc", ".png")
    path = env / rel
    entry = {"path": rel, "size": 3, "sha256": path.stem}
    target = blobstore.LocalBlobStore()
    # 대상(여기선 같은 로컬 경로) 이미 존재 → read_bytes 호출되면 실패해야 함
    local.read_bytes = lambda *a, **k: (_ for _ in ()).throw(AssertionError("read 금지"))
    assert storage_migration._copy_file(local, target, path, entry) is None


def test_copy_file_retries_three_times(env, monkeypatch):
    """일시적 실패는 파일당 최대 3회 재시도로 흡수된다."""
    local = blobstore.LocalBlobStore()
    rel = _cas(env, "documents", b"%PDF-x", ".pdf")
    path = env / rel
    entry = {"path": rel, "size": path.stat().st_size, "sha256": path.stem}

    class _Flaky:
        def __init__(self):
            self.calls = 0

        def is_file(self, p):
            return False  # 항상 미존재로 봐서 매번 쓰기를 시도하게

        def size(self, p):
            return entry["size"]

        def put_verified(self, p, data, sha):
            self.calls += 1
            if self.calls < 3:
                raise OSError("일시 오류")

    flaky = _Flaky()
    # 3회째 성공 후 검증: is_file/size 가 True/일치를 내도록 교체
    real_calls = {"n": 0}

    def is_file(p):
        real_calls["n"] += 1
        return flaky.calls >= 3
    flaky.is_file = is_file
    assert storage_migration._copy_file(local, flaky, path, entry) is None
    assert flaky.calls == 3


def test_copy_file_permanent_failure_returns_error(env):
    local = blobstore.LocalBlobStore()
    rel = _cas(env, "documents", b"%PDF-y", ".pdf")
    path = env / rel
    entry = {"path": rel, "size": path.stat().st_size, "sha256": path.stem}

    class _Down:
        def is_file(self, p):
            return False

        def size(self, p):
            return 0

        def put_verified(self, p, data, sha):
            raise OSError("계속 실패")

    err = storage_migration._copy_file(local, _Down(), path, entry)
    assert err is not None and "OSError" in err


# ---- 통합: 양방향 마이그레이션 ----


def test_migrate_local_to_s3_completes_and_flips(s3_env):
    client, root = s3_env
    seeded = _seed_local_blobs(root)
    # 활성 백엔드 기본 'local'
    assert storage_migration.start_migration() is None
    _join()
    st = storage_migration.status()
    assert st["status"] == "done"
    with db.connect() as conn:
        assert db.storage_backend(conn) == "s3"           # 활성 전환
        assert db.writes_paused(conn) is False            # 일시중지 해제
        summary = db.storage_migration_summary(conn)
    assert summary["cleanup_pending"] is True
    assert summary["source_backend"] == "local"
    assert summary["target_backend"] == "s3"
    assert summary["source_location"] == str(root)        # 원본 위치 기록
    # 대상(S3)에 실제로 올라갔는지 + 내용 일치
    target = _s3_backend()
    assert target.is_file(root / seeded["res_rel"])
    assert target.read_bytes(root / seeded["res_rel"]) == b"resource-bytes-1234"
    assert target.read_bytes(root / "sites" / "example.com" / "slug-abcdef12"
                             / "2026-06-01T00-00-00" / "content.md") == b"body text"


def test_migrate_s3_to_local_completes_and_flips(s3_env):
    client, root = s3_env
    seeded = _seed_local_blobs(root)
    # 로컬 blob 을 S3 로 올리고 backend='s3' 로 둔 뒤 로컬 트리 삭제 → s3→local 검증
    import shutil
    for sub in ("sites", "resources", "documents"):
        for f in (root / sub).rglob("*"):
            if f.is_file():
                client.upload_file(str(f), BUCKET, f.relative_to(root).as_posix())
    with db.connect() as conn:
        db.set_storage_backend(conn, "s3")
    for sub in ("sites", "resources", "documents"):
        shutil.rmtree(root / sub)

    assert storage_migration.start_migration() is None
    _join()
    assert storage_migration.status()["status"] == "done"
    with db.connect() as conn:
        assert db.storage_backend(conn) == "local"
        summary = db.storage_migration_summary(conn)
    assert summary["source_backend"] == "s3"
    assert summary["target_backend"] == "local"
    assert summary["source_location"].startswith("s3://")
    # 로컬 디스크에 복원됐는지 + 내용 일치
    assert (root / seeded["res_rel"]).read_bytes() == b"resource-bytes-1234"
    assert (root / seeded["doc_rel"]).read_bytes() == b"%PDF-1.4 document body"


def test_corruption_stays_partial_no_flip_no_unpause(s3_env):
    """CAS 손상(내용≠이름) 파일은 재시도 후에도 실패 → partial, 전환·해제 금지."""
    client, root = s3_env
    _seed_local_blobs(root)
    # 이름은 'aaa...a.png' 인데 내용은 그 sha256 이 아님 → 매번 sha 불일치
    bad_dir = root / "resources" / "aa"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / ("a" * 64 + ".png")).write_bytes(b"content-not-matching-name")

    assert storage_migration.start_migration() is None
    _join()
    st = storage_migration.status()
    assert st["status"] == "partial"
    assert any(f["path"].endswith("a" * 64 + ".png") for f in st["failed"])
    with db.connect() as conn:
        assert db.storage_backend(conn) == "local"        # 전환 안 됨
        assert db.writes_paused(conn) is True             # 일시중지 유지
        assert db.storage_migration_summary(conn)["status"] == "partial"
    # 라이브 실패 카운트 — 손상 파일 1개가 failed_count 에 반영된다
    assert st["failed_count"] >= 1
    assert st["workers"] >= 1  # 동시 전송 워커 수 노출


def test_parallel_transfer_completes(s3_env, monkeypatch):
    """동시 전송 워커(>1)로 여러 파일을 옮겨도 0실패로 완료된다."""
    monkeypatch.setattr(config, "S3_MIGRATION_WORKERS", 8)
    client, root = s3_env
    # 여러 자원 파일 시드 (병렬 경로 검증)
    for i in range(12):
        _cas(root, "resources", f"parallel-{i}".encode(), ".png")
    _seed_local_blobs(root)
    assert storage_migration.start_migration() is None
    _join()
    st = storage_migration.status()
    assert st["status"] == "done"
    assert st["failed_count"] == 0
    assert st["workers"] == 8
    with db.connect() as conn:
        assert db.storage_backend(conn) == "s3"


def test_partial_then_retry_completes(s3_env, monkeypatch):
    """일시 실패로 partial → 원인 해소 후 retry → 0실패 완료·전환."""
    client, root = s3_env
    _seed_local_blobs(root)
    fail = {"on": True}
    real_make = storage_migration._make_backend

    def flaky_make(name):
        b = real_make(name)
        if name == "s3":
            orig = b.put_verified

            def pv(path, data, sha):
                if path.name == "content.md" and fail["on"]:
                    raise OSError("대상 일시 장애")
                return orig(path, data, sha)
            b.put_verified = pv
        return b
    monkeypatch.setattr(storage_migration, "_make_backend", flaky_make)

    assert storage_migration.start_migration() is None
    _join()
    assert storage_migration.status()["status"] == "partial"
    with db.connect() as conn:
        assert db.storage_backend(conn) == "local"
        assert db.writes_paused(conn) is True

    fail["on"] = False  # 장애 해소
    assert storage_migration.retry_failed() is None
    _join()
    assert storage_migration.status()["status"] == "done"
    with db.connect() as conn:
        assert db.storage_backend(conn) == "s3"
        assert db.writes_paused(conn) is False


def test_idempotent_rerun_skips_existing(s3_env):
    """완료 후 같은 방향으로 다시 돌려도(원본 보존) 0실패로 다시 완료된다."""
    client, root = s3_env
    _seed_local_blobs(root)
    assert storage_migration.start_migration() is None
    _join()
    assert storage_migration.status()["status"] == "done"
    # backend 가 's3' 로 바뀌었으니 local 로 되돌려 같은 local→s3 를 재실행
    with db.connect() as conn:
        db.set_storage_backend(conn, "local")
    config.reset_blob_store()
    monkey_state = {"status": "idle"}
    storage_migration._state.clear()
    storage_migration._state.update(monkey_state)
    assert storage_migration.start_migration() is None
    _join()
    assert storage_migration.status()["status"] == "done"  # 멱등: 다시 0실패 완료


def test_confirm_cleanup_clears_flag(s3_env):
    client, root = s3_env
    _seed_local_blobs(root)
    storage_migration.start_migration()
    _join()
    with db.connect() as conn:
        assert db.storage_migration_summary(conn)["cleanup_pending"] is True
    assert storage_migration.confirm_cleanup() is None
    with db.connect() as conn:
        assert db.storage_migration_summary(conn)["cleanup_pending"] is False
    # 더 이상 정리 대기 없음
    assert storage_migration.confirm_cleanup() is not None


# ---- 일시중지 일반화 ----


def test_writes_paused_generalized(env):
    with db.connect() as conn:
        assert db.writes_paused(conn) is False
        db.set_storage_migration_active(conn, True)
        assert db.writes_paused(conn) is True            # 스토리지 마이그레이션
        db.set_storage_migration_active(conn, False)
        db.set_migration_mode(conn, True, token_hash="x")
        assert db.writes_paused(conn) is True            # 인스턴스 이전
        db.set_migration_mode(conn, False)
        assert db.writes_paused(conn) is False


def test_scheduler_gate_short_circuits_when_paused(env, monkeypatch):
    """스토리지 마이그레이션 진행 중이면 scheduler.run_due 가 즉시 no-op."""
    with db.connect() as conn:
        db.set_storage_migration_active(conn, True)

    def _boom(*a, **k):
        raise AssertionError("일시중지 중에는 스케줄을 조회하면 안 된다")
    monkeypatch.setattr(db, "list_due_schedules", _boom)
    assert scheduler.run_due() == []  # 게이트가 list_due_schedules 전에 차단


# ---- API ----


def _admin_client(env):
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(conn, "v@test.co", auth.hash_password("password1234"), role="viewer")
    web_app._active_jobs.clear()
    client = TestClient(web_app.app, headers={"X-Requested-With": "fetch"})
    return client


def test_api_storage_status_admin(env):
    client = _admin_client(env)
    client.post("/api/web/auth/login",
                json={"email": "boss@test.co", "password": "bosspass1234"})
    r = client.get("/api/web/system/storage/status")
    assert r.status_code == 200
    assert r.json()["active_backend"] == "local"


def test_api_storage_status_forbidden_for_non_admin(env):
    client = _admin_client(env)
    client.post("/api/web/auth/login",
                json={"email": "v@test.co", "password": "password1234"})
    assert client.get("/api/web/system/storage/status").status_code == 403


def test_api_storage_migrate_start_incomplete_env_409(env, monkeypatch):
    """backend=local 인데 S3 env 불완전이면 start 가 409 (target=s3 검증 실패)."""
    monkeypatch.setattr(config, "S3_BUCKET", "")
    monkeypatch.setattr(config, "S3_ACCESS_KEY_ID", "")
    monkeypatch.setattr(config, "S3_SECRET_ACCESS_KEY", "")
    client = _admin_client(env)
    client.post("/api/web/auth/login",
                json={"email": "boss@test.co", "password": "bosspass1234"})
    r = client.post("/api/web/system/storage/migrate/start")
    assert r.status_code == 409
