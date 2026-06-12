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
