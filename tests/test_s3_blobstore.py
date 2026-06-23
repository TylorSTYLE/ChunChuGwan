"""S3 blob 백엔드 + read-through 캐시 + 서빙 경로 (moto 인메모리 S3).

실제 S3·네트워크 없이 moto 로 검증한다. 로컬 기본 모드는 다른 테스트가
보장하므로(WCCG_S3_* 미설정) 여기서는 S3 모드만 다룬다.
"""
import gzip
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from chunchugwan import auth, blobstore, config, db, storage  # noqa: E402
from chunchugwan.web import app as web_app  # noqa: E402

BUCKET = "wccg-test"


def _make_store(archive_root, *, cache_max_bytes=10 * 1024 * 1024, prefix=""):
    """moto 버킷을 만들고 그 위에 S3BlobStore 를 구성한다."""
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    store = blobstore.S3BlobStore(
        bucket=BUCKET,
        archive_root=archive_root,
        cache_dir=archive_root / "blobcache",
        cache_max_bytes=cache_max_bytes,
        region="us-east-1",
        access_key_id="testkey",
        secret_access_key="testsecret",
        force_path_style=True,
        prefix=prefix,
    )
    return store, client


# ---- S3BlobStore 기본 연산 ----


def test_s3_roundtrip_write_read_size(tmp_path):
    with mock_aws():
        store, _ = _make_store(tmp_path)
        p = tmp_path / "resources" / "ab" / "ab1234.png"
        assert store.is_file(p) is False
        store.write_atomic(p, b"hello-bytes")
        assert store.is_file(p) is True
        assert store.read_bytes(p) == b"hello-bytes"
        assert store.size(p) == len(b"hello-bytes")
        assert store.read_bytes(p, size=5) == b"hello"  # range GET


def test_s3_existence_check_does_not_download(tmp_path):
    """is_file·is_dir·size 는 객체를 캐시로 받지 않는다 (HEAD/LIST 만)."""
    with mock_aws():
        store, _ = _make_store(tmp_path)
        p = tmp_path / "sites" / "d" / "s" / "ts" / "page.html.gz"
        payload = b"\x1f\x8bdata"
        store.write_bytes(p, payload)
        cache = tmp_path / "blobcache"
        assert store.is_file(p) is True
        assert store.size(p) == len(payload)
        assert store.is_dir(tmp_path / "sites" / "d") is True
        # 존재 확인만으로는 캐시에 어떤 파일도 생기지 않아야 한다
        assert not cache.exists() or not any(cache.rglob("*"))


def test_s3_local_path_caches_and_serves_from_cache(tmp_path):
    """local_path 가 미스 시 다운로드, 히트 시 캐시에서 (S3 객체가 지워져도) 서빙."""
    with mock_aws():
        store, client = _make_store(tmp_path)
        p = tmp_path / "resources" / "cd" / "cd5678.css"
        store.write_atomic(p, b"body{}")

        local = store.local_path(p)  # 캐시 미스 → 다운로드
        assert local.read_bytes() == b"body{}"
        assert local.is_relative_to(tmp_path / "blobcache")
        assert not list(local.parent.glob("*.tmp"))  # 부분 파일 잔재 없음

        # S3 객체를 지워도 캐시 히트로 같은 바이트가 나와야 한다 (다운로드 없음)
        client.delete_object(Bucket=BUCKET, Key="resources/cd/cd5678.css")
        local2 = store.local_path(p)
        assert local2 == local
        assert local2.read_bytes() == b"body{}"


def test_s3_cache_lru_eviction(tmp_path):
    """캐시 총량이 상한을 넘으면 LRU(오래 미접근)부터 제거된다."""
    with mock_aws():
        store, _ = _make_store(tmp_path, cache_max_bytes=2500)
        paths = []
        for i in range(4):
            p = tmp_path / "resources" / "ee" / f"{'e' * 63}{i}.png"
            store.write_atomic(p, bytes([i]) * 1000)  # 각 1000 bytes
            paths.append(p)
            store.local_path(p)  # 순서대로 materialize → 오래된 것이 먼저 제거 대상
        cache = tmp_path / "blobcache"
        remaining = [f for f in cache.rglob("*") if f.is_file()]
        total = sum(f.stat().st_size for f in remaining)
        assert total <= 2500  # 상한 준수
        assert len(remaining) < 4  # 일부는 제거됨


def test_s3_prefix_key_layout(tmp_path):
    """WCCG_S3_PREFIX 가 키 앞에 붙는다."""
    with mock_aws():
        store, client = _make_store(tmp_path, prefix="myprefix")
        p = tmp_path / "documents" / "ff" / "ff9999.pdf"
        store.write_bytes(p, b"%PDF-")
        # 프리픽스 하위 키로 저장됐는지 직접 확인
        obj = client.get_object(Bucket=BUCKET, Key="myprefix/documents/ff/ff9999.pdf")
        assert obj["Body"].read() == b"%PDF-"
        assert store.read_bytes(p) == b"%PDF-"


def test_s3_move_uploads_and_removes_local(tmp_path):
    """move(스테이징 로컬 → S3) 는 업로드 후 로컬 원본을 지운다."""
    with mock_aws():
        store, _ = _make_store(tmp_path)
        src = tmp_path / "staging" / "doc.pdf"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"payload")
        dst = tmp_path / "documents" / "aa" / "aabbcc.pdf"
        store.move(src, dst)
        assert not src.exists()
        assert store.is_file(dst) is True
        assert store.read_bytes(dst) == b"payload"


def test_s3_delete_and_rmtree(tmp_path):
    with mock_aws():
        store, _ = _make_store(tmp_path)
        a = tmp_path / "sites" / "d" / "s" / "t1" / "content.md"
        b = tmp_path / "sites" / "d" / "s" / "t2" / "content.md"
        store.write_bytes(a, b"a")
        store.write_bytes(b, b"b")
        store.delete(a)
        assert store.is_file(a) is False
        store.rmtree(tmp_path / "sites" / "d" / "s")
        assert store.is_file(b) is False
        assert store.is_dir(tmp_path / "sites" / "d") is False


def test_s3_glob_and_iterdir(tmp_path):
    with mock_aws():
        store, _ = _make_store(tmp_path)
        # resources/{ab}/{name} 두 개
        store.write_bytes(tmp_path / "resources" / "ab" / "ab1.png", b"1")
        store.write_bytes(tmp_path / "resources" / "cd" / "cd2.png", b"2")
        names = sorted(p.name for p in store.glob(tmp_path / "resources", "*/*"))
        assert names == ["ab1.png", "cd2.png"]
        # rglob 은 모든 파일
        assert len(list(store.rglob(tmp_path / "resources", "*"))) == 2
        # iterdir 은 직속 항목 (버킷 디렉토리 ab, cd)
        children = sorted(p.name for p in store.iterdir(tmp_path / "resources"))
        assert children == ["ab", "cd"]


# ---- config 팩토리 / 활성 판정 (활성 = DB 설정 storage_backend, env 는 가용성만) ----


def _isolated_db(tmp_path, monkeypatch):
    """tmp 아카이브 루트/DB 로 격리 (storage_backend 설정을 쓰기 위함)."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "BLOB_CACHE_DIR", tmp_path / "blobcache")
    monkeypatch.setattr(config, "_blob_store", None)


def test_factory_local_by_default(tmp_path, monkeypatch):
    """storage_backend 미설정이면 env 가 있어도 LocalBlobStore (env 단독 전환 금지)."""
    _isolated_db(tmp_path, monkeypatch)
    # env 가 완전해도 활성 백엔드 설정이 없으면 로컬이어야 한다
    monkeypatch.setattr(config, "S3_BUCKET", BUCKET)
    monkeypatch.setattr(config, "S3_ACCESS_KEY_ID", "k")
    monkeypatch.setattr(config, "S3_SECRET_ACCESS_KEY", "s")
    assert isinstance(config.blob_store(), blobstore.LocalBlobStore)


def test_factory_s3_when_backend_setting_s3(tmp_path, monkeypatch):
    """storage_backend='s3' + env 완전 → S3BlobStore."""
    with mock_aws():
        _isolated_db(tmp_path, monkeypatch)
        monkeypatch.setattr(config, "S3_ENDPOINT_URL", "")
        monkeypatch.setattr(config, "S3_BUCKET", BUCKET)
        monkeypatch.setattr(config, "S3_ACCESS_KEY_ID", "k")
        monkeypatch.setattr(config, "S3_SECRET_ACCESS_KEY", "s")
        with db.connect() as conn:
            db.set_storage_backend(conn, "s3")
        monkeypatch.setattr(config, "_blob_store", None)
        assert isinstance(config.blob_store(), blobstore.S3BlobStore)
        monkeypatch.setattr(config, "_blob_store", None)


def test_s3_backend_setting_but_incomplete_env_boot_fails(tmp_path, monkeypatch):
    """storage_backend='s3' 인데 env 불완전이면 부팅 실패 — 누락 변수명만, 비밀값 미노출."""
    _isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "S3_ENDPOINT_URL", "https://minio.example")
    monkeypatch.setattr(config, "S3_BUCKET", "")  # 누락
    monkeypatch.setattr(config, "S3_ACCESS_KEY_ID", "")  # 누락
    monkeypatch.setattr(config, "S3_SECRET_ACCESS_KEY", "TOP-SECRET-VALUE")
    with db.connect() as conn:
        db.set_storage_backend(conn, "s3")
    monkeypatch.setattr(config, "_blob_store", None)
    with pytest.raises(RuntimeError) as ei:
        config.blob_store()
    msg = str(ei.value)
    assert "WCCG_S3_BUCKET" in msg
    assert "WCCG_S3_ACCESS_KEY_ID" in msg
    assert "TOP-SECRET-VALUE" not in msg  # 비밀값 미노출


# ---- 서빙 경로 (read-through 캐시 경유) ----


def _png_bytes(color):
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def s3_serving(tmp_path, monkeypatch):
    """로컬에 아카이브를 시드 → 전체 blob 을 S3 로 올리고 로컬 트리 삭제 →
    S3 모드로 전환해 서빙이 read-through 캐시를 거치게 한다."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "BLOB_CACHE_DIR", tmp_path / "blobcache")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        monkeypatch.setattr(config, "S3_ENDPOINT_URL", "")
        monkeypatch.setattr(config, "S3_BUCKET", BUCKET)
        monkeypatch.setattr(config, "S3_REGION", "us-east-1")
        monkeypatch.setattr(config, "S3_ACCESS_KEY_ID", "k")
        monkeypatch.setattr(config, "S3_SECRET_ACCESS_KEY", "s")
        monkeypatch.setattr(config, "S3_FORCE_PATH_STYLE", True)
        monkeypatch.setattr(config, "S3_PREFIX", "")
        # 활성 백엔드를 DB 설정으로 's3' 로 전환 (env 단독으로는 활성화되지 않음)
        with db.connect() as conn:
            db.set_storage_backend(conn, "s3")
        monkeypatch.setattr(config, "_blob_store", None)
        web_app._active_jobs.clear()
        yield s3, tmp_path
        web_app._active_jobs.clear()
        monkeypatch.setattr(config, "_blob_store", None)


def _upload_tree_then_drop_local(s3, archive_root):
    """로컬 blob 트리(sites·resources·documents)를 S3 로 올리고 로컬은 지운다."""
    import shutil

    for sub in ("sites", "resources", "documents"):
        root = archive_root / sub
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            if f.is_file():
                key = f.relative_to(archive_root).as_posix()
                s3.upload_file(str(f), BUCKET, key)
        shutil.rmtree(root)  # 로컬 제거 → 서빙은 반드시 S3 에서 와야 한다


def test_s3_serving_all_routes(s3_serving):
    s3, root = s3_serving
    URL = "https://example.com/page"
    domain, slug = "example.com", storage.url_to_slug(URL)

    # 자원 CAS (이미지) + 문서 CAS (pdf) 바이트
    res_bytes = _png_bytes((10, 20, 30))
    import hashlib
    res_name = hashlib.sha256(res_bytes).hexdigest() + ".png"
    doc_bytes = b"%PDF-1.4 fake document"
    doc_sha = hashlib.sha256(doc_bytes).hexdigest()
    doc_file = "report.pdf"
    doc_cas = doc_sha + ".pdf"

    # DB + 로컬 파일 시드
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        page_id = db.get_or_create_page(conn, URL, domain, slug)
        snap_ids = []
        for i, color in enumerate([(0, 0, 0), (255, 0, 0)]):
            d = f"2026-06-0{i + 1}T00-00-00"
            snap_dir = storage.page_dir(domain, slug) / d
            snap_dir.mkdir(parents=True)
            (snap_dir / "content.md").write_text(f"body {i}", encoding="utf-8")
            (snap_dir / "page.html.gz").write_bytes(gzip.compress(b"<html>page</html>"))
            (snap_dir / "screenshot.png").write_bytes(_png_bytes(color))
            storage.write_meta(snap_dir, storage.SnapshotMeta(
                url=URL, final_url=URL, taken_at=f"2026-06-0{i + 1}T00:00:00+00:00",
                content_hash=f"h{i}", http_status=200, title="t",
                documents=[{"url": URL + "/r.pdf", "file": doc_file,
                            "bytes": len(doc_bytes), "sha256": doc_sha,
                            "content_type": "application/pdf"}],
            ))
            sid = db.insert_snapshot(
                conn, page_id, taken_at=f"2026-06-0{i + 1}T00:00:00+00:00",
                dir_name=d, content_hash=f"h{i}", final_url=URL, http_status=200,
                changed=1,
            )
            db.insert_snapshot_documents(conn, sid, [{
                "url": URL + "/r.pdf", "file": doc_file, "bytes": len(doc_bytes),
                "sha256": doc_sha, "content_type": "application/pdf"}])
            snap_ids.append(sid)

    # storage.write_meta 는 S3 모드라 meta.json 을 이미 S3 로 기록했다. 나머지 blob
    # (content.md·page.html.gz·screenshot·CAS)은 로컬에 둔 뒤 통째로 업로드한다.
    (config.RESOURCES_DIR / res_name[:2]).mkdir(parents=True, exist_ok=True)
    (config.RESOURCES_DIR / res_name[:2] / res_name).write_bytes(res_bytes)
    (config.DOCUMENTS_DIR / doc_cas[:2]).mkdir(parents=True, exist_ok=True)
    (config.DOCUMENTS_DIR / doc_cas[:2] / doc_cas).write_bytes(doc_bytes)

    _upload_tree_then_drop_local(s3, root)
    # 로컬 blob 트리가 사라졌는지 확인 (서빙은 S3 경유여야 함)
    assert not (config.SITES_DIR).exists()
    assert not (config.RESOURCES_DIR).exists()

    client = TestClient(web_app.app, headers={"X-Requested-With": "fetch"})
    client.post("/api/web/auth/login",
                json={"email": "boss@test.co", "password": "bosspass1234"})

    sid = snap_ids[0]
    # 1) /snapshot/{id}/file/content.md
    r = client.get(f"/snapshot/{sid}/file/content.md")
    assert r.status_code == 200
    assert r.content == b"body 0"
    assert r.headers["cache-control"] == "private, max-age=31536000, immutable"

    # page.html → CSP sandbox + gzip 헤더 유지
    r = client.get(f"/snapshot/{sid}/file/page.html")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == (
        "sandbox allow-top-navigation-by-user-activation")
    assert r.headers["content-encoding"] == "gzip"

    # 2) /resource/{name} (인증 예외 + CSP sandbox)
    r = client.get(f"/resource/{res_name}")
    assert r.status_code == 200
    assert r.content == res_bytes
    assert r.headers["content-security-policy"] == "sandbox"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"

    # 3) /snapshot/{id}/doc/{name} (인증 게이트 + attachment)
    r = client.get(f"/snapshot/{sid}/doc/{doc_file}")
    assert r.status_code == 200
    assert r.content == doc_bytes
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["content-security-policy"] == "sandbox"

    # 4) /document/{sha256}/{name}
    r = client.get(f"/document/{doc_sha}/{doc_file}")
    assert r.status_code == 200
    assert r.content == doc_bytes
    assert "attachment" in r.headers["content-disposition"]

    # 5) /diff/{page_id}/shotdiff (두 스냅샷 스크린샷 픽셀 diff)
    r = client.get(f"/diff/{page_id}/shotdiff")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"

    # read-through 캐시에 실제로 materialize 됐는지 확인
    cache_files = [f for f in (root / "blobcache").rglob("*") if f.is_file()]
    assert cache_files  # 서빙이 캐시를 채웠다


def test_s3_serving_auth_gate_preserved(s3_serving):
    """문서 라우트는 S3 모드에서도 비로그인 접근을 막는다 (인증 게이트 보존)."""
    s3, root = s3_serving
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
    client = TestClient(web_app.app, headers={"X-Requested-With": "fetch"})
    # 비로그인 → 문서/스냅샷 파일 라우트는 401/403 (뷰어 권한 필요)
    assert client.get("/snapshot/1/file/content.md").status_code in (401, 403)
    assert client.get("/document/" + "0" * 64 + "/x.pdf").status_code in (401, 403)
