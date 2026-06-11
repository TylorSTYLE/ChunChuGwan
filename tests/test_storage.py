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
