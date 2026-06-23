"""S3 DB 백업 — 일관 복사·rules.json 동봉·업로드·보존 rotation·정기 실행·API.

실제 S3 없이 moto 로 검증한다. 로컬 모드 비활성은 별도 테스트로 확인한다.
"""
import io
import tarfile

import pytest
from fastapi.testclient import TestClient

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from chunchugwan import auth, config, db, db_backup, scheduler  # noqa: E402
from chunchugwan.web import app as web_app  # noqa: E402

BUCKET = "wccg-dbbackup-test"


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
    monkeypatch.setattr(db_backup, "_running", False)
    with db.connect():
        pass  # 스키마 생성
    yield tmp_path
    monkeypatch.setattr(config, "_blob_store", None)


@pytest.fixture
def s3_env(env, monkeypatch):
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
        with db.connect() as conn:
            db.set_storage_backend(conn, "s3")
        yield client, env


def _list_keys(client):
    resp = client.list_objects_v2(Bucket=BUCKET, Prefix="db-backups/")
    return sorted(o["Key"] for o in resp.get("Contents", []))


def _tar_members(client, key):
    obj = client.get_object(Bucket=BUCKET, Key=key)
    with tarfile.open(fileobj=io.BytesIO(obj["Body"].read()), mode="r:gz") as tar:
        return sorted(tar.getnames())


# ---- 백업 생성 (일관 복사 + rules.json) ----


def test_build_archive_includes_db_and_rules(env):
    config.RULES_PATH.write_text('{"example.com": {}}', encoding="utf-8")
    data = db_backup._build_archive()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = sorted(tar.getnames())
        assert names == ["index.db", "rules.json"]
        # 일관 복사된 index.db 는 유효한 sqlite (헤더 확인)
        dbf = tar.extractfile("index.db").read()
        assert dbf.startswith(b"SQLite format 3\x00")


def test_build_archive_without_rules_is_safe(env):
    assert not config.RULES_PATH.exists()
    data = db_backup._build_archive()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        assert tar.getnames() == ["index.db"]


# ---- 업로드 + 메타 ----


def test_run_once_uploads_to_db_backups(s3_env):
    client, _ = s3_env
    config.RULES_PATH.write_text("{}", encoding="utf-8")
    meta = db_backup.run_once()
    keys = _list_keys(client)
    assert len(keys) == 1
    assert keys[0].startswith("db-backups/") and keys[0].endswith(".tar.gz")
    assert meta["last_key"] == keys[0]
    assert meta["last_status"] == "ok"
    assert _tar_members(client, keys[0]) == ["index.db", "rules.json"]
    # 메타가 DB 에 저장됨
    with db.connect() as conn:
        assert db.db_backup_meta(conn)["last_key"] == keys[0]


def test_run_once_prefix_applied(env, monkeypatch):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        monkeypatch.setattr(config, "S3_ENDPOINT_URL", "")
        monkeypatch.setattr(config, "S3_BUCKET", BUCKET)
        monkeypatch.setattr(config, "S3_ACCESS_KEY_ID", "k")
        monkeypatch.setattr(config, "S3_SECRET_ACCESS_KEY", "s")
        monkeypatch.setattr(config, "S3_PREFIX", "myprefix")
        with db.connect() as conn:
            db.set_storage_backend(conn, "s3")
        meta = db_backup.run_once()
        assert meta["last_key"].startswith("myprefix/db-backups/")


# ---- 보존 rotation ----


def test_rotation_keeps_newest_deletes_old(s3_env, monkeypatch):
    client, _ = s3_env
    with db.connect() as conn:
        db.set_db_backup_settings(conn, interval_hours=24, keep=3)
    # 타임스탬프가 겹치지 않게 _timestamp 를 단조 증가로 교체
    seq = {"n": 0}

    def fake_ts():
        seq["n"] += 1
        return f"2026-06-01T00-00-0{seq['n']}Z"
    monkeypatch.setattr(db_backup, "_timestamp", fake_ts)

    for _ in range(5):
        db_backup.run_once()
    keys = _list_keys(client)
    assert len(keys) == 3  # keep=3
    # 최신 3개(04,05 ... 가장 큰 타임스탬프)가 남는다
    assert keys == [
        "db-backups/2026-06-01T00-00-03Z.tar.gz",
        "db-backups/2026-06-01T00-00-04Z.tar.gz",
        "db-backups/2026-06-01T00-00-05Z.tar.gz",
    ]


# ---- run_blocking 가드 ----


def test_run_blocking_requires_s3(env):
    # storage_backend 기본 local → RuntimeError
    with pytest.raises(RuntimeError):
        db_backup.run_blocking()


def test_run_blocking_works_in_s3(s3_env):
    client, _ = s3_env
    db_backup.run_blocking()
    assert len(_list_keys(client)) == 1


# ---- 정기 실행 (run_scheduled) ----


def test_run_scheduled_runs_when_due(s3_env):
    client, _ = s3_env
    db_backup.run_scheduled()  # 메타 없음 → 도래
    assert len(_list_keys(client)) == 1


def test_run_scheduled_skips_when_not_due(s3_env):
    client, _ = s3_env
    db_backup.run_once()  # last_at = now
    db_backup.run_scheduled()  # 주기(24h) 안 지남 → 스킵
    assert len(_list_keys(client)) == 1


def test_run_scheduled_skips_when_local(env, monkeypatch):
    # s3 아님 → 정기 백업 안 함 (S3 호출 자체가 없어야 함)
    monkeypatch.setattr(db_backup, "_client_bucket_prefix",
                        lambda: (_ for _ in ()).throw(AssertionError("S3 호출 금지")))
    db_backup.run_scheduled()  # storage_backend=local → 즉시 반환


def test_run_scheduled_skips_when_paused(s3_env):
    client, _ = s3_env
    with db.connect() as conn:
        db.set_storage_migration_active(conn, True)  # 일시중지
    db_backup.run_scheduled()
    assert _list_keys(client) == []  # 건너뜀


def test_run_scheduled_survives_failure(s3_env, monkeypatch):
    """run_once 실패해도 예외를 던지지 않고 메타에 오류만 남긴다 (스레드 보호)."""
    def _boom():
        raise RuntimeError("업로드 실패")
    monkeypatch.setattr(db_backup, "run_once", _boom)
    db_backup.run_scheduled()  # 예외 전파 안 됨
    with db.connect() as conn:
        assert db.db_backup_meta(conn)["last_status"] == "error"


def test_scheduler_loop_calls_run_scheduled(env, monkeypatch):
    """스케줄러 폴링 1회가 db_backup.run_scheduled 를 호출한다."""
    import threading
    called = {"n": 0}
    monkeypatch.setattr(db_backup, "run_scheduled", lambda: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(scheduler, "run_due", lambda **k: [])
    stop = threading.Event()

    def _stop_after():
        # 한 번 폴링하면 멈춘다
        stop.set()
    # poll_seconds 를 0 으로 두면 즉시 1회 실행 후 stop 확인
    t = threading.Thread(target=scheduler.run_loop, args=(stop,), kwargs={"poll_seconds": 0})
    t.start()
    import time
    time.sleep(0.05)
    stop.set()
    t.join(timeout=5)
    assert called["n"] >= 1


# ---- 설정 클램핑 ----


def test_settings_clamping(env):
    with db.connect() as conn:
        db.set_setting(conn, db.DB_BACKUP_INTERVAL_HOURS_KEY, "99999")  # > MAX(720)
        db.set_setting(conn, db.DB_BACKUP_KEEP_KEY, "0")  # < MIN(1)
        assert db.db_backup_interval_hours(conn) == config.DB_BACKUP_INTERVAL_HOURS_MAX
        assert db.db_backup_keep(conn) == config.DB_BACKUP_KEEP_MIN
        db.set_setting(conn, db.DB_BACKUP_INTERVAL_HOURS_KEY, "garbage")
        assert db.db_backup_interval_hours(conn) == config.DB_BACKUP_INTERVAL_HOURS_DEFAULT


# ---- status ----


def test_status_local_mode(env):
    st = db_backup.status()
    assert st["s3_mode"] is False
    assert st["count"] == 0


def test_status_s3_lists_backups(s3_env):
    client, _ = s3_env
    db_backup.run_once()
    st = db_backup.status()
    assert st["s3_mode"] is True
    assert st["count"] == 1
    assert len(st["backups"]) == 1
    assert st["last_status"] == "ok"


# ---- API ----


def _admin_client(env):
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(conn, "v@test.co", auth.hash_password("password1234"), role="viewer")
    web_app._active_jobs.clear()
    return TestClient(web_app.app, headers={"X-Requested-With": "fetch"})


def test_api_status_and_settings(env):
    client = _admin_client(env)
    client.post("/api/web/auth/login",
                json={"email": "boss@test.co", "password": "bosspass1234"})
    r = client.get("/api/web/system/db-backup/status")
    assert r.status_code == 200
    assert r.json()["s3_mode"] is False
    # 설정 변경 + 클램핑 범위 검증
    r = client.post("/api/web/system/db-backup/settings",
                    json={"interval_hours": 12, "keep": 7})
    assert r.status_code == 200
    with db.connect() as conn:
        assert db.db_backup_interval_hours(conn) == 12
        assert db.db_backup_keep(conn) == 7
    r = client.post("/api/web/system/db-backup/settings",
                    json={"interval_hours": 99999, "keep": 7})
    assert r.status_code == 400


def test_api_run_409_when_local(env):
    client = _admin_client(env)
    client.post("/api/web/auth/login",
                json={"email": "boss@test.co", "password": "bosspass1234"})
    r = client.post("/api/web/system/db-backup/run")
    assert r.status_code == 409  # S3 아님


def test_api_status_forbidden_for_non_admin(env):
    client = _admin_client(env)
    client.post("/api/web/auth/login",
                json={"email": "v@test.co", "password": "password1234"})
    assert client.get("/api/web/system/db-backup/status").status_code == 403
