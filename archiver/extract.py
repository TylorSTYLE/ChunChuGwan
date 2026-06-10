"""본문 텍스트 추출 + 비교용 정규화.

diff 품질의 핵심 모듈. 해시/diff는 항상 normalize() 결과를 기준으로 한다.
"""

from __future__ import annotations

import html
import re

import trafilatura

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


def extract_text(raw_html: str, url: str) -> str:
    """raw.html 에서 본문 텍스트(markdown)를 추출. 실패 시 <body> 텍스트 폴백."""
    text = trafilatura.extract(
        raw_html, output_format="markdown", include_links=True, url=url
    )
    if text:
        return text
    return _body_text_fallback(raw_html)


def _body_text_fallback(raw_html: str) -> str:
    """trafilatura 추출 실패 시 <body> 태그 제거 기반의 단순 텍스트 추출."""
    m = re.search(r"<body[^>]*>(.*?)</body>", raw_html, re.I | re.S)
    body = m.group(1) if m else raw_html
    body = re.sub(r"<(script|style|noscript)\b[^>]*>.*?</\1>", " ", body, flags=re.I | re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    return html.unescape(body)


def normalize(text: str) -> str:
    """비교/해시 전 노이즈 제거. 멱등: normalize(normalize(x)) == normalize(x)."""
    for pat in _TIMESTAMP_PATTERNS:
        text = pat.sub("[TIME]", text)
    for pat in _RELATIVE_TIME_PATTERNS:
        text = pat.sub("[TIME]", text)

    lines: list[str] = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if _AD_LINE.match(line):
            continue
        lines.append(line)
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()
