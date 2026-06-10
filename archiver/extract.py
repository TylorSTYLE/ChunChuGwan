"""본문 텍스트 추출 + 비교용 정규화.

diff 품질의 핵심 모듈. 해시/diff는 항상 normalize() 결과를 기준으로 한다.
"""

from __future__ import annotations

import re


def extract_text(raw_html: str, url: str) -> str:
    """raw.html 에서 본문 텍스트(markdown)를 추출.

    TODO(M2): trafilatura.extract(raw_html, output_format="markdown",
    include_links=True, url=url) 사용. 추출 실패(None) 시 <body> 텍스트
    폴백 추출.
    """
    raise NotImplementedError


def normalize(text: str) -> str:
    """비교/해시 전 노이즈 제거.

    TODO(M2): 최소 다음을 처리
    - 연속 공백/빈 줄 정리
    - ISO/RFC 타임스탬프, '3분 전' 류 상대시각 패턴을 placeholder 로 치환
    - 광고/추천 위젯에서 흔한 단독 줄 패턴 제거 (도메인별 룰은 M5)
    멱등해야 함: normalize(normalize(x)) == normalize(x)
    """
    raise NotImplementedError
