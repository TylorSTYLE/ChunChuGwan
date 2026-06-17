"""ingest.py (확장 클라이언트 캡처 적재) 테스트. 네트워크 없이 fixture 페이로드로 검증."""
import base64
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest

from chunchugwan import config, db, documents, ingest, netcheck, pipeline, resources, storage


def _use_advancing_clock(monkeypatch):
    """ingest 의 시각 소스를 1초씩 증가하게 고정 — 같은 페이지 다중 스냅샷의
    디렉토리명(초 단위) 충돌을 피한다 (실제로는 캡처가 초 단위로 떨어진다)."""
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    state = {"n": 0}

    class _Clock:
        @staticmethod
        def now(tz=None):
            state["n"] += 1
            return base + timedelta(seconds=state["n"])

    monkeypatch.setattr(ingest, "datetime", _Clock)

URL = "https://example.com/post"
RAW = "<html><body><h1>제목</h1><p>본문 내용입니다.</p></body></html>"
PAGE = "<!DOCTYPE html><html><body><h1>제목</h1><p>본문 내용입니다.</p></body></html>"


@pytest.fixture
def root(tmp_path, monkeypatch):
    """격리된 임시 아카이브 루트."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.json")
    with db.connect():  # 스키마/마이그레이션 초기화
        pass
    return tmp_path


def _dir_for(url: str, dir_name: str):
    norm = storage.normalize_url(url)
    domain = urlsplit(norm).hostname or ""
    return storage.page_dir(domain, storage.url_to_slug(norm)) / dir_name


def test_ingest_page_creates_extension_snapshot(root):
    """페이지 적재 — origin=extension, client_captured=1, 색인·로그·디스크 산출물."""
    with db.connect() as conn:
        uid = db.create_user(conn, "ext@example.com", password_hash="x", role="archiver")
    res = ingest.ingest_capture(url=URL, page_html=PAGE, raw_html=RAW, requested_by=uid)
    assert res.status == "new" and res.snapshot_id is not None
    with db.connect() as conn:
        snap = conn.execute("SELECT * FROM snapshots WHERE id=?", (res.snapshot_id,)).fetchone()
        page = conn.execute("SELECT * FROM pages WHERE id=?", (res.page_id,)).fetchone()
        log = conn.execute("SELECT * FROM archive_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert snap["origin"] == "extension" and snap["incomplete"] == 0
    assert snap["search_indexed"] == 1
    assert page["client_captured"] == 1
    assert log["source"] == "extension" and log["requested_by"] == uid and log["status"] == "new"
    d = _dir_for(URL, snap["dir_name"])
    assert (d / "page.html.gz").is_file()
    assert (d / "raw.html.gz").is_file()
    assert "제목" in (d / "content.md").read_text(encoding="utf-8")


def test_ingest_dedup_unchanged_then_force(root, monkeypatch):
    """같은 내용 재적재 → unchanged(check만), force → 강제 저장."""
    _use_advancing_clock(monkeypatch)
    a = ingest.ingest_capture(url=URL, page_html=PAGE, raw_html=RAW)
    b = ingest.ingest_capture(url=URL, page_html=PAGE, raw_html=RAW)
    assert a.status == "new"
    assert b.status == "unchanged" and b.snapshot_id == a.snapshot_id
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM checks").fetchone()["c"] == 1
    c = ingest.ingest_capture(url=URL, page_html=PAGE, raw_html=RAW, force=True)
    assert c.status == "forced_same"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"] == 2


def test_ingest_externalizes_only_whitelisted_resource(root):
    """큰 image 데이터 URI 는 CAS 외부화, 비화이트리스트(text/html)는 인라인 유지(원칙 5)."""
    png = base64.b64encode(b"\x89PNG\r\n" + b"x" * 6000).decode()
    htmldata = base64.b64encode(b"<b>" + b"y" * 6000).decode()
    page = (
        f'<!DOCTYPE html><html><body><img src="data:image/png;base64,{png}">'
        f'<iframe src="data:text/html;base64,{htmldata}"></iframe></body></html>'
    )
    res = ingest.ingest_capture(url=URL, page_html=page, raw_html=RAW)
    with db.connect() as conn:
        names = [r["name"] for r in conn.execute(
            "SELECT name FROM snapshot_resources WHERE snapshot_id=?", (res.snapshot_id,))]
    assert len(names) == 1 and names[0].endswith(".png")  # text/html 은 외부화 안 됨
    assert resources.resource_path(names[0]).is_file()


def test_ingest_loopback_rejected(root):
    """루프백 호스트는 하드 거부 (ValueError)."""
    with pytest.raises(ValueError):
        ingest.ingest_capture(url="http://localhost:9000/x", page_html=PAGE, raw_html=RAW)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"] == 0


def test_ingest_private_requires_tag_then_ok(root, monkeypatch):
    """사설 호스트는 태그 없으면 NetworkTagRequired, 태그 주면 연결되어 저장."""
    monkeypatch.setattr(netcheck, "classify_host", lambda h: netcheck.PRIVATE)
    purl = "https://intranet.example/x"
    with pytest.raises(ingest.NetworkTagRequired):
        ingest.ingest_capture(url=purl, page_html=PAGE, raw_html=RAW)
    with db.connect() as conn:
        tag = db.create_network_tag(conn, "사무실")
    res = ingest.ingest_capture(url=purl, page_html=PAGE, raw_html=RAW, network_tag=tag["id"])
    assert res.status == "new"
    with db.connect() as conn:
        page = conn.execute("SELECT network_tag_id FROM pages WHERE id=?", (res.page_id,)).fetchone()
    assert page["network_tag_id"] == tag["id"]


def test_ingest_documents_stored_and_filtered(root):
    """업로드 문서 — 화이트리스트 PDF 는 CAS 저장, 비화이트리스트(.exe)는 제외(원칙 5)."""
    pdf = ingest.IngestDocument("https://example.com/a.pdf", "a.pdf",
                                "application/pdf", b"%PDF-1.4 hello")
    evil = ingest.IngestDocument("https://example.com/x.exe", "x.exe",
                                 "application/octet-stream", b"MZ\x90\x00")
    res = ingest.ingest_capture(url=URL, page_html=PAGE, raw_html=RAW, documents_in=[pdf, evil])
    with db.connect() as conn:
        docs = conn.execute(
            "SELECT * FROM snapshot_documents WHERE snapshot_id=?", (res.snapshot_id,)).fetchall()
    assert len(docs) == 1 and docs[0]["content_type"] == "application/pdf"
    name = documents.cas_name(docs[0]["sha256"], docs[0]["file"])
    assert name is not None and documents.cas_path(name).is_file()


def test_ingest_document_url_mode(root):
    """URL 자체가 문서 — 문서 스냅샷(안내 page.html, raw.html 없음)."""
    doc = ingest.IngestDocument("https://example.com/report.pdf", "report.pdf",
                                "application/pdf", b"%PDF-1.4 report body")
    res = ingest.ingest_document(url="https://example.com/report.pdf", document=doc)
    assert res.status == "new"
    with db.connect() as conn:
        snap = conn.execute("SELECT * FROM snapshots WHERE id=?", (res.snapshot_id,)).fetchone()
        ndoc = conn.execute(
            "SELECT COUNT(*) c FROM snapshot_documents WHERE snapshot_id=?", (res.snapshot_id,)
        ).fetchone()["c"]
    assert snap["origin"] == "extension" and ndoc == 1
    d = _dir_for("https://example.com/report.pdf", snap["dir_name"])
    assert (d / "page.html.gz").is_file()       # 안내 페이지
    assert not (d / "raw.html.gz").exists()      # 문서는 raw 없음


def test_client_captured_blocks_enqueue(root):
    """확장 캡처 후 그 URL 의 단발 큐 등록은 거부된다 (불변식 — 서버 재요청 차단)."""
    ingest.ingest_capture(url=URL, page_html=PAGE, raw_html=RAW)
    norm = storage.normalize_url(URL)
    with db.connect() as conn:
        assert db.enqueue_archive_job(conn, norm, source="cli") is False
        assert conn.execute("SELECT COUNT(*) c FROM archive_jobs").fetchone()["c"] == 0


def test_client_captured_blocks_pipeline_archive(root):
    """확장 캡처 후 pipeline.archive_url 은 캡처 전에 차단한다 (스케줄·크롤·워커 공통 백스톱)."""
    ingest.ingest_capture(url=URL, page_html=PAGE, raw_html=RAW)
    with pytest.raises(ValueError, match="확장"):
        pipeline.archive_url(URL, source="schedule")


def test_ingest_incomplete_and_capture_env_in_meta(root):
    """incomplete 플래그 → DB·meta, capture_env → meta.json."""
    res = ingest.ingest_capture(
        url=URL, page_html=PAGE, raw_html=RAW, incomplete=True,
        capture_env={"viewport_w": 1920, "viewport_h": 1080, "dpr": 2},
    )
    with db.connect() as conn:
        snap = conn.execute("SELECT * FROM snapshots WHERE id=?", (res.snapshot_id,)).fetchone()
    assert snap["incomplete"] == 1
    meta = json.loads((_dir_for(URL, snap["dir_name"]) / "meta.json").read_text(encoding="utf-8"))
    assert meta["origin"] == "extension" and meta["incomplete"] is True
    assert meta["capture_env"]["dpr"] == 2
