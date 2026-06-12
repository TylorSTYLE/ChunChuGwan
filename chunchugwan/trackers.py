"""알려진 사이트 추적기 패턴.

EXTERNAL_SELECTORS  — src 속성 기반 CSS 셀렉터 (외부 스크립트)
INLINE_PATTERNS     — 인라인 스크립트·noscript 텍스트에서 매칭할 정규식 패턴 (문자열)

capture.py 가 page.html 저장 전 live DOM 에 적용한다.
raw.html 은 원본 보존을 위해 건드리지 않는다.
"""
from __future__ import annotations

# 외부(src=) 추적기 스크립트를 가리키는 CSS 셀렉터
EXTERNAL_SELECTORS: tuple[str, ...] = (
    # ── Google Analytics / Tag Manager ─────────────────────────────
    "script[src*='google-analytics.com']",
    "script[src*='googletagmanager.com']",
    "script[src*='googleadservices.com']",
    "script[src*='googlesyndication.com']",
    # ── Meta / Facebook ────────────────────────────────────────────
    "script[src*='connect.facebook.net']",
    # ── Twitter / X ────────────────────────────────────────────────
    "script[src*='static.ads-twitter.com']",
    "script[src*='platform.twitter.com']",
    # ── LinkedIn ───────────────────────────────────────────────────
    "script[src*='snap.licdn.com']",
    # ── TikTok ─────────────────────────────────────────────────────
    "script[src*='analytics.tiktok.com']",
    # ── Hotjar ─────────────────────────────────────────────────────
    "script[src*='static.hotjar.com']",
    # ── Microsoft Clarity ──────────────────────────────────────────
    "script[src*='clarity.ms']",
    # ── Cloudflare Beacon ──────────────────────────────────────────
    "script[src*='static.cloudflareinsights.com']",
    # ── Amplitude ──────────────────────────────────────────────────
    "script[src*='cdn.amplitude.com']",
    # ── Segment ────────────────────────────────────────────────────
    "script[src*='cdn.segment.com']",
    # ── Mixpanel ───────────────────────────────────────────────────
    "script[src*='cdn.mxpnl.com']",
    "script[src*='cdn.mixpanel.com']",
    # ── Adobe Analytics ────────────────────────────────────────────
    "script[src*='omtrdc.net']",
    "script[src*='2o7.net']",
    # ── Naver ──────────────────────────────────────────────────────
    "script[src*='wcs.naver.com']",
    # ── Kakao ──────────────────────────────────────────────────────
    "script[src*='t1.kakaocdn.net/kasnet']",
)

# src 없는 인라인 <script> 및 <noscript> 텍스트에서 매칭할 정규식 패턴
# 추적기 초기화 코드에만 나타나는 특정 패턴으로 한정 — 오탐 방지
INLINE_PATTERNS: tuple[str, ...] = (
    # Google Tag Manager init (인라인 loader)
    r"gtm\.start",
    # GA4 gtag('config', ...) 호출
    r"gtag\s*\(\s*['\"]config['\"]",
    # Google Analytics UA (구형)
    r"_gaq\.push",
    # Facebook Pixel init / track
    r"fbq\s*\(\s*['\"](?:init|track)['\"]",
    # TikTok Pixel
    r"ttq\.load\s*\(",
    # Microsoft Clarity
    r"clarity\s*\(\s*['\"]set['\"]",
    # Hotjar
    r"window\._hjSettings\s*=",
    # Segment
    r"window\.analytics\.load\s*\(",
    # GTM noscript fallback (<noscript><iframe src="...googletagmanager.com/ns.html...">)
    r"googletagmanager\.com/ns\.html",
    # Facebook Pixel noscript fallback (<noscript><img src="...facebook.com/tr?...">)
    r"facebook\.com/tr\?",
)
