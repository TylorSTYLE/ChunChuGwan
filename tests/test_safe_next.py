"""safe_next open redirect 가드 (보안 검토 F5)."""
from chunchugwan.web.auth_routes import safe_next


def test_allows_internal_path():
    assert safe_next("/archive/list") == "/archive/list"
    assert safe_next("/search?q=a&b=c") == "/search?q=a&b=c"


def test_rejects_external_and_protocol_relative():
    assert safe_next("//evil.com") == "/"
    assert safe_next("https://evil.com") == "/"
    assert safe_next("http://evil.com") == "/"


def test_rejects_backslash_bypass():
    # 브라우저가 \ 를 / 로 정규화해 //evil 로 새는 우회 차단
    assert safe_next("/\\evil.com") == "/"
    assert safe_next("\\/evil.com") == "/"
    assert safe_next("/path\\x") == "/"


def test_rejects_control_chars_and_empty():
    assert safe_next("/a\nb") == "/"
    assert safe_next("/a\x00b") == "/"
    assert safe_next("") == "/"
    assert safe_next(None) == "/"
    assert safe_next("relative/path") == "/"
