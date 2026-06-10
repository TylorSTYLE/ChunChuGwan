"""extract.py 테스트. 네트워크 없이 로컬 fixture HTML 사용."""
from pathlib import Path

import pytest

from archiver import extract

FIXTURE = Path(__file__).parent / "fixtures" / "article.html"


@pytest.fixture
def article_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_extract_text_main_content(article_html):
    text = extract.extract_text(article_html, "https://example.com/article")
    assert "웹 아카이빙 시스템의 설계 원칙" in text
    assert "정규화 단계의 중요성" in text
    assert "이 스크립트는 추출되면 안 된다" not in text


def test_extract_text_fallback_when_trafilatura_fails(monkeypatch):
    monkeypatch.setattr(extract.trafilatura, "extract", lambda *a, **kw: None)
    html = "<html><body><p>본문 &amp; 폴백</p><script>x()</script></body></html>"
    text = extract.extract_text(html, "https://example.com/")
    assert "본문 & 폴백" in text
    assert "x()" not in text


def test_normalize_replaces_timestamps():
    out = extract.normalize("발행 2026-06-10T09:30:00+09:00 / 수정 3분 전 / 5 minutes ago")
    assert "2026-06-10T09:30" not in out
    assert "3분 전" not in out
    assert "minutes ago" not in out
    assert "[TIME]" in out


def test_normalize_drops_ad_lines_and_collapses_whitespace():
    src = "본문   첫줄\n\n\n\n광고\nSponsored\n본문  둘째줄\n"
    out = extract.normalize(src)
    assert out == "본문 첫줄\n\n본문 둘째줄"


def test_normalize_idempotent(article_html):
    text = extract.extract_text(article_html, "https://example.com/article")
    once = extract.normalize(text)
    assert extract.normalize(once) == once


def test_hash_stable_across_timestamp_noise():
    a = extract.normalize("기사 본문입니다. 작성 2026-06-10T09:00:00Z")
    b = extract.normalize("기사 본문입니다. 작성 2026-06-11T21:42:13Z")
    assert a == b
