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
    (
        "https://www.weather.go.kr/w/index.do#dong/4148051000/37.76/126.77/경기%20파주시%20아동동/SCH/파주시청",
        "https://www.weather.go.kr/w/index.do#dong/4148051000/37.76/126.77/경기%20파주시%20아동동/SCH/파주시청",
    ),
    ("https://example.com/app#!/users/42", "https://example.com/app#!/users/42"),
    ("https://example.com/a#section-2", "https://example.com/a"),
])
def test_normalize_url(raw, expected):
    assert storage.normalize_url(raw) == expected


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
