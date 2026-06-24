"""알려진 쿠키 동의(CMP) 오버레이 셀렉터.

쿠키 동의 배너·설정 모달·다크 백드롭은 캡처된 DOM 에 그대로 남는데, 스냅샷
렌더는 `<iframe sandbox>`(스크립트 금지, 원칙 5)라 '동의' 버튼을 눌러 닫을 수
없어 본문을 영구히 가린다. capture.py 가 page.html 저장 전 live DOM 에서 이 셀렉터
들을 제거한다 (추적기 제거 `trackers.py` 와 같은 모델 — 렌더 노이즈 제거).

raw.html(원본 DOM 소스)은 건드리지 않는다. 본문 추출/해시용 content_html 도
건드리지 않아 콘텐츠 해시(원칙 3)·diff(원칙 4)에는 영향이 없다 — page.html·
screenshot(보기용 산출물)에서만 사라진다.

대상은 각 CMP 의 전용 컨테이너 id/클래스(루트 1개로 배너+백드롭+모달을 함께
제거)라 정상 본문을 지울 위험이 낮다.
"""
from __future__ import annotations

# 알려진 CMP 의 전용 컨테이너 셀렉터 (루트 우선 — 하위 배너/백드롭/모달을 함께 제거).
SELECTORS: tuple[str, ...] = (
    # ── OneTrust ───────────────────────────────────────────────────
    "#onetrust-consent-sdk",        # 루트 (배너+다크필터+설정모달 포함)
    ".onetrust-pc-dark-filter",     # 다크 백드롭 오버레이
    ".ot-sdk-overlay",
    # ── Cookiebot ──────────────────────────────────────────────────
    "#CybotCookiebotDialog",
    "#CybotCookiebotDialogBodyUnderlay",
    # ── TrustArc / TRUSTe ──────────────────────────────────────────
    "#truste-consent-track",
    ".truste_overlay",
    ".truste_box_overlay",
    ".trustarc-banner-container",
    # ── Quantcast ──────────────────────────────────────────────────
    "#qc-cmp2-ui",
    ".qc-cmp2-container",
    ".qc-cmp-cleanslate",
    # ── Didomi ─────────────────────────────────────────────────────
    "#didomi-host",
    # ── Usercentrics ───────────────────────────────────────────────
    "#usercentrics-root",
    "#usercentrics-cmp-ui",
    # ── Sourcepoint ────────────────────────────────────────────────
    "div[id^='sp_message_container_']",
    # ── Osano ──────────────────────────────────────────────────────
    ".osano-cm-window",
    ".osano-cm-dialog",
    # ── Cookie Consent (insites / orestbida) ───────────────────────
    ".cc-window",
    "#cc-main",
    # ── Complianz ──────────────────────────────────────────────────
    "#cmplz-cookiebanner-container",
    ".cmplz-cookiebanner",
    # ── Borlabs Cookie ─────────────────────────────────────────────
    "#BorlabsCookieBox",
    "#BorlabsCookieBoxWrap",
    # ── CookieYes / GDPR Cookie Consent (WordPress) ────────────────
    "#cookie-law-info-bar",
    ".cli-modal",
    # ── Iubenda ────────────────────────────────────────────────────
    "#iubenda-cs-banner",
    ".iubenda-cs-overlay",
    # ── Termly ─────────────────────────────────────────────────────
    "#termly-code-snippet-support",
    # ── 일반 GDPR 래퍼 ─────────────────────────────────────────────
    "#gdpr-consent-tool-wrapper",
)
