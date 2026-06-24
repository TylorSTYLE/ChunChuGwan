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


def test_s3_is_file_independent_of_head_object_404_dialect(tmp_path):
    """없는 객체의 HeadObject 에 400(404 아님) 을 주는 제공자에서도 동작.

    일부 S3 호환 제공자는 없는 객체의 HeadObject 에 404 가 아니라 400 Bad
    Request 를 돌려준다. is_file 이 HEAD 에 의존하면 그 400 을 "없음"으로
    매핑하지 못해 캡처 dedup 체크(_write_cas)가 크래시한다. LIST 기반이면
    HeadObject 가 어떤 방언을 쓰든 영향받지 않아야 한다.
    """
    from botocore.exceptions import ClientError

    with mock_aws():
        store, _ = _make_store(tmp_path)
        existing = tmp_path / "resources" / "ab" / "ab1234.png"
        missing = tmp_path / "resources" / "cd" / "cd5678.png"
        store.write_atomic(existing, b"x")

        # HeadObject 가 항상 400 을 던지게 해도 is_file 은 LIST 로 판정해야 한다.
        def _boom(*_a, **_k):
            raise ClientError(
                {"Error": {"Code": "400", "Message": "Bad Request"}},
                "HeadObject",
            )

        store._client.head_object = _boom
        assert store.is_file(existing) is True
        assert store.is_file(missing) is False


def test_s3_put_omits_expect_100_continue(tmp_path):
    """PUT 에 Expect: 100-continue 를 보내지 않는다 (Garage/MinIO 헤더파싱 경고 회피)."""
    with mock_aws():
        store, _ = _make_store(tmp_path)
        captured: dict = {}

        def _spy(params, **kwargs):
            captured.update(params.get("headers", {}))
        store._client.meta.events.register("before-call.s3", _spy)
        store.write_bytes(tmp_path / "resources" / "ab" / "ab12.png", b"x" * 200)
        # add_expect_header 가 언레지스터됐으므로 Expect 헤더가 없어야 한다
        assert "Expect" not in captured


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


def test_s3_staging_paths_use_local_fs(tmp_path):
    """아카이브 루트 밖 경로(캡처 tmp 스테이징)는 S3 가 아니라 로컬 파일시스템.

    캡처는 tmp 에 산출물을 모아 compact 한 뒤 finalize 에서만 S3 로 올린다.
    그 전 tmp 입출력에 S3 키 매핑(_rel)을 적용하면 archive_root 밖이라
    ValueError 가 났다(회귀 방지)."""
    with mock_aws():
        store, client = _make_store(tmp_path)
        staging = tmp_path.parent / "wccg-staging-xyz"  # archive_root 밖
        staging.mkdir()
        page = staging / "page.html"

        # 쓰기·존재·크기·읽기·삭제가 모두 로컬에서 동작하고 S3 를 건드리지 않는다
        store.write_bytes(page, b"<html>raw</html>")
        assert store.is_file(page) is True
        assert page.is_file()  # 실제 로컬 파일
        assert store.size(page) == len(b"<html>raw</html>")
        assert store.read_text(page) == "<html>raw</html>"
        assert store.local_path(page) == page  # 캐시 거치지 않고 그 경로 그대로
        # 스테이징에 쓴 것은 S3 버킷에 객체로 남지 않는다
        assert client.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == 0

        store.delete(page)
        assert store.is_file(page) is False


def test_s3_move_staging_dir_to_archive_uploads(tmp_path):
    """move(스테이징 디렉토리 → 아카이브)는 디렉토리째 S3 로 업로드한다 (finalize 경로)."""
    with mock_aws():
        store, _ = _make_store(tmp_path)
        staging = tmp_path.parent / "wccg-finalize-abc"
        staging.mkdir()
        (staging / "page.html.gz").write_bytes(b"gz")
        (staging / "content.md").write_text("body", encoding="utf-8")
        dst = tmp_path / "sites" / "d" / "s" / "2026-06-24T00-00-00"
        store.move(staging, dst)
        assert not staging.exists()
        assert store.read_bytes(dst / "page.html.gz") == b"gz"
        assert store.read_text(dst / "content.md") == "body"


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


def test_s3_system_overview_usage_keys(s3_serving):
    """S3 모드 /api/web/system 은 db/cache/blobcache 사용량을 내려준다 (KeyError 회귀).

    S3 모드 archive_disk_usage 는 sites/resources/documents 대신 로컬 분해
    (db/cache/blobcache)를 돌려주므로, 엔드포인트가 고정 키를 골라내면 안 된다."""
    s3, root = s3_serving
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
    client = TestClient(web_app.app, headers={"X-Requested-With": "fetch"})
    client.post("/api/web/auth/login",
                json={"email": "boss@test.co", "password": "bosspass1234"})
    r = client.get("/api/web/system")
    assert r.status_code == 200
    usage = r.json()["usage"]
    assert set(usage) == {"db", "cache", "blobcache"}  # S3 모드 키
    assert "sites" not in usage  # 로컬 전용 키는 없다


def test_s3_compact_tmp_staging_extracts_to_s3_cas(s3_serving):
    """S3 모드 캡처: compact_snapshot_dir 가 tmp(로컬) 산출물을 변환하되
    추출 자원은 S3 CAS 로 올린다 (tmp 경로 _rel ValueError 회귀)."""
    import base64
    import hashlib

    from chunchugwan import resources

    s3, root = s3_serving
    # archive_root 밖 캡처 스테이징 디렉토리 (pipeline 의 tempfile.mkdtemp 모사)
    staging = root.parent / "wccg-capture-tmp"
    staging.mkdir()
    # RESOURCE_MIN_BYTES 이상인 PNG data URI 한 개를 가진 page.html
    blob = b"\x89PNG\r\n" + b"Z" * (config.RESOURCE_MIN_BYTES + 100)
    b64 = base64.b64encode(blob).decode()
    (staging / "page.html").write_text(
        f'<html><img src="data:image/png;base64,{b64}"></html>', encoding="utf-8")

    stats = resources.compact_snapshot_dir(staging, "https://example.com/")

    assert stats.externalized == 1
    # tmp 산출물은 로컬에서 gz 로 바뀌고 원본 page.html 은 사라진다
    assert (staging / "page.html.gz").is_file()
    assert not (staging / "page.html").exists()
    # 추출된 자원은 S3 CAS(archive_root 안)로 올라가야 한다
    name = stats.resource_names[0]
    assert config.blob_store().is_file(resources.resource_path(name))
    sha = hashlib.sha256(blob).hexdigest()
    obj = s3.get_object(Bucket=BUCKET, Key=f"resources/{sha[:2]}/{name}")
    assert obj["Body"].read() == blob


def test_s3_document_ingest_uploads_to_cas(s3_serving):
    """S3 모드 캡처: ingest_into_cas 가 받은 문서를 로컬이 아니라 S3 CAS 로 올린다.

    _move_into_cas 가 os.replace(로컬 전용)를 쓰면 S3 모드에서도 문서가 로컬
    디스크에만 남아 서빙(local_path)이 S3 에서 못 찾는다(회귀 방지)."""
    import hashlib

    from chunchugwan import documents

    s3, root = s3_serving
    staging = root.parent / "wccg-doc-tmp" / "files"  # archive_root 밖 캡처 스테이징
    staging.mkdir(parents=True)
    doc_bytes = b"%PDF-1.4 captured document body"
    (staging / "report.pdf").write_bytes(doc_bytes)
    sha = hashlib.sha256(doc_bytes).hexdigest()
    manifest = [{"url": "https://example.com/r.pdf", "file": "report.pdf",
                 "bytes": len(doc_bytes), "sha256": sha,
                 "content_type": "application/pdf"}]

    documents.ingest_into_cas(staging, manifest)

    assert manifest  # 항목이 유지됐다
    cas = documents.cas_path(sha + ".pdf")
    # 로컬 스테이징 원본은 사라지고, S3 CAS 에 객체가 있어야 한다 (로컬 디스크 아님)
    assert not (staging / "report.pdf").exists()
    assert not cas.exists()  # 로컬 archive 트리에는 없다 (S3 모드)
    obj = s3.get_object(Bucket=BUCKET, Key=f"documents/{sha[:2]}/{sha}.pdf")
    assert obj["Body"].read() == doc_bytes
    assert config.blob_store().read_bytes(cas) == doc_bytes


def test_s3_compactable_count_single_list(s3_serving):
    """S3 모드 compactable_count 는 sites/ 를 한 번만 나열해 정확히 센다.

    스냅샷마다 meta.json HEAD + files/ LIST 를 하던 N+1(시스템 화면 504)을
    없앤 뒤에도, 구형 산출물이 남은 스냅샷 수가 맞아야 한다 (S3 에선 레거시
    검사가 로컬 .is_file() 이라 과소 집계되던 정확성 문제도 함께 교정)."""
    from chunchugwan import resources

    s3, root = s3_serving
    base = config.SITES_DIR / "example.com" / "post-abcd1234"
    legacy = base / "2026-06-01T00-00-00"      # 구형 raw.html → 대상
    compacted = base / "2026-06-02T00-00-00"   # 변환 완료 → 비대상
    for d in (legacy, compacted):
        d.mkdir(parents=True)
        (d / "meta.json").write_text("{}", encoding="utf-8")
    (legacy / "raw.html").write_text("<html>원본</html>", encoding="utf-8")
    (compacted / "raw.html.gz").write_bytes(gzip.compress(b"<html></html>"))

    _upload_tree_then_drop_local(s3, root)
    assert not config.SITES_DIR.exists()  # 로컬 트리 없음 → 판정은 S3 나열로만

    assert resources.compactable_count() == 1


def test_s3_serving_auth_gate_preserved(s3_serving):
    """문서 라우트는 S3 모드에서도 비로그인 접근을 막는다 (인증 게이트 보존)."""
    s3, root = s3_serving
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
    client = TestClient(web_app.app, headers={"X-Requested-With": "fetch"})
    # 비로그인 → 문서/스냅샷 파일 라우트는 401/403 (뷰어 권한 필요)
    assert client.get("/snapshot/1/file/content.md").status_code in (401, 403)
    assert client.get("/document/" + "0" * 64 + "/x.pdf").status_code in (401, 403)
