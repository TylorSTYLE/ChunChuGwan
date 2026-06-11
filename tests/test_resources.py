"""공유 자원 CAS 추출(resources.py)과 스냅샷 압축 변환 테스트."""
import base64
import gzip
import hashlib

import pytest
from PIL import Image

from chunchugwan import config, resources


@pytest.fixture
def cas_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "RESOURCE_MIN_BYTES", 16)  # 테스트용 소형 임계값
    return tmp_path


def _data_uri(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def test_externalize_replaces_with_resource_ref(cas_env):
    data = b"P" * 64
    html = f'<img src="{_data_uri(data)}">'
    out, count = resources.externalize_data_uris(html)
    assert count == 1
    name = hashlib.sha256(data).hexdigest() + ".png"
    assert f'/resource/{name}' in out
    assert "base64" not in out
    assert resources.resource_path(name).read_bytes() == data


def test_externalize_keeps_small_and_unknown_mime(cas_env):
    small = _data_uri(b"x" * 4)                            # 임계값 미만
    html_doc = _data_uri(b"<script>evil</script>" * 10, "text/html")  # 문서 타입 금지
    html = f'<img src="{small}"><iframe src="{html_doc}"></iframe>'
    out, count = resources.externalize_data_uris(html)
    assert count == 0
    assert out == html
    assert not config.RESOURCES_DIR.exists()


def test_externalize_dedups_same_content(cas_env):
    uri = _data_uri(b"F" * 100, "font/woff2")
    out, count = resources.externalize_data_uris(f"url({uri}) url({uri})")
    assert count == 2
    files = list(config.RESOURCES_DIR.glob("*/*"))
    assert len(files) == 1 and files[0].suffix == ".woff2"


def test_externalize_strips_base_tag(cas_env):
    html = f'<base href="https://evil.example/"><img src="{_data_uri(b"i" * 64)}">'
    out, count = resources.externalize_data_uris(html)
    assert count == 1
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


def test_compact_keeps_png_when_conversion_fails(cas_env, tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "screenshot.png").write_bytes(b"\x89PNG not really")
    stats = resources.compact_snapshot_dir(snap)
    assert (snap / "screenshot.png").is_file()      # 폴백: 원본 유지
    assert not (snap / "screenshot.webp").exists()
    assert stats.before_bytes == stats.after_bytes == 0
