"""extract.py 테스트. 네트워크 없이 로컬 fixture HTML 사용."""
from pathlib import Path

import pytest

from chunchugwan import extract

FIXTURE = Path(__file__).parent / "fixtures" / "article.html"
BOARD_FIXTURE = Path(__file__).parent / "fixtures" / "board_list.html"
BOARD_META_FIXTURE = Path(__file__).parent / "fixtures" / "board_list_meta.html"


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

    과거 trafilatura 의 link-density 필터가 링크 비중 높은 목록 블록을
    통째로 버리던 회귀 — fixture 는 실제 게시판 목록 페이지를 축소한 것.
    """
    html = BOARD_FIXTURE.read_text(encoding="utf-8")
    text = extract.extract_text(html, "https://www.clien.net/service/board/news")
    assert "구글 플레이스토어서 엑스 '청소년 이용불가'…등급 상향 조치" in text
    assert "macOS 27은 이제 울트라와이드 모니터를 기본적으로 지원합니다" in text
    assert "Apple, 서비스 전반에 걸쳐 여러 혁신적인 기능과 지능 경험 공개" in text


def test_extract_text_keeps_meta_classed_cells_on_board_list():
    """class 에 meta/author 등이 들어간 작성자·날짜·조회수 셀이 잘리지 않는다.

    과거 trafilatura 의 OVERALL_DISCARD 가 class 부분문자열(meta 등)만으로
    노드를 버리던 회귀 — fixture 는 실제 게시판 목록의 행 구조(post-meta-text,
    mobile-meta 클래스)를 그대로 가져온 것.
    """
    html = BOARD_META_FIXTURE.read_text(encoding="utf-8")
    text = extract.extract_text(html, "https://damoang.net/new")
    assert "오픈AI 요금 대폭 인하 검토…앤트로픽과 기업시장 경쟁" in text  # 제목
    assert "아름다운별" in text  # 작성자
    assert "2.1k" in text       # 조회수


def test_extract_text_headings_become_markdown():
    html = "<html><body><h1>큰 제목</h1><h2>작은 <b>제목</b></h2><p>본문</p></body></html>"
    text = extract.extract_text(html, "https://example.com/")
    assert "# 큰 제목" in text
    assert "## 작은 제목" in text


def test_extract_text_table_row_joins_cells():
    html = (
        "<html><body><table><tr><th>제목</th><th>작성자</th></tr>"
        "<tr><td>첫 글</td><td>아무개</td><td style='display:none'>숨김</td></tr>"
        "</table></body></html>"
    )
    text = extract.extract_text(html, "https://example.com/")
    assert "제목 | 작성자" in text
    assert "첫 글 | 아무개" in text
    assert "숨김" not in text


def test_extract_text_skips_hidden_elements():
    html = (
        "<html><body><p>보임</p>"
        "<div hidden>hidden 속성</div>"
        '<div aria-hidden="true">aria 숨김</div>'
        '<div style="display: none">스타일 숨김</div>'
        '<div style="visibility:hidden">visibility 숨김</div>'
        "</body></html>"
    )
    text = extract.extract_text(html, "https://example.com/")
    assert "보임" in text
    assert "hidden 속성" not in text
    assert "aria 숨김" not in text
    assert "스타일 숨김" not in text
    assert "visibility 숨김" not in text


def test_extract_text_skips_ad_containers_by_exact_token():
    html = (
        "<html><body>"
        '<div class="da-ad-banner">광고 위젯 텍스트</div>'
        '<ins class="adsbygoogle">애드센스</ins>'
        '<div class="adventure-story">모험 이야기</div>'  # 부분문자열 오탐 금지
        '<div class="post-meta-text">아무개 06.11 2.1k</div>'
        "</body></html>"
    )
    text = extract.extract_text(html, "https://example.com/")
    assert "광고 위젯 텍스트" not in text
    assert "애드센스" not in text
    assert "모험 이야기" in text
    assert "아무개 06.11 2.1k" in text


def test_extract_text_keeps_large_container_despite_ad_class():
    """광고 클래스가 붙은 큰 컨테이너는 콘텐츠로 본다.

    실측 회귀 — 클리앙은 댓글 영역 전체에 'comment ad_banner' 클래스를
    붙여서, 토큰 매칭만으로 지우면 댓글이 통째로 사라진다.
    """
    comments = "".join(
        f"<div>댓글 {i}번 — 충분히 긴 본문 텍스트를 가진 댓글입니다.</div>" for i in range(20)
    )
    html = f'<html><body><div class="comment ad_banner">{comments}</div></body></html>'
    text = extract.extract_text(html, "https://example.com/")
    assert "댓글 7번" in text


def test_extract_text_skips_form_controls_keeps_form_content():
    html = (
        "<html><body><form><p>폼 안 본문</p>"
        "<select><option>옵션 노이즈</option></select>"
        "<button>버튼 라벨</button><input value='입력값'>"
        "</form></body></html>"
    )
    text = extract.extract_text(html, "https://example.com/")
    assert "폼 안 본문" in text
    assert "옵션 노이즈" not in text
    assert "버튼 라벨" not in text


def test_extract_text_inline_tags_do_not_split_words():
    html = "<html><body><p>안<b>녕</b>하세요 <i>본문</i>입니다</p></body></html>"
    text = extract.extract_text(html, "https://example.com/")
    assert "안녕하세요 본문입니다" in text


def test_extract_text_keeps_text_after_comment():
    html = "<html><body><p>앞 텍스트<!-- 주석 -->뒤 텍스트</p></body></html>"
    text = extract.extract_text(html, "https://example.com/")
    assert "앞 텍스트뒤 텍스트" in text
    assert "주석" not in text


def test_extract_text_fallback_when_parse_fails(monkeypatch):
    def boom(*a, **kw):
        raise ValueError("파싱 실패")

    monkeypatch.setattr(extract.lxml_html, "fromstring", boom)
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


def test_normalize_drops_no_word_lines():
    src = "본문 첫줄\n───────\n. . .\n***\n본문 둘째줄"
    out = extract.normalize(src)
    assert out == "본문 첫줄\n본문 둘째줄"


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
