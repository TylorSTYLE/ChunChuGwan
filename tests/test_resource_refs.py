"""스냅샷 자원 참조(snapshot_resources) — 기록, 삭제 GC, URL 폴백.

page.html 이 /resource/ CAS 로 참조하는 자원의 인덱스를 검증한다:
- 파이프라인이 캡처 시 참조 행(원본 URL 포함)을 기록하고,
- 스냅샷 삭제 시 참조 0 이 된 CAS 파일이 GC 되며,
- 자원 인라인 실패 시 같은 URL 의 과거 캡처본을 재사용한다.
"""
import base64
import hashlib

import pytest

from chunchugwan import capture, config, db, deletion, pipeline, resources, storage


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    """임시 아카이브 루트."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    return tmp_path


IMG_BYTES = b"P" * 5000
IMG_SHA = hashlib.sha256(IMG_BYTES).hexdigest()
IMG_NAME = IMG_SHA + ".png"
IMG_URL = "https://cdn.example.com/logo.png"


def _fake_capture(monkeypatch, body: str = "내용"):
    """캡처 모킹 — page.html 에 큰 이미지 data URI 를 남기고 URL 매핑을 돌려준다."""

    def fake(url, out_dir, remove_selectors=(), link_rewriter=None, session=None,
             resource_fallback=None, **kwargs):
        uri = f"data:image/png;base64,{base64.b64encode(IMG_BYTES).decode()}"
        html = f'<html><body><img src="{uri}">{body}</body></html>'
        (out_dir / "page.html").write_text(html, encoding="utf-8")
        (out_dir / "raw.html").write_text(html, encoding="utf-8")
        return capture.CaptureResult(
            final_url=url, http_status=200, title="제목",
            raw_html=html, content_html=f"<html><body>{body}</body></html>",
            resource_urls={IMG_SHA: IMG_URL},
        )

    monkeypatch.setattr(pipeline.capture, "capture", fake)


# ---- 기록 ----


def test_pipeline_records_resource_refs(archive_env, monkeypatch):
    _fake_capture(monkeypatch)
    outcome = pipeline.archive_url("https://example.com/a")
    assert outcome.status == "new"
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM snapshot_resources").fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == IMG_NAME
    assert rows[0]["url"] == IMG_URL
    assert resources.resource_path(IMG_NAME).read_bytes() == IMG_BYTES


def test_pipeline_forwards_mobile_screenshot_setting(archive_env, monkeypatch):
    """시스템 '캡처 설정'이 켜지면 pipeline 이 capture 에 mobile_screenshot 를
    전달하고, 찍힌 모바일 스크린샷이 압축(WebP)·확정을 거쳐 스냅샷에 남는다.

    pipeline → capture_kwargs → compact_snapshot_dir → finalize_snapshot 전체
    경로를 검증한다 (실제 브라우저는 capture 단위 테스트가 본다)."""
    from PIL import Image

    seen: dict[str, bool] = {}

    def fake(url, out_dir, mobile_screenshot=False, **kwargs):
        seen["mobile"] = mobile_screenshot
        html = "<html><body>본문</body></html>"
        (out_dir / "page.html").write_text(html, encoding="utf-8")
        (out_dir / "raw.html").write_text(html, encoding="utf-8")
        Image.new("RGB", (8, 8), (10, 20, 30)).save(out_dir / "screenshot.png")
        if mobile_screenshot:
            Image.new("RGB", (8, 16), (40, 50, 60)).save(
                out_dir / "screenshot-mobile.png"
            )
        return capture.CaptureResult(
            final_url=url, http_status=200, title="제목",
            raw_html=html, content_html=html,
        )

    monkeypatch.setattr(pipeline.capture, "capture", fake)

    # 기본(off): 모바일 스크린샷을 요청하지 않고, 산출물도 없다
    out = pipeline.archive_url("https://example.com/a")
    assert seen["mobile"] is False
    assert storage.find_screenshot(out.snapshot_dir) is not None
    assert storage.find_mobile_screenshot(out.snapshot_dir) is None

    # 설정 on: pipeline 이 mobile_screenshot=True 를 전달 → screenshot-mobile.webp 확정
    with db.connect() as conn:
        db.set_setting(conn, db.MOBILE_SCREENSHOT_ENABLED_KEY, "on")
    out2 = pipeline.archive_url("https://example.com/b")
    assert seen["mobile"] is True
    mobile = storage.find_mobile_screenshot(out2.snapshot_dir)
    assert mobile is not None and mobile.name == "screenshot-mobile.webp"


def test_insert_refs_idempotent(archive_env):
    with db.connect() as conn:
        page_id = db.get_or_create_page(
            conn, "https://example.com/a", "example.com",
            storage.url_to_slug("https://example.com/a"),
        )
        snap_id = db.insert_snapshot(
            conn, page_id, taken_at="2026-06-01T00:00:00+00:00",
            dir_name="2026-06-01T00-00-00", content_hash="0" * 64,
            final_url="https://example.com/a", http_status=200, changed=1,
        )
        db.insert_snapshot_resources(
            conn, snap_id, [{"name": IMG_NAME, "url": IMG_URL}]
        )
        db.insert_snapshot_resources(conn, snap_id, [{"name": IMG_NAME, "url": None}])
        rows = conn.execute("SELECT * FROM snapshot_resources").fetchall()
        assert len(rows) == 1
        assert db.find_resource_by_url(conn, IMG_URL) == IMG_NAME
        assert db.find_resource_by_url(conn, "https://no.such/x.png") is None


# ---- 삭제 GC ----


def test_resource_gc_on_delete(archive_env, monkeypatch):
    _fake_capture(monkeypatch, body="첫 페이지")
    pipeline.archive_url("https://example.com/a")
    _fake_capture(monkeypatch, body="둘째 페이지")  # 같은 이미지를 공유하는 다른 페이지
    pipeline.archive_url("https://example.com/b")
    assert resources.resource_path(IMG_NAME).is_file()

    with db.connect() as conn:
        page_a = db.get_page(conn, "https://example.com/a")
    deletion.delete_page(page_a["id"])
    # 다른 스냅샷이 아직 참조 — CAS 파일 유지
    assert resources.resource_path(IMG_NAME).is_file()

    with db.connect() as conn:
        page_b = db.get_page(conn, "https://example.com/b")
    deletion.delete_page(page_b["id"])
    # 참조 0 — CAS 파일 GC
    assert not resources.resource_path(IMG_NAME).exists()


def test_resource_gc_on_snapshot_delete(archive_env, monkeypatch):
    _fake_capture(monkeypatch)
    outcome = pipeline.archive_url("https://example.com/a")
    deletion.delete_snapshot(outcome.snapshot_id)
    assert not resources.resource_path(IMG_NAME).exists()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM snapshot_resources").fetchone()["c"] == 0


# ---- URL 폴백 (이전 캡처본 재사용) ----


def test_resource_fallback_returns_cas_content(archive_env, monkeypatch):
    _fake_capture(monkeypatch)
    pipeline.archive_url("https://example.com/a")
    content_type, body = pipeline._resource_fallback(IMG_URL)
    assert content_type == "image/png"
    assert body == IMG_BYTES
    assert pipeline._resource_fallback("https://no.such/x.png") is None


def test_inline_fallback_reuses_previous_capture(archive_env, monkeypatch):
    """fetch·재시도 모두 실패한 자원이 과거 캡처본으로 메워진다."""
    resources._store(IMG_BYTES, ".png")  # 과거 캡처본이 CAS 에 존재

    class FakePage:
        url = "https://example.com/a"

        def __init__(self):
            self.applied: list[dict] = []

        def evaluate(self, js, arg=None):
            if "inlined" in js:  # _INLINE_JS
                return {"failed": [{"kind": "img", "url": IMG_URL}], "inlined": []}
            self.applied += arg  # _APPLY_INLINE_JS
            return None

        def content(self):
            return "<html></html>"

    monkeypatch.setattr(capture, "_fetch_via_context", lambda page, url, **kw: None)
    page = FakePage()
    html, resource_urls = capture._inline_resources(
        page, "<html></html>",
        resource_fallback=lambda url: ("image/png", IMG_BYTES) if url == IMG_URL else None,
    )
    assert resource_urls == {IMG_SHA: IMG_URL}
    assert len(page.applied) == 1
    assert page.applied[0]["kind"] == "img"
    assert page.applied[0]["dataUrl"].startswith("data:image/png;base64,")


def test_inline_fallback_miss_keeps_original_url(archive_env, monkeypatch):
    class FakePage:
        url = "https://example.com/a"

        def evaluate(self, js, arg=None):
            if "inlined" in js:
                return {"failed": [{"kind": "img", "url": IMG_URL}], "inlined": []}
            raise AssertionError("치환할 것이 없어야 한다")

        def content(self):
            return "<html></html>"

    monkeypatch.setattr(capture, "_fetch_via_context", lambda page, url, **kw: None)
    html, resource_urls = capture._inline_resources(
        FakePage(), "<html></html>", resource_fallback=lambda url: None
    )
    assert resource_urls == {}
