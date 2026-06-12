"""URL 정규화/slug 테스트. M1에서 구현과 함께 통과시킬 것."""
import pytest
from chunchugwan import storage


@pytest.mark.parametrize("raw,expected", [
    ("HTTPS://Example.COM:443/a?b=2&a=1#frag", "https://example.com/a?a=1&b=2"),
    ("http://example.com/a?utm_source=x&q=1", "http://example.com/a?q=1"),
    ("https://example.com/", "https://example.com/"),
    # 스킴 생략 시 https:// 자동 보완
    ("example.com", "https://example.com/"),
    ("Example.COM/a?b=2&a=1#frag", "https://example.com/a?a=1&b=2"),
    ("localhost:8080/x", "https://localhost:8080/x"),
    ("//example.com/a", "https://example.com/a"),
    # SPA 라우팅 fragment(/ 포함)는 화면을 결정하므로 보존, 단순 앵커는 제거
    ("https://example.com/app#!/users/42", "https://example.com/app#!/users/42"),
    ("https://example.com/a#section-2", "https://example.com/a"),
    # 퍼센트 인코딩 표기 통일: 한글 원형과 %XX 인코딩형은 같은 URL
    (
        "https://www.weather.go.kr/w/index.do#dong/4148051000/경기%20파주시%20아동동/SCH/파주시청",
        "https://www.weather.go.kr/w/index.do"
        "#dong/4148051000/%EA%B2%BD%EA%B8%B0%20%ED%8C%8C%EC%A3%BC%EC%8B%9C%20%EC%95%84%EB%8F%99%EB%8F%99"
        "/SCH/%ED%8C%8C%EC%A3%BC%EC%8B%9C%EC%B2%AD",
    ),
    (
        "https://www.weather.go.kr/w/index.do"
        "#dong/4148051000/%EA%B2%BD%EA%B8%B0%20%ED%8C%8C%EC%A3%BC%EC%8B%9C%20%EC%95%84%EB%8F%99%EB%8F%99"
        "/SCH/%ED%8C%8C%EC%A3%BC%EC%8B%9C%EC%B2%AD",
        "https://www.weather.go.kr/w/index.do"
        "#dong/4148051000/%EA%B2%BD%EA%B8%B0%20%ED%8C%8C%EC%A3%BC%EC%8B%9C%20%EC%95%84%EB%8F%99%EB%8F%99"
        "/SCH/%ED%8C%8C%EC%A3%BC%EC%8B%9C%EC%B2%AD",
    ),
    ("https://example.com/글/하나", "https://example.com/%EA%B8%80/%ED%95%98%EB%82%98"),
    # 인코딩된 %2F 는 경로 구분자 '/' 와 구분 유지
    ("https://example.com/a%2Fb/c", "https://example.com/a%2Fb/c"),
])
def test_normalize_url(raw, expected):
    assert storage.normalize_url(raw) == expected


def test_normalize_url_encoding_variants_equal():
    """원형 한글 URL 과 퍼센트 인코딩 URL 은 같은 페이지로 취급."""
    a = storage.normalize_url(
        "https://www.weather.go.kr/w/index.do"
        "#dong/4148051000/37.76004319269484/126.77988022977084/경기%20파주시%20아동동/SCH/파주시청"
    )
    b = storage.normalize_url(
        "https://www.weather.go.kr/w/index.do"
        "#dong/4148051000/37.76004319269484/126.77988022977084"
        "/%EA%B2%BD%EA%B8%B0%20%ED%8C%8C%EC%A3%BC%EC%8B%9C%20%EC%95%84%EB%8F%99%EB%8F%99"
        "/SCH/%ED%8C%8C%EC%A3%BC%EC%8B%9C%EC%B2%AD"
    )
    assert a == b
    assert storage.url_to_slug(a) == storage.url_to_slug(b)


@pytest.mark.parametrize("raw", ["ftp://example.com/a", "", "https://"])
def test_normalize_url_rejects(raw):
    with pytest.raises(ValueError):
        storage.normalize_url(raw)


@pytest.mark.parametrize("raw,expected", [
    ("example.com/a", True),
    ("  Example.COM ", True),
    ("//example.com/a", True),
    ("https://example.com/a", False),
    ("http://example.com/a", False),
    ("HTTPS://example.com", False),
])
def test_scheme_inferred(raw, expected):
    assert storage.scheme_inferred(raw) is expected


def test_normalize_idempotent():
    u = storage.normalize_url("https://example.com/path?b=2&a=1")
    assert storage.normalize_url(u) == u


def test_normalize_idempotent_with_route_fragment():
    u = storage.normalize_url("https://www.weather.go.kr/w/index.do#dong/4148051000/SCH/파주시청")
    assert storage.normalize_url(u) == u


def test_normalize_keeps_non_utf8_escapes():
    """UTF-8 로 디코딩되지 않는 시퀀스(EUC-KR 등)는 원형 유지 (손상 금지)."""
    u = "https://example.com/%B1%D7%B8%B2.png"
    assert storage.normalize_url(u) == u


def test_slug_safe():
    slug = storage.url_to_slug("https://example.com/../../etc/passwd?x=<script>")
    assert "/" not in slug and ".." not in slug
    assert len(slug) <= 49  # 40 + '-' + 8


def test_slug_unique_per_url():
    a = storage.url_to_slug("https://example.com/post?p=1")
    b = storage.url_to_slug("https://example.com/post?p=2")
    assert a != b


def test_slug_includes_route_fragment():
    a = storage.url_to_slug("https://www.weather.go.kr/w/index.do#dong/4148051000/SCH/x")
    b = storage.url_to_slug("https://www.weather.go.kr/w/index.do#dong/4143025300/SCH/y")
    assert a != b
    assert a.startswith("w-index-do-dong-")
    assert "/" not in a and len(a) <= 49


# ---- 스냅샷 메타/finalize (문서 파일 포함) ----

def _meta(**overrides) -> storage.SnapshotMeta:
    base = dict(
        url="https://example.com/", final_url="https://example.com/",
        taken_at="2026-06-11T00:00:00+00:00", content_hash="ab" * 32,
        http_status=200, title="제목",
    )
    base.update(overrides)
    return storage.SnapshotMeta(**base)


def test_meta_roundtrip_with_documents(tmp_path):
    docs = [{"url": "https://example.com/r.pdf", "file": "r-12345678.pdf",
             "bytes": 10, "sha256": "cd" * 32, "content_type": "application/pdf"}]
    storage.write_meta(tmp_path, _meta(documents=docs))
    assert storage.read_meta(tmp_path).documents == docs


def test_meta_reads_legacy_without_documents(tmp_path):
    """documents 필드가 없는 구형 meta.json 도 읽힌다 (기본 None)."""
    storage.write_meta(tmp_path, _meta())
    raw = (tmp_path / "meta.json").read_text(encoding="utf-8")
    import json
    data = json.loads(raw)
    del data["documents"]
    (tmp_path / "meta.json").write_text(json.dumps(data), encoding="utf-8")
    assert storage.read_meta(tmp_path).documents is None


def test_finalize_snapshot_moves_files_dir(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from chunchugwan import config
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")

    tmp_dir = tmp_path / "capture"
    (tmp_dir / "files").mkdir(parents=True)
    (tmp_dir / "raw.html").write_text("<html></html>", encoding="utf-8")
    (tmp_dir / "files" / "r-12345678.pdf").write_bytes(b"%PDF-1.4")

    snap_dir = storage.finalize_snapshot(
        tmp_dir, "example.com", "root-deadbeef", _meta(), "본문",
        datetime(2026, 6, 11, tzinfo=timezone.utc),
    )
    assert (snap_dir / "files" / "r-12345678.pdf").read_bytes() == b"%PDF-1.4"

    names = [f["name"] for f in storage.snapshot_files(snap_dir)]
    assert "files/r-12345678.pdf" in names  # 용량 집계/로그 목록에 포함된다


def test_finalize_snapshot_moves_webp_skip_marker(tmp_path, monkeypatch):
    """캡처 단계에서 PNG 유지가 확정된 마커도 스냅샷으로 옮겨진다."""
    from datetime import datetime, timezone
    from chunchugwan import config
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")

    tmp_dir = tmp_path / "capture"
    tmp_dir.mkdir()
    (tmp_dir / "screenshot.png").write_bytes(b"\x89PNG huge")
    (tmp_dir / storage.WEBP_SKIP_MARKER).touch()

    snap_dir = storage.finalize_snapshot(
        tmp_dir, "example.com", "root-deadbeef", _meta(), "본문",
        datetime(2026, 6, 11, tzinfo=timezone.utc),
    )
    assert (snap_dir / storage.WEBP_SKIP_MARKER).is_file()
    from chunchugwan import resources
    assert not resources.needs_compaction(snap_dir)
