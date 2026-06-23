"""첫 구동 분류 + S3 DB백업 복원 + 복구모드 재구축 + 복구-선택 (P5a).

S3 경로는 moto 로 검증. 복구는 blob 에서 인덱스를 재구성하며, 모든 복구 스냅샷은
authenticated=1(관리자 전용)이어야 한다(보안 — 전수 검증).
"""
import gzip
import json

import pytest
from fastapi.testclient import TestClient

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from chunchugwan import (  # noqa: E402
    auth, config, db, db_backup, recovery, storage,
)
from chunchugwan.web import app as web_app  # noqa: E402

BUCKET = "wccg-recovery-test"
RES_NAME = "a" * 64 + ".png"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(config, "BLOB_CACHE_DIR", tmp_path / "blobcache")
    monkeypatch.setattr(config, "_blob_store", None)
    # S3 env 비움 (로컬 기본) — s3 테스트는 별도로 채운다
    for attr in ("S3_ENDPOINT_URL", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_PREFIX"):
        monkeypatch.setattr(config, attr, "")
    monkeypatch.setattr(recovery, "_state", {"status": "idle"})
    monkeypatch.setattr(recovery, "_thread", None)
    monkeypatch.setattr(db_backup, "_running", False)
    with db.connect():
        pass  # 스키마
    yield tmp_path
    monkeypatch.setattr(config, "_blob_store", None)


def _set_s3(monkeypatch, prefix=""):
    monkeypatch.setattr(config, "S3_BUCKET", BUCKET)
    monkeypatch.setattr(config, "S3_REGION", "us-east-1")
    monkeypatch.setattr(config, "S3_ACCESS_KEY_ID", "k")
    monkeypatch.setattr(config, "S3_SECRET_ACCESS_KEY", "s")
    monkeypatch.setattr(config, "S3_FORCE_PATH_STYLE", True)
    monkeypatch.setattr(config, "S3_PREFIX", prefix)


def _make_snapshot(root, url, i, content_hash, *, docs=None, resource=False):
    """sites/{domain}/{slug}/{ts}/ 에 meta.json + page.html.gz 를 만든다."""
    domain = "example.com"
    slug = storage.url_to_slug(url)
    ts = f"2026-06-0{i}T00-00-00"
    d = root / "sites" / domain / slug / ts
    d.mkdir(parents=True)
    meta = {
        "url": url, "final_url": url,
        "taken_at": f"2026-06-0{i}T00:00:00+00:00",
        "content_hash": content_hash, "http_status": 200, "title": "T",
        "documents": docs or [], "origin": "server", "incomplete": False,
    }
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "content.md").write_text("body", encoding="utf-8")
    html = f'<img src="/resource/{RES_NAME}">' if resource else "<html>x</html>"
    (d / "page.html.gz").write_bytes(gzip.compress(html.encode("utf-8")))
    return d


def _join(timeout=30):
    t = recovery._thread
    if t is not None:
        t.join(timeout)


# ---- 1. 첫 구동 분류 (6 케이스) ----


def test_classify_operating_when_users_exist(env):
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
    assert recovery.classify()["case"] == "operating"


def test_classify_data_preserved(env):
    # 사용자 0명 + 스냅샷 데이터 있음
    url = "https://example.com/x"
    with db.connect() as conn:
        pid = db.get_or_create_page(conn, url, "example.com", storage.url_to_slug(url))
        db.insert_snapshot(conn, pid, taken_at="2026-06-01T00:00:00+00:00",
                           dir_name="2026-06-01T00-00-00", content_hash="h",
                           final_url=url, http_status=200, changed=1)
    assert recovery.classify()["case"] == "data_preserved"


def test_classify_recover_local(env):
    _make_snapshot(env, "https://example.com/p", 1, "h1")
    assert recovery.classify()["case"] == "recover_local"


def test_classify_fresh_when_no_blob(env):
    assert recovery.classify()["case"] == "fresh"


def test_classify_restore_s3_when_backup_available(env, monkeypatch):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        _set_s3(monkeypatch)
        # S3 에 blob(sites) + db-backup 둘 다 존재
        _make_snapshot(env, "https://example.com/p", 1, "h1")
        for f in (env / "sites").rglob("*"):
            if f.is_file():
                client.upload_file(str(f), BUCKET, f.relative_to(env).as_posix())
        import shutil
        shutil.rmtree(env / "sites")  # 로컬 blob 제거 → s3 만
        with db.connect() as conn:
            db.set_storage_backend(conn, "s3")
        db_backup.run_once()  # db-backup 생성
        assert recovery.classify()["case"] == "restore_s3"


def test_classify_recover_s3_when_no_backup(env, monkeypatch):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        _set_s3(monkeypatch)
        _make_snapshot(env, "https://example.com/p", 1, "h1")
        for f in (env / "sites").rglob("*"):
            if f.is_file():
                client.upload_file(str(f), BUCKET, f.relative_to(env).as_posix())
        import shutil
        shutil.rmtree(env / "sites")  # 로컬 제거 → s3 blob 만, db-backup 없음
        assert recovery.classify()["case"] == "recover_s3"


# ---- 2. S3 DB백업 복원 ----


def test_restore_s3_replaces_db_and_clears_first_run(env, monkeypatch):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        _set_s3(monkeypatch)
        config.RULES_PATH.write_text('{"example.com": {}}', encoding="utf-8")
        with db.connect() as conn:
            db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
            db.set_storage_backend(conn, "s3")
        db_backup.run_once()  # 현재 DB(인증 포함)를 백업

        # 빈 인스턴스 시뮬레이션 — DB·rules 제거 후 빈 스키마
        config.DB_PATH.unlink()
        config.RULES_PATH.unlink()
        db.invalidate_schema_cache()
        with db.connect() as conn:
            assert db.count_users(conn) == 0  # first_run
        result = db_backup.restore_latest()
        assert result["restored_key"]
        with db.connect() as conn:
            assert db.count_users(conn) > 0  # 복원으로 사용자 복구 → first_run 해제
            assert db.storage_backend(conn) == "s3"
            assert db.first_run_needed(conn) is False
        assert config.RULES_PATH.is_file()  # rules.json 배치


def test_restore_s3_corrupt_backup_preserves_db(env, monkeypatch):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        _set_s3(monkeypatch)
        with db.connect() as conn:
            db.create_first_admin(conn, "keep@test.co", auth.hash_password("keeppass1234"))
        # 손상된 백업 객체 업로드 (index.db 가 sqlite 가 아님)
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("index.db")
            payload = b"not a sqlite db"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        client.put_object(Bucket=BUCKET, Key="db-backups/2026-06-01T00-00-00Z.tar.gz",
                          Body=buf.getvalue())
        with pytest.raises(RuntimeError):
            db_backup.restore_latest()
        # 기존 DB 보존 (사용자 그대로)
        with db.connect() as conn:
            assert db.get_user_by_email(conn, "keep@test.co") is not None


# ---- 3. 복구모드 재구축 ----


def test_recovery_local_rebuilds_index(env):
    url = "https://example.com/p"
    _make_snapshot(env, url, 1, "h1", resource=True,
                   docs=[{"url": url + "/d.pdf", "file": "d.pdf", "bytes": 5,
                          "sha256": "f" * 64, "content_type": "application/pdf"}])
    _make_snapshot(env, url, 2, "h2")
    _make_snapshot(env, url, 3, "h2")  # 같은 해시 → changed=0

    assert recovery.start_recovery() is None
    _join()
    assert recovery.status()["status"] == "done"
    with db.connect() as conn:
        assert db.count_snapshots_raw(conn) == 3
        rows = conn.execute(
            "SELECT dir_name, authenticated, changed FROM snapshots ORDER BY dir_name"
        ).fetchall()
        # 보안: 복구 스냅샷 전수 authenticated=1
        assert all(r["authenticated"] == 1 for r in rows)
        # changed 재유도: h1(첫)=1, h2(변경)=1, h2(동일)=0
        assert [r["changed"] for r in rows] == [1, 1, 0]
        # snapshot_resources (page.html.gz 의 /resource/ 참조)
        nres = conn.execute("SELECT COUNT(*) c FROM snapshot_resources").fetchone()["c"]
        assert nres == 1
        # snapshot_documents (meta documents)
        ndoc = conn.execute("SELECT COUNT(*) c FROM snapshot_documents").fetchone()["c"]
        assert ndoc == 1
        assert db.storage_backend(conn) == "local"  # 로컬 blob → local
        assert db.migration_mode_enabled(conn) is False  # 일시중지 해제


def test_recovery_idempotent_rerun_skips(env):
    _make_snapshot(env, "https://example.com/p", 1, "h1")
    recovery.start_recovery()
    _join()
    with db.connect() as conn:
        first = db.count_snapshots_raw(conn)
    # 재실행 — 이미 재구축된 스냅샷은 스킵
    recovery._state = {"status": "idle"}
    recovery._thread = None
    recovery.start_recovery()
    _join()
    with db.connect() as conn:
        assert db.count_snapshots_raw(conn) == first  # 중복 생성 없음


def test_recovery_pauses_writes(env, monkeypatch):
    """복구 시작이 캡처·스케줄·크롤 일시중지(migration_mode)를 켠다."""
    monkeypatch.setattr(recovery, "_recover_worker", lambda *a, **k: None)  # 워커 무력화
    _make_snapshot(env, "https://example.com/p", 1, "h1")
    assert recovery.start_recovery() is None
    with db.connect() as conn:
        assert db.migration_mode_enabled(conn) is True  # 일시중지 켜짐


def test_recovery_s3_sets_backend_s3(env, monkeypatch):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        _set_s3(monkeypatch)
        _make_snapshot(env, "https://example.com/p", 1, "h1", resource=True)
        for f in (env / "sites").rglob("*"):
            if f.is_file():
                client.upload_file(str(f), BUCKET, f.relative_to(env).as_posix())
        import shutil
        shutil.rmtree(env / "sites")  # 로컬 제거 → s3 에서 복구
        assert recovery.start_recovery() is None
        _join()
        assert recovery.status()["status"] == "done"
        with db.connect() as conn:
            assert db.count_snapshots_raw(conn) == 1
            assert db.storage_backend(conn) == "s3"  # s3 blob → s3
            r = conn.execute("SELECT authenticated FROM snapshots").fetchone()
            assert r["authenticated"] == 1


# ---- 4. 복구-선택 (관리자) ----


def _admin_client(env):
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(conn, "v@test.co", auth.hash_password("password1234"), role="viewer")
    web_app._active_jobs.clear()
    return TestClient(web_app.app, headers={"X-Requested-With": "fetch"})


def test_recovery_expose_all_and_toggle(env):
    # 먼저 복구로 authenticated=1 스냅샷 생성 (관리자 생성 전 baseline=0)
    _make_snapshot(env, "https://example.com/p", 1, "h1")
    _make_snapshot(env, "https://example.com/p", 2, "h2")
    recovery.start_recovery()
    _join()
    # 관리자 생성 후 복구-선택
    client = _admin_client(env)
    client.post("/api/web/auth/login",
                json={"email": "boss@test.co", "password": "bosspass1234"})
    # 전체 노출 → authenticated=0 일괄
    r = client.post("/api/web/system/recovery/expose-all")
    assert r.status_code == 200 and r.json()["exposed"] == 2
    with db.connect() as conn:
        rows = conn.execute("SELECT authenticated FROM snapshots").fetchall()
        assert all(x["authenticated"] == 0 for x in rows)
    # 단일 토글 → 다시 제한(authenticated=1)
    sid = 1
    r = client.post(f"/api/web/system/recovery/snapshot/{sid}/authenticated",
                    json={"value": True})
    assert r.status_code == 200
    with db.connect() as conn:
        row = conn.execute("SELECT authenticated FROM snapshots WHERE id=?", (sid,)).fetchone()
        assert row["authenticated"] == 1


def test_recovery_choice_forbidden_for_non_admin(env):
    client = _admin_client(env)
    client.post("/api/web/auth/login",
                json={"email": "v@test.co", "password": "password1234"})
    assert client.post("/api/web/system/recovery/expose-all").status_code == 403


# ---- 5. setup 엔드포인트 ----


def test_setup_status_returns_case(env):
    _make_snapshot(env, "https://example.com/p", 1, "h1")
    client = TestClient(web_app.app, headers={"X-Requested-With": "fetch"})
    body = client.get("/api/web/auth/setup").json()
    assert body["needed"] is True
    assert body["case"] == "recover_local"
    assert "recovery" in body


def test_setup_recover_endpoint_starts(env):
    _make_snapshot(env, "https://example.com/p", 1, "h1")
    client = TestClient(web_app.app, headers={"X-Requested-With": "fetch"})
    r = client.post("/api/web/auth/setup/recover")
    assert r.status_code == 200
    _join()
    st = client.get("/api/web/auth/setup/recover/status").json()
    assert st["status"] in ("done", "rebuilding", "scanning")


def test_setup_operating_no_scan(env, monkeypatch):
    """사용자 존재(1-a) 경로는 분류가 blob/S3 스캔을 하지 않는다."""
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
    # _has_snapshots 가 호출되면 실패 — 1-a 는 스캔 없음이어야 한다
    monkeypatch.setattr(recovery, "_has_snapshots",
                        lambda b: (_ for _ in ()).throw(AssertionError("스캔 금지")))
    client = TestClient(web_app.app, headers={"X-Requested-With": "fetch"})
    body = client.get("/api/web/auth/setup").json()
    assert body["needed"] is False and body["case"] == "operating"
