"""공유 자원 CAS 추출(resources.py)과 스냅샷 압축 변환 테스트."""
import base64
import gzip
import hashlib

import pytest
from PIL import Image

from chunchugwan import config, resources, storage


@pytest.fixture
def cas_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "RESOURCE_MIN_BYTES", 16)  # 테스트용 소형 임계값
    return tmp_path


def _data_uri(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def test_externalize_replaces_with_resource_ref(cas_env):
    data = b"P" * 64
    html = f'<img src="{_data_uri(data)}">'
    out, names = resources.externalize_data_uris(html)
    name = hashlib.sha256(data).hexdigest() + ".png"
    assert names == [name]
    assert f'/resource/{name}' in out
    assert "base64" not in out
    assert resources.resource_path(name).read_bytes() == data


def test_externalize_keeps_small_and_unknown_mime(cas_env):
    small = _data_uri(b"x" * 4)                            # 임계값 미만
    html_doc = _data_uri(b"<script>evil</script>" * 10, "text/html")  # 문서 타입 금지
    html = f'<img src="{small}"><iframe src="{html_doc}"></iframe>'
    out, names = resources.externalize_data_uris(html)
    assert names == []
    assert out == html
    assert not config.RESOURCES_DIR.exists()


def test_externalize_dedups_same_content(cas_env):
    uri = _data_uri(b"F" * 100, "font/woff2")
    out, names = resources.externalize_data_uris(f"url({uri}) url({uri})")
    assert len(names) == 1  # 같은 내용은 이름도 하나 (참조 기록용 중복 제거)
    assert out.count(f"/resource/{names[0]}") == 2
    files = list(config.RESOURCES_DIR.glob("*/*"))
    assert len(files) == 1 and files[0].suffix == ".woff2"


def test_externalize_strips_base_tag(cas_env):
    html = f'<base href="https://evil.example/"><img src="{_data_uri(b"i" * 64)}">'
    out, names = resources.externalize_data_uris(html)
    assert len(names) == 1
    assert "<base" not in out  # base href 가 /resource/ 참조를 깨지 않게 제거


# ---- 인라인 <style> 추출 ----

_CSS = "body { color: #fff; background: #000; }"  # 16바이트 임계값 이상


def _css_name(css: str = _CSS) -> str:
    return hashlib.sha256(css.encode()).hexdigest() + ".css"


def test_externalize_style_block(cas_env):
    html = f"<html><head><style>{_CSS}</style></head><body>본문</body></html>"
    out, names = resources.externalize_style_blocks(html)
    assert names == [_css_name()]
    assert f'<link rel="stylesheet" href="/resource/{names[0]}">' in out
    assert "<style" not in out
    # 본체는 gzip 저장 (이름은 원문 sha256) — /resource/ 가 Content-Encoding 으로 서빙
    stored = resources.resource_path(names[0]).read_bytes()
    assert gzip.decompress(stored) == _CSS.encode()
    assert resources.is_gzipped(resources.resource_path(names[0]))


def test_store_css_skips_recompress_when_present(cas_env, monkeypatch):
    """이미 있는 CSS 는 gzip 재압축 없이 mtime 만 갱신한다 (재아카이빙 낭비 방지)."""
    data = _CSS.encode()
    name = resources._store_css(data)  # 최초 저장 — 압축 1회
    path = resources.resource_path(name)
    assert resources.is_gzipped(path)

    calls = []
    real_compress = gzip.compress
    monkeypatch.setattr(
        gzip, "compress",
        lambda *a, **k: calls.append(1) or real_compress(*a, **k),
    )
    old_mtime = path.stat().st_mtime
    import os
    os.utime(path, (old_mtime - 100, old_mtime - 100))  # 과거로 돌려 갱신 확인

    assert resources._store_css(data) == name
    assert calls == []                       # 재압축 안 함
    assert path.stat().st_mtime > old_mtime - 100  # mtime 은 갱신


def test_externalize_style_keeps_small(cas_env):
    html = "<style>p{}</style>"  # 임계값 미만
    out, names = resources.externalize_style_blocks(html)
    assert names == [] and out == html


def test_externalize_style_preserves_media(cas_env):
    html = f'<style media="print" data-x="1">{_CSS}</style>'
    out, names = resources.externalize_style_blocks(html)
    assert f'<link rel="stylesheet" href="/resource/{names[0]}" media="print">' in out


def test_externalize_style_skips_relative_refs_without_base(cas_env):
    # base_url 없이는 상대 url()/@import 의 해석 기준(<style>=문서,
    # 외부 .css=/resource/)이 달라지므로 인라인을 유지한다
    for ref in ("url(../bg.png)", "url('img/a.png')", '@import "common.css";'):
        html = f"<style>{_CSS} .x {{ {ref} }}</style>"
        out, names = resources.externalize_style_blocks(html)
        assert names == [] and out == html, ref
    # 위치 무관 참조(절대 URL·data:·#·/resource/)만 있으면 추출된다
    safe = (
        f"{_CSS} .y {{ background: url(https://cdn.example/a.png) "
        f"url(data:image/png;base64,AAAA) url(/resource/{'a' * 64}.png) url(#f) }}"
    )
    out, names = resources.externalize_style_blocks(f"<style>{safe}</style>")
    assert len(names) == 1 and "<style" not in out


def test_externalize_style_absolutizes_with_base(cas_env):
    # final_url 이 주어지면 상대 참조를 원래 해석(페이지 기준)으로 절대화 후 추출
    css = f"{_CSS} .x {{ background: url(../image/bg.png) }} @import 'common.css';"
    out, names = resources.externalize_style_blocks(
        f"<style>{css}</style>", "https://example.com/sub/page.html"
    )
    assert len(names) == 1 and "<style" not in out
    stored = gzip.decompress(resources.resource_path(names[0]).read_bytes()).decode()
    assert "url(https://example.com/image/bg.png)" in stored
    assert "@import 'https://example.com/sub/common.css'" in stored
    # 같은 페이지의 다음 스냅샷도 같은 절대화 결과 → 같은 CAS 이름 (공유)
    again, names2 = resources.externalize_style_blocks(
        f"<style>{css}</style>", "https://example.com/sub/page.html"
    )
    assert names2 == names


def test_externalize_style_honors_base_tag(cas_env):
    # 문서에 <base href> 가 있으면 상대 참조는 그 기준으로 해석된다
    css = f"{_CSS} .x {{ background: url(img/a.png) }}"
    html = f'<base href="https://cdn.example/assets/"><style>{css}</style>'
    out, names = resources.externalize_style_blocks(html, "https://example.com/")
    stored = gzip.decompress(resources.resource_path(names[0]).read_bytes()).decode()
    assert "url(https://cdn.example/assets/img/a.png)" in stored
    assert "<base" not in out  # 치환 후 <base> 제거 (externalize_data_uris 와 동일)


def test_externalize_style_skips_svg(cas_env):
    svg_css = _CSS + "/* svg */"
    html = f"<svg><style>{svg_css}</style></svg><style>{_CSS}</style>"
    out, names = resources.externalize_style_blocks(html)
    assert names == [_css_name()]  # <svg> 안의 블록은 그대로
    assert svg_css in out and out.count("<style") == 1


def test_externalize_style_dedups_and_strips_base(cas_env):
    html = (
        '<base href="https://evil.example/">'
        f"<style>{_CSS}</style><style>{_CSS}</style>"
    )
    out, names = resources.externalize_style_blocks(html)
    assert len(names) == 1  # 같은 내용은 한 번만 저장
    assert out.count(f"/resource/{names[0]}") == 2
    assert "<base" not in out


def test_compact_extracts_style_blocks(cas_env, tmp_path):
    # 폰트 data URI 가 든 <style> — data URI 추출 후 /resource/ 참조가 되어
    # 위치 무관 형태로 바뀌므로 스타일 블록도 추출된다
    font = _data_uri(b"F" * 100, "font/woff2")
    css = f"{_CSS} @font-face {{ src: url({font}); }}"
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "page.html").write_text(
        f"<html><style>{css}</style>본문</html>", encoding="utf-8"
    )
    stats = resources.compact_snapshot_dir(snap)
    assert stats.externalized == 2  # 폰트 + 스타일
    page = gzip.decompress((snap / "page.html.gz").read_bytes()).decode("utf-8")
    assert "<style" not in page and '<link rel="stylesheet"' in page
    assert len(stats.resource_names) == 2
    assert {n[64:] for n in stats.resource_names} == {".woff2", ".css"}


def test_valid_name():
    h = "a" * 64
    assert resources.is_valid_name(h + ".png")
    assert not resources.is_valid_name(h + ".html")   # 문서 타입 서빙 금지
    assert not resources.is_valid_name("../" + h + ".png")
    assert not resources.is_valid_name(h.upper() + ".png")
    assert not resources.is_valid_name("short.png")


def test_compact_snapshot_dir(cas_env, tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    data = b"R" * 64
    (snap / "page.html").write_text(
        f'<html><img src="{_data_uri(data)}">본문</html>', encoding="utf-8"
    )
    (snap / "raw.html").write_text("<html>원본</html>", encoding="utf-8")
    Image.new("RGB", (8, 8), (200, 0, 0)).save(snap / "screenshot.png")
    (snap / "content.md").write_text("본문", encoding="utf-8")

    stats = resources.compact_snapshot_dir(snap)
    assert stats.externalized == 1
    assert stats.before_bytes > 0

    # 원본은 압축/변환본으로 교체된다
    assert not (snap / "page.html").exists()
    assert not (snap / "raw.html").exists()
    assert not (snap / "screenshot.png").exists()
    page = gzip.decompress((snap / "page.html.gz").read_bytes()).decode("utf-8")
    assert "/resource/" in page and "본문" in page
    assert gzip.decompress((snap / "raw.html.gz").read_bytes()) == "<html>원본</html>".encode()
    with Image.open(snap / "screenshot.webp") as im:
        assert im.format == "WEBP" and im.size == (8, 8)
    assert (snap / "content.md").is_file()  # content.md 는 그대로

    # 멱등 — 이미 변환된 디렉토리는 건드릴 것이 없다
    again = resources.compact_snapshot_dir(snap)
    assert again.before_bytes == 0 and again.externalized == 0


def test_compact_converts_mobile_screenshot(cas_env, tmp_path):
    """모바일 스크린샷(screenshot-mobile.png)도 데스크탑과 같이 WebP 로 변환된다."""
    snap = tmp_path / "snap"
    snap.mkdir()
    Image.new("RGB", (8, 8), (200, 0, 0)).save(snap / "screenshot.png")
    Image.new("RGB", (8, 16), (0, 100, 200)).save(snap / "screenshot-mobile.png")

    resources.compact_snapshot_dir(snap)

    assert not (snap / "screenshot.png").exists()
    assert not (snap / "screenshot-mobile.png").exists()
    with Image.open(snap / "screenshot.webp") as im:
        assert im.format == "WEBP" and im.size == (8, 8)
    with Image.open(snap / "screenshot-mobile.webp") as im:
        assert im.format == "WEBP" and im.size == (8, 16)
    assert not resources.needs_compaction(snap)


def test_compact_keeps_mobile_png_independently(cas_env, tmp_path):
    """모바일 PNG 만 변환 불가여도 데스크탑은 변환되고, 각자 마커로 관리된다."""
    snap = tmp_path / "snap"
    snap.mkdir()
    Image.new("RGB", (8, 8), (200, 0, 0)).save(snap / "screenshot.png")
    (snap / "screenshot-mobile.png").write_bytes(b"\x89PNG not really")

    resources.compact_snapshot_dir(snap)

    assert (snap / "screenshot.webp").is_file()             # 데스크탑은 변환됨
    assert (snap / "screenshot-mobile.png").is_file()       # 모바일은 폴백 유지
    assert (snap / storage.MOBILE_WEBP_SKIP_MARKER).is_file()
    assert not (snap / storage.WEBP_SKIP_MARKER).is_file()
    assert not resources.needs_compaction(snap)


def test_needs_compaction_and_count(cas_env, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    assert resources.compactable_count() == 0  # 스냅샷 자체가 없음

    base = tmp_path / "sites" / "example.com" / "post-abcd1234"
    legacy, compacted = base / "2026-06-01T00-00-00", base / "2026-06-02T00-00-00"
    for d in (legacy, compacted):
        d.mkdir(parents=True)
        (d / "meta.json").write_text("{}", encoding="utf-8")
    (legacy / "raw.html").write_text("<html>원본</html>", encoding="utf-8")
    (compacted / "raw.html.gz").write_bytes(gzip.compress(b"<html></html>"))

    assert resources.needs_compaction(legacy)
    assert not resources.needs_compaction(compacted)
    assert resources.compactable_count() == 1  # 구형 산출물이 남은 스냅샷만

    # WebP 변환을 안 하기로 확정한 PNG(마커 보유)는 대상으로 세지 않는다
    kept = base / "2026-06-03T00-00-00"
    kept.mkdir()
    (kept / "meta.json").write_text("{}", encoding="utf-8")
    (kept / "screenshot.png").write_bytes(b"\x89PNG huge")
    assert resources.needs_compaction(kept)
    (kept / storage.WEBP_SKIP_MARKER).touch()
    assert not resources.needs_compaction(kept)
    assert resources.compactable_count() == 1


def test_compact_keeps_png_when_conversion_fails(cas_env, tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "screenshot.png").write_bytes(b"\x89PNG not really")
    stats = resources.compact_snapshot_dir(snap)
    assert (snap / "screenshot.png").is_file()      # 폴백: 원본 유지
    assert not (snap / "screenshot.webp").exists()
    assert stats.before_bytes == stats.after_bytes == 0
    # 마커가 남아 압축 대상에서 빠지고, 재실행에도 변환을 재시도하지 않는다
    assert (snap / storage.WEBP_SKIP_MARKER).is_file()
    assert not resources.needs_compaction(snap)


def test_compact_keeps_png_when_webp_larger(cas_env, tmp_path, monkeypatch):
    snap = tmp_path / "snap"
    snap.mkdir()
    png = snap / "screenshot.png"
    Image.new("RGB", (8, 8), (200, 0, 0)).save(png)
    png_bytes = png.read_bytes()

    orig_save = Image.Image.save
    def bloated_save(self, fp, format=None, **kw):  # WebP 가 원본보다 커지는 상황 재현
        orig_save(self, fp, format, **kw)
        if format == "WEBP":
            fp.write_bytes(fp.read_bytes() + b"\0" * 4096)
    monkeypatch.setattr(Image.Image, "save", bloated_save)

    stats = resources.compact_snapshot_dir(snap)
    assert png.read_bytes() == png_bytes            # 원본 유지
    assert not (snap / "screenshot.webp").exists()  # 커진 변환본은 버린다
    assert stats.before_bytes == stats.after_bytes == 0
    assert (snap / storage.WEBP_SKIP_MARKER).is_file()
    assert not resources.needs_compaction(snap)
