"""아카이브 전문 검색(searchindex.py + db FTS5) 테스트.

네트워크 의존 없이, 스냅샷 행 + content.md 파일을 직접 만들어 색인·검색·
삭제 동기화·백필·문서 본문 추출·쿼리 안전성을 검증한다.
"""
from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from chunchugwan import config, db, deletion, doctext, documents, searchindex, storage


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """빈 임시 아카이브 — config 경로를 tmp 로 돌린다."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    db.invalidate_schema_cache()
    with db.connect():
        pass
    yield tmp_path
    db.invalidate_schema_cache()


def _make_snapshot(url: str, content: str, *, title: str | None = None,
                   taken_at: str = "2026-06-01T00:00:00+00:00", index: bool = True) -> int:
    """페이지+스냅샷 행과 content.md/meta.json 파일을 만들고 snapshot_id 반환."""
    domain = storage.normalize_url(url).split("/")[2]
    slug = storage.url_to_slug(storage.normalize_url(url))
    norm = storage.normalize_url(url)
    dir_name = taken_at.replace(":", "-").replace("+00-00", "")[:19]
    snap_dir = storage.page_dir(domain, slug) / dir_name
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "content.md").write_text(content, encoding="utf-8")
    storage.write_meta(snap_dir, storage.SnapshotMeta(
        url=norm, final_url=norm, taken_at=taken_at,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        http_status=200, title=title, documents=None,
    ))
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, norm, domain, slug)
        snapshot_id = db.insert_snapshot(
            conn, page_id, taken_at=taken_at, dir_name=dir_name,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            final_url=norm, http_status=200, changed=1,
        )
        if index:
            searchindex.index_snapshot(conn, snapshot_id)
    return snapshot_id


# ---- 색인 가용성 ----


def test_fts5_available(archive):
    assert searchindex.available() is True


# ---- 한국어 부분문자열 검색 (trigram) ----


def test_korean_substring_match(archive):
    sid = _make_snapshot("https://a.com/1", "대한민국 헌법 제1조 대한민국은 민주공화국이다")
    res = searchindex.search("한민국")  # 조사 결합 무시 부분문자열
    assert res.mode == "fts"
    assert res.total == 1
    assert res.hits[0].snapshot_id == sid
    assert "한민국" in res.hits[0].snippet


def test_no_match_returns_empty(archive):
    _make_snapshot("https://a.com/1", "전혀 다른 내용")
    res = searchindex.search("존재하지않는단어")
    assert res.total == 0
    assert res.hits == []


def test_phrase_and_semantics(archive):
    _make_snapshot("https://a.com/1", "검색엔진 구현 문서")
    _make_snapshot("https://a.com/2", "검색엔진 소개")
    # 두 토큰 모두 3+ → AND
    assert searchindex.search("검색엔진 구현").total == 1
    assert searchindex.search("검색엔진").total == 2


# ---- 짧은 쿼리 LIKE 폴백 ----


def test_short_query_like_fallback(archive):
    _make_snapshot("https://a.com/1", "세금 정책 발표")
    res = searchindex.search("세금")  # 2글자 → trigram 불가 → LIKE
    assert res.mode == "like"
    assert res.total == 1


def test_short_query_finds_what_fts_cannot(archive):
    _make_snapshot("https://a.com/1", "법")
    assert searchindex.search("법").total == 1  # 1글자 LIKE


# ---- 필터 ----


def test_domain_filter(archive):
    _make_snapshot("https://a.com/1", "공통키워드 내용")
    _make_snapshot("https://b.com/1", "공통키워드 자료")
    assert searchindex.search("공통키워드").total == 2
    assert searchindex.search("공통키워드", domain="b.com").total == 1


def test_latest_only(archive):
    _make_snapshot("https://a.com/1", "키워드포함 첫 스냅샷",
                   taken_at="2026-06-01T00:00:00+00:00")
    _make_snapshot("https://a.com/1", "키워드포함 둘째 스냅샷",
                   taken_at="2026-06-02T00:00:00+00:00")
    assert searchindex.search("키워드포함").total == 2
    assert searchindex.search("키워드포함", latest_only=True).total == 1


# ---- 삭제 동기화 ----


def test_delete_snapshot_removes_from_index(archive):
    sid = _make_snapshot("https://a.com/1", "삭제대상 본문 내용")
    assert searchindex.search("삭제대상").total == 1
    deletion.delete_snapshot(sid)
    assert searchindex.search("삭제대상").total == 0


def test_delete_page_removes_from_index(archive):
    _make_snapshot("https://a.com/1", "페이지삭제 키워드")
    with db.connect() as conn:
        page = db.get_page(conn, storage.normalize_url("https://a.com/1"))
    deletion.delete_page(page["id"])
    assert searchindex.search("페이지삭제").total == 0


# ---- 백필 / 재색인 ----


def test_backfill_indexes_unindexed(archive):
    _make_snapshot("https://a.com/1", "백필대상 본문", index=False)
    assert searchindex.search("백필대상").total == 0
    assert searchindex.pending_count() == 1
    assert searchindex.backfill_all() == 1
    assert searchindex.pending_count() == 0
    assert searchindex.search("백필대상").total == 1


def test_reindex_all_is_idempotent(archive):
    _make_snapshot("https://a.com/1", "재색인 본문")
    searchindex.reindex_all()
    assert searchindex.search("재색인").total == 1
    searchindex.reindex_all()
    assert searchindex.search("재색인").total == 1


# ---- 쿼리 안전성 (FTS 구문 주입) ----


@pytest.mark.parametrize("q", ['시간: 9시', '"따옴표"검색', 'a:b:c 검색어', 'OR AND NOT'])
def test_query_injection_safe(archive, q):
    _make_snapshot("https://a.com/1", "평범한 본문 내용")
    # 어떤 입력도 OperationalError 없이 결과(0건 이상)를 돌려줘야 한다
    res = searchindex.search(q)
    assert res.total >= 0


# ---- 문서 본문 추출 + 색인 ----


def _make_docx_bytes(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
            f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        )
    return buf.getvalue()


def test_doctext_extracts_docx(tmp_path):
    p = tmp_path / "doc.docx"
    p.write_bytes(_make_docx_bytes("문서본문키워드 테스트"))
    text = doctext.extract_text(p)
    assert text and "문서본문키워드" in text


def test_doctext_unsupported_returns_none(tmp_path):
    p = tmp_path / "old.hwp"
    p.write_bytes(b"\xd0\xcf\x11\xe0binary")
    assert doctext.extract_text(p) is None


def test_document_body_is_searchable(archive):
    """첨부 문서(docx)의 본문이 검색 색인에 포함된다."""
    blob = _make_docx_bytes("첨부문서고유어 본문")
    sha = hashlib.sha256(blob).hexdigest()
    name = sha + ".docx"
    cas = config.DOCUMENTS_DIR / name[:2] / name
    cas.parent.mkdir(parents=True, exist_ok=True)
    cas.write_bytes(blob)
    # 페이지 본문에는 없는 단어가 문서 본문에만 있다
    domain, slug, norm = "a.com", storage.url_to_slug("https://a.com/d"), storage.normalize_url("https://a.com/d")
    dir_name = "2026-06-01T00-00-00"
    snap_dir = storage.page_dir(domain, slug) / dir_name
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "content.md").write_text("페이지 본문", encoding="utf-8")
    storage.write_meta(snap_dir, storage.SnapshotMeta(
        url=norm, final_url=norm, taken_at="2026-06-01T00:00:00+00:00",
        content_hash="0" * 64, http_status=200, title="문서첨부", documents=None))
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, norm, domain, slug)
        sid = db.insert_snapshot(
            conn, page_id, taken_at="2026-06-01T00:00:00+00:00", dir_name=dir_name,
            content_hash="0" * 64, final_url=norm, http_status=200, changed=1)
        db.insert_snapshot_documents(conn, sid, [{
            "url": "https://a.com/d.docx", "file": "report.docx",
            "bytes": len(blob), "sha256": sha,
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }])
        searchindex.index_snapshot(conn, sid)
    assert searchindex.search("첨부문서고유어").total == 1


# ---- 정합성 점검 / 교정 (verify / repair) ----


def test_verify_clean(archive):
    _make_snapshot("https://a.com/1", "정상 본문")
    r = searchindex.verify()
    assert r.available and r.consistent
    assert r.indexed == 1 and r.fts_rows == 1 and r.missing == 0 and r.orphan == 0


def test_verify_detects_missing(archive):
    """search_indexed=1 인데 FTS 행이 없는 '거짓말 플래그' 를 잡는다."""
    sid = _make_snapshot("https://a.com/1", "본문", index=False)
    with db.connect() as conn:
        db.mark_snapshot_search_indexed(conn, sid)  # 플래그만 1, FTS 행 없음
    r = searchindex.verify()
    assert r.missing == 1 and not r.consistent


def test_verify_detects_orphan(archive):
    """대응 스냅샷이 없는 FTS 행(orphan)을 잡는다."""
    _make_snapshot("https://a.com/1", "본문")
    with db.connect() as conn:
        db.upsert_snapshot_fts(conn, 9999, "고아 본문", None, "u")
    r = searchindex.verify()
    assert r.orphan == 1 and not r.consistent


def test_repair_fixes_missing_and_orphan(archive):
    sid = _make_snapshot("https://a.com/1", "재색인대상 본문", index=False)
    with db.connect() as conn:
        db.mark_snapshot_search_indexed(conn, sid)        # missing
        db.upsert_snapshot_fts(conn, 9999, "고아", None, "u")  # orphan
    result = searchindex.repair()
    assert result.orphans_removed == 1 and result.reindexed >= 1
    r = searchindex.verify()
    assert r.consistent
    assert searchindex.search("재색인대상").total == 1


# ---- compact ↔ 인덱스 정합 (구형 files/ 문서 self-heal) ----


def test_compact_marks_legacy_document_stale_and_indexes_body(archive):
    """구형 files/ 문서를 가진 스냅샷은 compact 후 다시 색인 대상이 되고,
    재색인하면 문서 본문이 검색된다 (compact ↔ 인덱스 정합)."""
    blob = _make_docx_bytes("구형문서본문어 내용")
    norm = storage.normalize_url("https://a.com/legacy")
    domain, slug, dir_name = "a.com", storage.url_to_slug(norm), "2026-06-01T00-00-00"
    snap_dir = storage.page_dir(domain, slug) / dir_name
    (snap_dir / "files").mkdir(parents=True, exist_ok=True)
    (snap_dir / "content.md").write_text("페이지 본문만", encoding="utf-8")
    (snap_dir / "files" / "old.docx").write_bytes(blob)
    storage.write_meta(snap_dir, storage.SnapshotMeta(
        url=norm, final_url=norm, taken_at="2026-06-01T00:00:00+00:00",
        content_hash="0" * 64, http_status=200, title=None,
        documents=[{"url": "https://a.com/old.docx", "file": "old.docx",
                    "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest(),
                    "content_type": "application/octet-stream"}]))
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, norm, domain, slug)
        sid = db.insert_snapshot(
            conn, page_id, taken_at="2026-06-01T00:00:00+00:00", dir_name=dir_name,
            content_hash="0" * 64, final_url=norm, http_status=200, changed=1)
        searchindex.index_snapshot(conn, sid)  # 색인됨 (문서 본문은 아직 files/ 라 없음)
    # 색인됐지만 문서 본문은 안 잡힌다
    assert searchindex.search("구형문서본문어").total == 0
    with db.connect() as conn:
        assert db.get_snapshot(conn, sid)["search_indexed"] == 1
    # compact: files/ → CAS 이전 + snapshot_documents 행 + search_indexed=0 표시
    documents.compact_legacy_documents()
    with db.connect() as conn:
        assert db.get_snapshot(conn, sid)["search_indexed"] == 0  # self-heal 표시
    assert searchindex.pending_count() == 1
    # 재색인하면 문서 본문이 검색된다
    searchindex.backfill_all()
    assert searchindex.search("구형문서본문어").total == 1


# ---- 시스템 메뉴 전체 다시 색인 버튼 ----


def test_system_reindex_button(archive, monkeypatch):
    from fastapi.testclient import TestClient
    from chunchugwan.web import app as web_app
    monkeypatch.setattr(config, "AUTH_ENABLED", False)  # loopback — 관리자 허용
    _make_snapshot("https://a.com/1", "버튼색인 본문", index=False)
    assert searchindex.pending_count() == 1
    client = TestClient(web_app.app)
    res = client.post("/system/search/reindex", follow_redirects=False)
    assert res.status_code == 303
    assert searchindex.pending_count() == 0
    assert searchindex.search("버튼색인").total == 1
