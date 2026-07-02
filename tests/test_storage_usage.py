"""온디맨드 S3 카테고리 사용량 + 로컬 분해 + full backup 차단 + S3 export 라운드트립 (P6).

S3 경로는 moto. 로컬 모드 동작은 다른 테스트가 보장한다.
"""
import gzip
import json
import tarfile

import pytest

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from chunchugwan import (  # noqa: E402
    auth, backup, config, db, storage, storage_usage,
)
from fastapi.testclient import TestClient  # noqa: E402
from chunchugwan.web import app as web_app  # noqa: E402

BUCKET = "wccg-usage-test"


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
    for attr in ("S3_ENDPOINT_URL", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_PREFIX"):
        monkeypatch.setattr(config, attr, "")
    with db.connect():
        pass
    storage._disk_usage_cache = None
    yield tmp_path
    monkeypatch.setattr(config, "_blob_store", None)
    storage._disk_usage_cache = None


def _set_s3(monkeypatch, prefix=""):
    monkeypatch.setattr(config, "S3_BUCKET", BUCKET)
    monkeypatch.setattr(config, "S3_REGION", "us-east-1")
    monkeypatch.setattr(config, "S3_ACCESS_KEY_ID", "k")
    monkeypatch.setattr(config, "S3_SECRET_ACCESS_KEY", "s")
    monkeypatch.setattr(config, "S3_FORCE_PATH_STYLE", True)
    monkeypatch.setattr(config, "S3_PREFIX", prefix)


# ---- S3 사용량 스캔 (카테고리별 합산·캐시·시각) ----


def test_scan_s3_usage_categorizes(env, monkeypatch):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        _set_s3(monkeypatch)
        client.put_object(Bucket=BUCKET, Key="sites/a/b/ts/page.html.gz", Body=b"x" * 100)
        client.put_object(Bucket=BUCKET, Key="resources/ab/n.png", Body=b"y" * 50)
        client.put_object(Bucket=BUCKET, Key="documents/cd/m.pdf", Body=b"z" * 30)
        client.put_object(Bucket=BUCKET, Key="db-backups/2026.tar.gz", Body=b"w" * 10)
        client.put_object(Bucket=BUCKET, Key="stray.txt", Body=b"o" * 5)
        with db.connect() as conn:
            db.set_storage_backend(conn, "s3")

        result = storage_usage.scan_s3_usage()
        cats = result["categories"]
        assert cats["sites"] == 100
        assert cats["resources"] == 50
        assert cats["documents"] == 30
        assert cats["db-backups"] == 10
        assert cats["other"] == 5
        assert result["total"] == 195
        assert result["scanned_at"]
        # DB 에 캐시됨
        with db.connect() as conn:
            assert db.s3_usage(conn)["total"] == 195


def test_scan_s3_usage_respects_prefix(env, monkeypatch):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        _set_s3(monkeypatch, prefix="pre")
        client.put_object(Bucket=BUCKET, Key="pre/sites/a/b/ts/f", Body=b"x" * 40)
        client.put_object(Bucket=BUCKET, Key="other/ignore", Body=b"q" * 999)  # 프리픽스 밖
        with db.connect() as conn:
            db.set_storage_backend(conn, "s3")
        result = storage_usage.scan_s3_usage()
        assert result["categories"]["sites"] == 40
        assert result["total"] == 40  # 프리픽스 밖 객체는 제외


# ---- GET 사용량은 S3 미호출 (캐시만) ----


def test_usage_snapshot_does_not_call_s3(env, monkeypatch):
    """usage_snapshot 은 캐시·로컬만 읽고 S3 클라이언트를 만들지 않는다 (moto 밖에서 성공)."""
    _set_s3(monkeypatch)  # env 설정돼 있어도
    with db.connect() as conn:
        db.set_storage_backend(conn, "s3")
        db.set_s3_usage(conn, {"categories": {"sites": 7}, "total": 7, "scanned_at": "t"})
    # boto3 호출이 일어나면 moto 없이 네트워크 시도 → 실패. 성공하면 S3 미호출 증명.
    snap = storage_usage.usage_snapshot()
    assert snap["backend"] == "s3"
    assert snap["s3"]["total"] == 7
    assert set(snap["local"]) == {"db", "cache", "blobcache"}


def test_archive_disk_usage_s3_is_local_only(env, monkeypatch):
    """S3 모드 archive_disk_usage 는 로컬 분해만 (S3 미호출 — moto 밖 성공)."""
    _set_s3(monkeypatch)
    with db.connect() as conn:
        db.set_storage_backend(conn, "s3")
    usage = storage.archive_disk_usage(fresh=True)
    assert set(usage) == {"db", "cache", "blobcache"}


def test_archive_disk_usage_local_unchanged(env):
    """로컬 모드는 기존 키 그대로."""
    usage = storage.archive_disk_usage(fresh=True)
    assert set(usage) == {"db", "sites", "resources", "documents"}


# ---- full backup 차단 / export 유지 ----


def test_full_backup_blocked_in_s3(env, monkeypatch, tmp_path):
    _set_s3(monkeypatch)
    with db.connect() as conn:
        db.set_storage_backend(conn, "s3")
    with pytest.raises(RuntimeError):
        backup.create_backup(tmp_path / "out")
    with pytest.raises(RuntimeError):
        backup.restore_backup(tmp_path / "any.ccg.backup")


def test_export_s3_streams_and_cross_mode_import(env, monkeypatch, tmp_path):
    """S3 모드 export 가 blob 을 스트리밍 → 로컬 모드 import 라운드트립."""
    domain, slug, dir_name = "example.com", storage.url_to_slug("https://example.com/p"), "2026-06-01T00-00-00"
    url = "https://example.com/p"
    res = b"resource-bytes"
    res_name = __import__("hashlib").sha256(res).hexdigest() + ".png"
    doc = b"%PDF-doc"
    doc_sha = __import__("hashlib").sha256(doc).hexdigest()
    doc_name = doc_sha + ".pdf"
    out = tmp_path / "exp"

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        _set_s3(monkeypatch)
        # DB 행 시드 + S3 blob 업로드
        with db.connect() as conn:
            db.set_storage_backend(conn, "s3")
            pid = db.get_or_create_page(conn, url, domain, slug)
            sid = db.insert_snapshot(conn, pid, taken_at="2026-06-01T00:00:00+00:00",
                                     dir_name=dir_name, content_hash="h", final_url=url,
                                     http_status=200, changed=1)
            db.insert_snapshot_documents(conn, sid, [{
                "url": url, "file": "d.pdf", "bytes": len(doc), "sha256": doc_sha,
                "content_type": "application/pdf"}])
        base = f"sites/{domain}/{slug}/{dir_name}"
        client.put_object(Bucket=BUCKET, Key=f"{base}/content.md", Body=b"body")
        client.put_object(Bucket=BUCKET, Key=f"{base}/meta.json",
                          Body=json.dumps({"url": url}).encode())
        client.put_object(Bucket=BUCKET, Key=f"{base}/page.html.gz", Body=gzip.compress(b"<html>"))
        client.put_object(Bucket=BUCKET, Key=f"resources/{res_name[:2]}/{res_name}", Body=res)
        client.put_object(Bucket=BUCKET, Key=f"documents/{doc_name[:2]}/{doc_name}", Body=doc)
        config.reset_blob_store()

        exp_path = backup.export_archive(out)
        # tar 구조에 blob 파일이 같은 arcname 으로 담겼는지
        with tarfile.open(exp_path, "r:gz") as tar:
            names = set(tar.getnames())
        assert f"{base}/content.md" in names
        assert f"resources/{res_name[:2]}/{res_name}" in names
        assert f"documents/{doc_name[:2]}/{doc_name}" in names
        exp_bytes = exp_path.read_bytes()

    # cross-mode: 로컬 모드 새 인스턴스로 import
    fresh = tmp_path / "fresh"
    monkeypatch.setattr(config, "ARCHIVE_ROOT", fresh)
    monkeypatch.setattr(config, "SITES_DIR", fresh / "sites")
    monkeypatch.setattr(config, "DB_PATH", fresh / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", fresh / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", fresh / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", fresh / "documents")
    monkeypatch.setattr(config, "RULES_PATH", fresh / "rules.json")
    monkeypatch.setattr(config, "_blob_store", None)
    for attr in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        monkeypatch.setattr(config, attr, "")
    fresh.mkdir()
    imp = fresh / "imp.ccg.export"
    imp.write_bytes(exp_bytes)
    with db.connect():
        pass
    result = backup.import_archive(imp, mode="merge")
    assert result.snapshots_added == 1
    # 로컬에 blob 이 기록됐는지 + 내용 일치
    assert (fresh / "resources" / res_name[:2] / res_name).read_bytes() == res
    assert (fresh / "documents" / doc_name[:2] / doc_name).read_bytes() == doc
    assert (fresh / "sites" / domain / slug / dir_name / "content.md").read_bytes() == b"body"


# ---- API ----


def _admin_client(env):
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(conn, "v@test.co", auth.hash_password("password1234"), role="viewer")
    web_app._active_jobs.clear()
    return TestClient(web_app.app, headers={"X-Requested-With": "fetch"})


def test_api_usage_get_and_scan(env, monkeypatch):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        _set_s3(monkeypatch)
        client.put_object(Bucket=BUCKET, Key="sites/a/b/ts/f", Body=b"x" * 20)
        with db.connect() as conn:
            db.set_storage_backend(conn, "s3")
        c = _admin_client(env)
        c.post("/api/web/auth/login", json={"email": "boss@test.co", "password": "bosspass1234"})
        # GET 은 캐시(미스캔이면 None)
        r = c.get("/api/web/system/storage/usage")
        assert r.status_code == 200 and r.json()["backend"] == "s3"
        assert r.json()["s3"] is None  # 아직 미스캔
        # scan POST → 갱신
        r = c.post("/api/web/system/storage/usage/scan")
        assert r.status_code == 200
        assert r.json()["s3"]["categories"]["sites"] == 20


def test_api_usage_scan_409_when_local(env):
    c = _admin_client(env)
    c.post("/api/web/auth/login", json={"email": "boss@test.co", "password": "bosspass1234"})
    assert c.post("/api/web/system/storage/usage/scan").status_code == 409


def test_api_usage_forbidden_for_non_admin(env):
    c = _admin_client(env)
    c.post("/api/web/auth/login", json={"email": "v@test.co", "password": "password1234"})
    assert c.get("/api/web/system/storage/usage").status_code == 403
