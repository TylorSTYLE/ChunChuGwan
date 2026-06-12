"""본문 텍스트 추출 + 비교용 정규화.

diff 품질의 핵심 모듈. 해시/diff는 항상 normalize() 결과를 기준으로 한다.

추출은 렌더링된 DOM 의 가시 텍스트를 문서 순서대로 덤프하는 방식이다.
trafilatura 류의 본문 판별 휴리스틱은 게시판 목록·뉴스 기사·포털·상품
페이지에서 제목/본문까지 잘라내는 실패가 잦았다 (실측: 연합뉴스 기사
커버리지 19%, 클리앙 게시글 22% — 제목 유실). 아카이빙에서는 누락
(변경 미탐지)이 과잉(메뉴 텍스트 포함)보다 훨씬 비싸므로 recall 우선.
정적인 메뉴/푸터는 해시에 영향이 없고, 동적 노이즈는 normalize() 와
도메인 룰(remove_selectors / remove_line_patterns)로 거른다.
"""

from __future__ import annotations

import html
import re

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

# 광고/추천 위젯에서 흔한 단독 줄 (도메인별 룰은 rules.json)
_AD_LINE = re.compile(r"^(?:광고|AD|Advertisement|Sponsored(?:\s+Content)?|스폰서드?)$", re.I)

# 글자/숫자가 하나도 없는 줄 (구분선 ─── , 장식 기호, 광고 블록의 잔여 "." 등)
_NO_WORD_LINE = re.compile(r"^[\W_]+$")

# 텍스트를 만들지 않거나(메타데이터·미디어) 가시 텍스트가 아닌(스크립트·
# 폼 컨트롤·숨김 모달) 태그 — 서브트리째 건너뛴다.
_DROP_TAGS = frozenset({
    "script", "style", "noscript", "template", "head", "title", "base",
    "link", "meta", "svg", "math", "canvas", "iframe", "frame", "frameset",
    "object", "embed", "applet", "audio", "video", "source", "track",
    "picture", "img", "map", "area", "datalist", "select", "option",
    "optgroup", "input", "textarea", "button", "progress", "meter", "dialog",
})

_HEADING_TAGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}

# 줄바꿈 경계를 만드는 블록 레벨 태그 (인라인 태그는 텍스트가 이어진다)
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "caption", "dd",
    "details", "div", "dl", "dt", "fieldset", "figcaption", "figure",
    "footer", "form", "header", "hr", "legend", "li", "main", "menu", "nav",
    "ol", "p", "pre", "section", "summary", "table", "tbody", "td", "tfoot",
    "th", "thead", "tr", "ul",
}) | frozenset(_HEADING_TAGS)

_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)

# class/id 토큰이 정확히 일치할 때만 광고 컨테이너로 보고 제거.
# 부분문자열 매칭은 금지 — 과거 trafilatura 가 'meta' 부분문자열로
# 작성자·날짜 셀을 지우던 회귀(_GREEDY_DISCARD)와 같은 실수를 막는다.
_AD_TOKENS = frozenset({
    "ad", "ads", "advert", "adverts", "advertisement", "advertising",
    "adsense", "adsbygoogle", "sponsor", "sponsored",
})
_TOKEN_SPLIT = re.compile(r"[\s_-]+")
# 광고 클래스가 붙었어도 텍스트가 이만큼 크면 콘텐츠 컨테이너로 본다 —
# 실측: 클리앙은 댓글 영역 전체에 'comment ad_banner' 클래스를 붙인다.
_AD_MAX_CHARS = 300


def extract_text(raw_html: str, url: str) -> str:
    """렌더링된 DOM 에서 가시 텍스트를 문서 순서로 추출 (markdown 유사).

    - 헤딩은 ``#`` 접두로, 블록 요소는 줄바꿈으로 경계를 만든다.
    - 테이블 행은 셀을 `` | `` 로 이어 한 줄로 만든다 (목록형 페이지의
      행 단위 diff 를 위해).
    - hidden/aria-hidden/display:none 요소와 광고 컨테이너는 제외.

    파싱 실패 또는 결과가 비면 <body> 텍스트 폴백.
    """
    del url  # 시그니처 호환용 (도메인별 분기 없음)
    try:
        tree = lxml_html.fromstring(raw_html)
    except Exception:
        return _body_text_fallback(raw_html)
    body = tree.find("body")
    root = body if body is not None else tree
    parts: list[str] = []
    _walk(root, parts)
    text = _assemble(parts)
    if not text:
        return _body_text_fallback(raw_html)
    return text


def _dropped(el) -> bool:
    """서브트리째 건너뛸 요소인가 (비가시·폼 컨트롤·광고 컨테이너)."""
    if el.tag in _DROP_TAGS:
        return True
    if el.get("hidden") is not None or el.get("aria-hidden") == "true":
        return True
    style = el.get("style")
    if style and _HIDDEN_STYLE.search(style):
        return True
    for attr in ("class", "id"):
        val = el.get(attr)
        if val and any(t in _AD_TOKENS for t in _TOKEN_SPLIT.split(val.lower())):
            return len(" ".join(el.text_content().split())) < _AD_MAX_CHARS
    return False


def _walk(el, parts: list[str]) -> None:
    """el 서브트리의 가시 텍스트를 parts 에 누적. el 자체의 tail 은 부모 몫."""
    tag = el.tag
    if not isinstance(tag, str):  # 주석/PI — tail 은 부모 루프가 처리한다
        return
    if _dropped(el):
        return
    if tag in _HEADING_TAGS:
        t = _inline_text(el)
        if t:
            parts.append(f"\n{_HEADING_TAGS[tag]} {t}\n")
        return
    if tag == "tr":
        cells = [
            _inline_text(c)
            for c in el
            if isinstance(c.tag, str) and c.tag in ("td", "th") and not _dropped(c)
        ]
        row = " | ".join(c for c in cells if c)
        if row:
            parts.append(f"\n{row}\n")
        return
    if tag == "pre":
        t = el.text_content().strip("\n")
        if t.strip():
            parts.append(f"\n{t}\n")
        return
    if el.text:
        parts.append(el.text)
    for child in el:
        _walk(child, parts)
        if isinstance(child.tag, str) and child.tag in _BLOCK_TAGS:
            parts.append("\n")
        if child.tail:
            parts.append(child.tail)
    if tag in _BLOCK_TAGS:
        parts.append("\n")


def _inline_text(el) -> str:
    """헤딩·테이블 셀용 — 서브트리 가시 텍스트를 한 줄로."""
    parts: list[str] = []
    _walk_inline(el, parts)
    return " ".join("".join(parts).split())


def _walk_inline(el, parts: list[str]) -> None:
    if not isinstance(el.tag, str) or _dropped(el):
        return
    if el.text:
        parts.append(el.text)
    for child in el:
        _walk_inline(child, parts)
        if isinstance(child.tag, str) and child.tag in _BLOCK_TAGS:
            parts.append(" ")
        if child.tail:
            parts.append(child.tail)


def _assemble(parts: list[str]) -> str:
    """누적 조각 → 줄 단위 정리. 줄 안 공백 축약, 빈 줄 압축."""
    lines: list[str] = []
    blank = False
    for line in "".join(parts).split("\n"):
        line = " ".join(line.split())
        if line:
            lines.append(line)
            blank = False
        elif not blank and lines:
            lines.append("")
            blank = True
    out = "\n".join(lines).strip()
    # 헤딩 앞 빈 줄 유지보다 일관성이 중요 — 빈 줄을 모두 제거해
    # 멱등성과 diff 안정성을 높인다 (가독 단락은 줄 단위로 충분).
    return re.sub(r"\n{2,}", "\n", out)


def _body_text_fallback(raw_html: str) -> str:
    """DOM 파싱 실패 시 <body> 태그 제거 기반의 단순 텍스트 추출."""
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
        if line and _NO_WORD_LINE.match(line):
            continue
        if _AD_LINE.match(line):
            continue
        if any(p.search(line) for p in extra):
            continue
        lines.append(line)
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()
