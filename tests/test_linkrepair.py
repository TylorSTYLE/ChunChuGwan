"""아카이브 링크 교정 — 앵커 재작성(순수 함수)과 백필(스냅샷) 검증. 캡처 없음."""
import gzip

import pytest

from chunchugwan import config, db, linkrepair, storage

BASE = "https://example.com/post"


def test_rewrite_relative_anchor_to_resolver():
    html = '<a href="/products">상품</a>'
    out, n = linkrepair.rewrite_anchors(html, BASE)
    assert n == 1
    assert '/goto?url=https%3A%2F%2Fexample.com%2Fproducts' in out
    assert 'target="_top"' in out


def test_rewrite_document_relative_anchor():
    html = '<a href="other.html">다음</a>'
    out, n = linkrepair.rewrite_anchors(html, BASE)
    assert n == 1
    # example.com/post 기준 상대 → example.com/other.html
    assert '/goto?url=https%3A%2F%2Fexample.com%2Fother.html' in out


def test_rewrite_absolute_external_anchor():
    html = '<a href="https://other.test/x">외부</a>'
    out, n = linkrepair.rewrite_anchors(html, BASE)
    assert n == 1
    assert '/goto?url=https%3A%2F%2Fother.test%2Fx' in out


def test_skips_non_web_and_fragment_and_already_rewritten():
    html = (
        '<a href="#sec">앵커</a>'
        '<a href="mailto:a@b.c">메일</a>'
        '<a href="javascript:void(0)">js</a>'
        '<a href="/goto?url=https%3A%2F%2Fx.test%2F">이미</a>'
        '<a href="/crawl/10/goto?url=https%3A%2F%2Fx.test%2F">크롤</a>'
    )
    out, n = linkrepair.rewrite_anchors(html, BASE)
    assert n == 0
    assert out == html  # 아무것도 안 바뀜 (멱등)


def test_base_tag_removed_and_used_for_resolution():
    html = '<head><base href="https://root.test/"></head><a href="/p">x</a>'
    out, n = linkrepair.rewrite_anchors(html, BASE)
    assert n == 1
    # 상대 /p 는 <base> 기준 root.test 로 해석
    assert '/goto?url=https%3A%2F%2Froot.test%2Fp' in out
    assert "<base" not in out.lower()  # 재작성 후 base 제거


def test_existing_target_replaced_with_top():
    html = '<a href="/p" target="_blank">x</a>'
    out, _ = linkrepair.rewrite_anchors(html, BASE)
    assert 'target="_top"' in out
    assert 'target="_blank"' not in out


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """임시 아카이브 — 단일 페이지 스냅샷 1개(미교정 page.html.gz)."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")

    domain, slug = "example.com", storage.url_to_slug(BASE)
    dir_name = "2026-06-01T00-00-00"
    snap_dir = storage.page_dir(domain, slug) / dir_name
    snap_dir.mkdir(parents=True)
    html = '<html><body><a href="/next">다음</a><a href="#x">앵커</a></body></html>'
    (snap_dir / "page.html.gz").write_bytes(gzip.compress(html.encode("utf-8")))
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, BASE, domain, slug)
        db.insert_snapshot(
            conn, page_id,
            taken_at="2026-06-01T00:00:00+00:00", dir_name=dir_name,
            content_hash="x" * 64, final_url=BASE, http_status=200, changed=1,
            links_rewritten=0,  # 구형 단일 페이지 — 교정 대상
        )
    return snap_dir


def _read_page_html(snap_dir):
    return gzip.decompress((snap_dir / "page.html.gz").read_bytes()).decode("utf-8")


def test_backfill_rewrites_pending_snapshot(archive):
    assert linkrepair.pending_count() == 1
    rewritten = linkrepair.backfill_all()
    assert rewritten == 1
    out = _read_page_html(archive)
    assert '/goto?url=https%3A%2F%2Fexample.com%2Fnext' in out
    assert 'target="_top"' in out
    assert "#x" in out  # 프래그먼트 앵커는 보존
    # 플래그 갱신 → 더 이상 대상 아님
    assert linkrepair.pending_count() == 0


def test_backfill_idempotent(archive):
    linkrepair.backfill_all()
    first = _read_page_html(archive)
    # 다시 미교정으로 되돌려 재실행해도 추가 변경 없음(멱등)
    with db.connect() as conn:
        conn.execute("UPDATE snapshots SET links_rewritten = 0")
    rewritten = linkrepair.backfill_all()
    assert rewritten == 0  # 이미 /goto 라 재작성 건수 0
    assert _read_page_html(archive) == first
