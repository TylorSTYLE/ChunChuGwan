"""extract.py 테스트. 네트워크 없이 로컬 fixture HTML 사용."""
from pathlib import Path

import pytest

from chunchugwan import extract

FIXTURE = Path(__file__).parent / "fixtures" / "article.html"
BOARD_FIXTURE = Path(__file__).parent / "fixtures" / "board_list.html"


@pytest.fixture
def article_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_extract_text_main_content(article_html):
    text = extract.extract_text(article_html, "https://example.com/article")
    assert "웹 아카이빙 시스템의 설계 원칙" in text
    assert "정규화 단계의 중요성" in text
    assert "이 스크립트는 추출되면 안 된다" not in text


def test_extract_text_keeps_link_titles_on_board_list():
    """게시판/목록형 페이지에서 글 제목(<a> 안 텍스트)이 잘리지 않는다.

    trafilatura 의 link-density 필터가 링크 비중 높은 목록 블록을 통째로
    버리는 회귀 — fixture 는 실제 게시판 목록 페이지를 축소한 것으로,
    <a> detag 없이 추출하면 제목이 전부 사라진다.
    """
    html = BOARD_FIXTURE.read_text(encoding="utf-8")
    text = extract.extract_text(html, "https://www.clien.net/service/board/news")
    assert "구글 플레이스토어서 엑스 '청소년 이용불가'…등급 상향 조치" in text
    assert "macOS 27은 이제 울트라와이드 모니터를 기본적으로 지원합니다" in text
    assert "Apple, 서비스 전반에 걸쳐 여러 혁신적인 기능과 지능 경험 공개" in text


def test_detag_links_converts_anchors():
    out = extract._detag_links('<html><body><p><a href="/x">제목 링크</a> 본문</p></body></html>')
    assert "<a" not in out
    assert "제목 링크" in out


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


def test_normalize_drop_line_patterns():
    src = "본문 첫줄\n관련 기사: 어쩌고\n구독하기\n본문 둘째줄"
    out = extract.normalize(src, drop_line_patterns=("^관련 기사", "^구독하기$"))
    assert out == "본문 첫줄\n본문 둘째줄"
    # 패턴 없으면 그대로 유지
    assert "관련 기사" in extract.normalize(src)


def test_hash_stable_across_timestamp_noise():
    a = extract.normalize("기사 본문입니다. 작성 2026-06-10T09:00:00Z")
    b = extract.normalize("기사 본문입니다. 작성 2026-06-11T21:42:13Z")
    assert a == b
