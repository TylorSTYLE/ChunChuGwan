"""URL 정규화/slug 테스트. M1에서 구현과 함께 통과시킬 것."""
import pytest
from archiver import storage


@pytest.mark.parametrize("raw,expected", [
    ("HTTPS://Example.COM:443/a?b=2&a=1#frag", "https://example.com/a?a=1&b=2"),
    ("http://example.com/a?utm_source=x&q=1", "http://example.com/a?q=1"),
    ("https://example.com/", "https://example.com/"),
])
def test_normalize_url(raw, expected):
    assert storage.normalize_url(raw) == expected


def test_normalize_idempotent():
    u = storage.normalize_url("https://example.com/path?b=2&a=1")
    assert storage.normalize_url(u) == u


def test_slug_safe():
    slug = storage.url_to_slug("https://example.com/../../etc/passwd?x=<script>")
    assert "/" not in slug and ".." not in slug
    assert len(slug) <= 49  # 40 + '-' + 8


def test_slug_unique_per_url():
    a = storage.url_to_slug("https://example.com/post?p=1")
    b = storage.url_to_slug("https://example.com/post?p=2")
    assert a != b
