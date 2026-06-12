"""본문 텍스트 추출 + 비교용 정규화.

diff 품질의 핵심 모듈. 해시/diff는 항상 normalize() 결과를 기준으로 한다.
"""

from __future__ import annotations

import html
import re

import trafilatura
from lxml import html as lxml_html

_TIMESTAMP_PATTERNS = (
    # ISO 8601: 2026-06-10T12:34:56.123+09:00, 2026-06-10 12:34
    re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
    # RFC 2822: Tue, 10 Jun 2026 12:34:56 GMT
    re.compile(
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+\d{1,2}\s+"
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}"
        r"\s+\d{2}:\d{2}(?::\d{2})?(?:\s+(?:GMT|UTC|[+-]\d{4}))?"
    ),
)

_RELATIVE_TIME_PATTERNS = (
    re.compile(r"\d+\s*(?:초|분|시간|일|주|개월|달|년)\s*전"),
    re.compile(r"\d+\s*(?:second|minute|hour|day|week|month|year)s?\s+ago", re.I),
    re.compile(r"(?:방금|조금)\s*전|just now", re.I),
)

# 광고/추천 위젯에서 흔한 단독 줄 (도메인별 룰은 M5)
_AD_LINE = re.compile(r"^(?:광고|AD|Advertisement|Sponsored(?:\s+Content)?|스폰서드?)$", re.I)

# trafilatura 의 OVERALL_DISCARD 규칙은 class/id 부분문자열만으로 노드를 버리는데,
# 이 토큰들은 게시판 목록의 작성자·날짜·조회수 셀(post-meta-text, list_author 등)이나
# 기사 바이라인 같은 실제 콘텐츠까지 지운다. 추출 전에 해당 부분문자열을 치환해
# 무력화한다 — nav/footer/widget/sidebar 등 나머지 보일러플레이트 탐지는 그대로 동작.
_GREEDY_DISCARD = re.compile(r"meta|byline|author|timestamp", re.I)


def extract_text(raw_html: str, url: str) -> str:
    """raw.html 에서 본문 텍스트(markdown)를 추출. 실패 시 <body> 텍스트 폴백.

    trafilatura 는 링크 비중이 높은 블록을 보일러플레이트로 잘라내는데,
    게시판/목록형 페이지에서는 본문(글 제목)이 전부 <a> 안이라 통째로
    사라진다. 추출 전에 <a> 를 <span> 으로 바꿔 link-density 필터를
    우회한다 — nav/footer 등 본문 영역 탐지는 그대로 동작한다.
    class/id 기반 과잉 제거 토큰도 함께 무력화한다 (_GREEDY_DISCARD 참조).
    """
    text = trafilatura.extract(
        _prepare_html(raw_html), output_format="markdown", url=url
    )
    if text:
        return text
    return _body_text_fallback(raw_html)


def _prepare_html(raw_html: str) -> str:
    """trafilatura 추출 전처리 HTML 반환. 파싱 실패 시 원본 그대로.

    - <a> → <span> 치환 (link-density 필터 우회)
    - class/id 의 _GREEDY_DISCARD 토큰 치환 (작성자·날짜 셀 보존)
    """
    try:
        tree = lxml_html.fromstring(raw_html)
    except Exception:
        return raw_html
    for a in tree.iter("a"):
        a.tag = "span"
    for attr in ("class", "id"):
        for el in tree.xpath(f"//*[@{attr}]"):
            val = el.get(attr)
            if val and _GREEDY_DISCARD.search(val):
                el.set(attr, _GREEDY_DISCARD.sub("x", val))
    return lxml_html.tostring(tree, encoding="unicode")


def _body_text_fallback(raw_html: str) -> str:
    """trafilatura 추출 실패 시 <body> 태그 제거 기반의 단순 텍스트 추출."""
    m = re.search(r"<body[^>]*>(.*?)</body>", raw_html, re.I | re.S)
    body = m.group(1) if m else raw_html
    body = re.sub(r"<(script|style|noscript)\b[^>]*>.*?</\1>", " ", body, flags=re.I | re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    return html.unescape(body)


def normalize(text: str, drop_line_patterns: tuple[str, ...] = ()) -> str:
    """비교/해시 전 노이즈 제거. 멱등: normalize(normalize(x)) == normalize(x).

    drop_line_patterns: 도메인 룰(rules.json)의 추가 줄 제거 정규식.
    줄 단위로 search 가 걸리면 그 줄을 버린다.
    """
    extra = [re.compile(p) for p in drop_line_patterns]
    for pat in _TIMESTAMP_PATTERNS:
        text = pat.sub("[TIME]", text)
    for pat in _RELATIVE_TIME_PATTERNS:
        text = pat.sub("[TIME]", text)

    lines: list[str] = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if _AD_LINE.match(line):
            continue
        if any(p.search(line) for p in extra):
            continue
        lines.append(line)
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()
