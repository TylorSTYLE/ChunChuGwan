---
description: 대시보드 디자인·렌더링 보안(원칙 5)·i18n·diff 뷰. web/ 템플릿·라우트·differ 를 만질 때.
paths:
  - "chunchugwan/web/**"
  - "chunchugwan/differ.py"
  - "docs/DASHBOARD.md"
---

# 대시보드

## 렌더링·서빙 보안 (아키텍처 원칙 5)

**대시보드는 기본 loopback, 외부 노출 시 인증 필수.** 기본 바인딩 127.0.0.1.
컨테이너 등 포트포워딩이 필요한 환경에서만 `WCCG_HOST` 로 바인딩을
오버라이드하며(compose 가 0.0.0.0 주입), 호스트 노출은 항상 127.0.0.1
포트 매핑으로 제한한다. `WCCG_AUTH=off` 는 loopback 바인딩일 때만 허용
(`cli.serve` 가 강제 — 컨테이너의 0.0.0.0 바인딩에서는 인증이 항상 켜진다).
아카이빙된 HTML을 렌더링할 때는 반드시 `<iframe sandbox>` (스크립트 실행 금지)
안에서만 보여준다. 아카이빙된 페이지의 JS를 대시보드 컨텍스트에서 실행하는
일은 절대 없어야 한다. 허용하는 유일한 sandbox 토큰은
`allow-top-navigation-by-user-activation` — 사이트 전체 아카이브가
재작성한 링크(`/crawl/{id}/goto` + `target="_top"`)를 사용자가 직접
클릭했을 때만 뷰어 전체가 다음 스냅샷으로 이동하게 한다 (스크립트로는
불가, `allow-scripts`/`allow-same-origin` 절대 추가 금지).
`/resource/` (공유 자원 CAS)는 유일한 인증 예외
경로 — 샌드박스 문서의 하위 요청에는 SameSite 쿠키가 안 붙기 때문이며,
sha256 콘텐츠 주소 이름 + 미디어 타입 화이트리스트(문서 타입 금지) +
CSP sandbox 로만 서빙한다 (`resources.py` 보안 노트 참조). 함께 저장된
문서 파일은 별도의 문서 CAS(`documents/`, documents.py)에 두되 /resource/
로는 절대 합치지 않고, 인증이 걸린 라우트(`/snapshot/{id}/doc/{name}` —
meta.json documents 목록 검증, `/document/{sha256}/{name}` — snapshot_documents
행 검증)에서만 항상 첨부파일 다운로드(렌더링 금지)로 서빙한다. compact
이전 구형 스냅샷의 문서는 스냅샷 안 `files/` 에서 그대로 서빙된다.

## 디자인 방향

- 화면 22개 — 현황(`/`), 목록(`/archives` — 사이트(서브도메인) 단위),
  사이트 상세(`/sites/{id}` — 소속 페이지·문서·크롤 회차·스케줄·사이트 삭제),
  사이트 로그인 자격증명(`/sites/{id}/credentials` — 관리자 전용),
  문서(`/documents` — 문서 파일 통합 목록),
  검색(`/search` — 본문·문서 전문 검색, viewer 이상), 새 아카이빙(`/archive/new`),
  사이트 아카이브 진행(`/crawls/{id}` — 크롤 회차 상세), 스케줄(`/schedules`),
  타임라인, 스냅샷 뷰어, diff 뷰어, 아카이빙 로그(`/logs` — viewer 이상),
  시스템 로그(`/system/logs` — 관리자 전용), 시스템, 사용자,
  권한 그룹(`/system/groups` — 관리자 전용, 역할 프리셋 편집·커스텀 그룹 추가/삭제),
  API 키,
  개인 API Key(`/settings/api-keys` — 본인 확장 토큰 발급·폐기),
  내 아카이브(`/settings/archives` — 본인이 요청한 아카이빙 이력),
  사람 확인 필요(`/archive/needs-human` — 관리자 전용, `WCCG_LIVE_CHALLENGE` 켜짐 시)·
  라이브 챌린지 처리(`/archive/jobs/{id}/live` — 관리자, 스크린샷 보고 직접 클릭/입력).
  권한이 없는 메뉴는 헤더에 표시하지 않는다 (`templating._auth_context` 의
  노출 플래그). 로그(아카이빙·시스템)·관리자(사용자·시스템) 메뉴와 개인설정
  (우측 이메일/표시이름 → 계정·개인 API Key·내 아카이브·로그아웃)은 헤더에서
  같은 `<details>` 드롭다운(`.nav-group`)으로 묶는다 (base.html — 넓은 화면은
  겹침 패널, 좁은 화면은 햄버거 안 아코디언). 화면별 라우트·권한·세부 동작은
  `docs/DASHBOARD.md` 참조.
- 도구다운 밀도 있는 UI. 모노스페이스로 해시/시각 표기, 변경 상태는 색 뱃지
  (변경=amber, 동일=gray, 신규=green). 과한 장식/그라데이션 금지.
- 다국어(ko/en): `web/i18n.py` — 한국어 원문이 메시지 키(gettext msgid 방식),
  언어별 "원문 → 번역" dict 로 확장. 로케일은 `wccg_lang` 쿠키(헤더의 언어
  선택, `POST /lang`) → Accept-Language → ko. 템플릿은 `_("…")`, 라우트는
  `i18n.t(request, "…")`. 새 UI 문자열 추가 시 en 카탈로그도 채울 것 —
  템플릿 리터럴 키 누락은 `tests/test_i18n.py` 가 검사한다. CLI 는 한국어 유지.
- diff 뷰: 텍스트 side-by-side + 스크린샷 비교(슬라이더 또는 토글). 단, 비교
  대상 중 하나라도 확장(브라우저) 캡처(`origin=extension`)면 스크린샷 비교를
  숨기고(로컬 해상도·dpr 의존이라 무의미) 본문 diff 에 렌더 환경 차이 경고를 단다.
- 브라우저 클라이언트 캡처: 확장이 캡처한 스냅샷은 "브라우저 캡처"·"불완전"
  뱃지로 표시한다. 캡처/적재(ingest) 메커니즘과 REST API 상세는
  `.claude/rules/api-extension.md` 참조.

> 권한 가드(`web.permissions.has_permission`·역할 프리셋)는 `.claude/rules/authentication.md`,
> 화면이 노출하는 DB 테이블 상세는 `.claude/rules/database.md` 참조.
