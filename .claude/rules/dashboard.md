---
description: 대시보드 디자인·SvelteKit SPA·렌더링 보안(원칙 5)·i18n·diff 뷰. web/ 라우트·frontend·differ 를 만질 때.
paths:
  - "chunchugwan/web/**"
  - "frontend/**"
  - "chunchugwan/differ.py"
  - "docs/DASHBOARD.md"
---

# 대시보드

## 아키텍처 (C2 컷오버 이후 — SvelteKit SPA)

대시보드는 **SvelteKit 정적 SPA**다 (Jinja2 SSR 은 C2 컷오버로 제거). 프론트엔드 소스는
`frontend/`(Svelte 5 + adapter-static, `paths.base=''`), 빌드 산출물은
`chunchugwan/web/frontend_dist`(없으면 개발 빌드 `frontend/build`)를 FastAPI 가 **루트(/)**
로 서빙한다 — `app.py` 의 catch-all `@app.get("/{full_path:path}")`(반드시 마지막 등록)가
실존 파일이면 그 파일을, 아니면 `index.html`(딥링크·새로고침 fallback)을 돌려준다.
미매칭 `/api` 는 SPA HTML 대신 404 JSON. 데이터는 `/api/web/*` JSON API(`web_api_routes`·
`web_auth_routes`)가 담당하고 `require_session` 으로 401 게이트한다. 인증 라우팅(setup·
pending·login)은 **SPA 루트 레이아웃이 `/api/web/me` 응답으로 단일 결정**한다 — `auth_gate`
미들웨어는 경로별 리다이렉트를 하지 않고, active·비차단 세션만 `request.state.user` 에
싣고 pending 은 `/me`·i18n·auth 외 `/api` 를 403 으로 막는다. 아카이브 콘텐츠를 직접
서빙하는 자원 라우트(스냅샷 파일·문서·인증서·diff·확장)는 `_require_viewer` 로 로그인을
직접 강제한다(`/resource/` CAS 만 예외 — 원칙 5).

## 렌더링·서빙 보안 (아키텍처 원칙 5)

**대시보드는 기본 loopback, 외부 노출 시 인증 필수.** 기본 바인딩 127.0.0.1.
컨테이너 등 포트포워딩이 필요한 환경에서만 `WCCG_HOST` 로 바인딩을
오버라이드하며(compose 가 0.0.0.0 주입), 호스트 노출은 항상 127.0.0.1
포트 매핑으로 제한한다. `WCCG_AUTH=off` 는 loopback 바인딩일 때만 허용
(`cli.serve` 가 강제 — 컨테이너의 0.0.0.0 바인딩에서는 인증이 항상 켜진다).
아카이빙된 HTML을 렌더링할 때는 반드시 `<iframe sandbox>` (스크립트 실행 금지)
안에서만 보여준다. 아카이빙된 페이지의 JS를 대시보드 컨텍스트에서 실행하는
일은 절대 없어야 한다. 허용하는 유일한 sandbox 토큰은
`allow-top-navigation-by-user-activation` — page.html 의 재작성된 앵커
(크롤 `/crawl/{id}/goto`, 단일 페이지 `/goto?url=...` — 둘 다 `target="_top"`)를
사용자가 직접 클릭했을 때만 뷰어 전체가 이동하게 한다 (스크립트로는 불가,
`allow-scripts`/`allow-same-origin` 절대 추가 금지). 두 리졸버는 아카이브된
스냅샷을 찾으면 **정식 중첩 경로**(`/archive/sites/{site}/page/{page}/snapshot/{snap}`)로
302 한다 — SPA 가 해석하는 유일한 스냅샷 경로라 구형 `/snapshot/{id}` 로 보내면
안 된다(C2 컷오버 회귀). 없으면 라이브로 새지 않고 안내 화면을 보여준다.
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

- 화면 — 현황(`/`), 목록(`/archive/list` — 사이트(서브도메인) 단위),
  사이트 상세(`/archive/sites/{id}` — 소속 페이지·문서·크롤 회차·스케줄·사이트 삭제),
  사이트 로그인 자격증명(`/archive/sites/{id}/credentials` — 관리자 전용),
  문서(`/archive/documents` — 문서 파일 통합 목록),
  휴지통(`/archive/trash` — 삭제 보류 아카이브 목록·복원·영구삭제, `manage_trash` 권한 기본
  admin, 헤더 '아카이브' 메뉴에 `can_manage_trash` 게이트로 노출 — `.claude/rules/database.md`
  `trash_entries`·`.claude/rules/authentication.md`),
  검색(`/search` — 본문·문서 전문 검색, viewer 이상), 새 아카이빙(`/archive/new`),
  사이트 아카이브 진행(`/crawls/{id}` — 크롤 회차 상세), 스케줄(`/archive/schedules`),
  타임라인(`/archive/sites/{id}/page/{pageId}`), 스냅샷 뷰어
  (`/archive/sites/{id}/page/{pageId}/snapshot/{snapId}`), diff 뷰어,
  아카이브 로그(`/log/archive`), 시스템 로그(`/log/system`), 감사 로그(`/log/audit`),
  시스템 설정(`/system/general`), 사용자(`/system/users`),
  권한 그룹(`/system/groups` — 관리자 전용, 역할 프리셋 편집·커스텀 그룹 추가/삭제),
  API 키(`/system/api-keys`),
  개인 API Key(`/settings/api-keys` — 본인 확장 토큰 발급·폐기),
  내 아카이브(`/settings/archives` — 본인이 요청한 아카이빙 이력),
  사람 확인 필요(`/archive/needs-human` — 관리자 전용, `WCCG_LIVE_CHALLENGE` 켜짐 시)·
  라이브 챌린지 처리(`/archive/jobs/{id}/live` — 관리자, 스크린샷 보고 직접 클릭/입력).
  페이지·스냅샷은 사이트 계층 아래로 중첩한다(`/archive/sites/{site}/page/{page}/
  snapshot/{snap}`) — `site_id` 를 모르는 목록 링크는 `frontend/src/lib/urls.ts`
  의 `pagePath`/`snapPath` 헬퍼로 만들고, API 응답이 행마다 `site_id`(스냅샷은
  `page_site_id`)를 함께 내려준다. 로그 3종(`/log/archive`·`/log/system`·`/log/audit`)은
  각각 `view_archive_logs`·`view_system_logs`·`view_audit_logs` 권한(기본 admin 만,
  authentication.md)이며 헤더 '로그' 드롭다운으로 묶인다. 감사 로그는 누가
  아카이빙·열람·문서 다운로드·관리 작업을 했는지 전용 `audit_logs` 테이블에서 읽는다
  (database.md). 권한이 없는 메뉴는 헤더에 표시하지 않는다 —
  `/api/web/me`(=`permissions.auth_context`)가 내려주는 노출 플래그를 SPA 루트
  레이아웃이 읽어 메뉴를 가린다(서버 권한 가드는 각 엔드포인트에서 이중 유지). 로그·
  아카이브·관리자 메뉴와 개인설정(우측 이메일/표시이름 → 계정·개인 API Key·내 아카이브·
  로그아웃)을 헤더 드롭다운으로 묶는다(`frontend/src/routes/+layout.svelte`). 크롬 확장이
  여는 대시보드 딥링크는 `/extension/*`(page·crawl·needs-human·archives·token·go)가
  정식 화면으로 302 리다이렉트해 화면 구조와 분리한다(api-extension.md). 화면별 라우트·
  권한·세부 동작은 `docs/DASHBOARD.md` 참조.
- **Tailwind v4 + shadcn-svelte 기반의 모던하고 밀도 있는 UI.** UI 를 만들 땐
  shadcn 프리미티브(`frontend/src/lib/components/ui` — Button·Badge·Input·
  Dialog·DropdownMenu·Select·Tabs·Switch 등)와 공통 래퍼
  (`frontend/src/lib/components` — Card·Field·FormSection·Toolbar·Pager·
  AlertBox·Toggle·Segmented·ChipGroup 등)를 **우선 재사용**하고, 일회성 스타일은
  Tailwind 유틸로 쓴다(scoped `<style>` 는 라우트 고유 레이아웃에만 최소한으로).
- **색은 `app.css` 의 시맨틱 토큰만 사용(직접 hex 금지).** shadcn 표준 토큰
  (background/foreground/primary/secondary/muted/accent/destructive/border/ring)
  + 춘추관 상태 색 — 변경=amber(`changed`), 동일=gray(`same`), 신규=green(`new`),
  실행=blue(`running`), 오류=red(`error`), 관인(`seal`). 상태 뱃지는
  `<Badge variant="changed|same|new|running|error">`, 모노스페이스(`.mono`/
  `font-mono`)로 해시·시각 표기. 다크모드는 `.dark` 클래스(mode-watcher)가
  토큰을 치환하므로 토큰만 쓰면 자동 대응된다.
- 모던하게: 적절한 여백·라운드(`--radius`)를 쓰되 과한 그라데이션·장식은 지양,
  데이터 테이블은 밀도를 유지한다(컴팩트). 새 컴포넌트가 필요하면
  `npx shadcn-svelte@latest add <name> -y` 로 추가한다.
- 다국어(ko/en): `web/i18n.py` 가 정본 카탈로그(한국어 원문이 메시지 키, gettext msgid
  방식 — 언어별 "원문 → 번역" dict). SPA 는 `frontend/src/lib/i18n.ts` 의 `t('…')` 로
  쓰고, 로케일 카탈로그는 `/api/web/i18n/{locale}` 로 받아 `setCatalog` 주입한다(ko 는
  패스스루). 백엔드 오류 메시지(`HTTPException` detail)는 `app.py` 의 경계 예외
  핸들러(`_translate_http_exception`)가 요청 로케일로 **자동 번역**한다 — 라우트는
  한국어 원문을 그대로 raise 해도 되고(카탈로그에 있으면 번역, 없으면 원문 통과),
  로케일은 미들웨어가 적재한 `request.state.locale`(로그인 사용자는 저장 로케일,
  그 외 Accept-Language)을 쓴다. 명시 번역이 필요하면 여전히 `i18n.t(request, "…")`
  로 감싸도 된다(경계에서 재번역돼도 무해). **새 SPA 문자열·라우트 오류 메시지 추가
  시 `web/i18n.py` 의 en 카탈로그도 채울 것** — `.svelte`/`.ts` 의 `t('…')` 리터럴 키
  누락은 `tests/test_i18n.py` 가 검사한다(en `CATALOGS` 대조). CLI 는 한국어 유지.
- diff 뷰: 텍스트 side-by-side + 스크린샷 비교(슬라이더 또는 토글). 단, 비교
  대상 중 하나라도 확장(브라우저) 캡처(`origin=extension`)면 스크린샷 비교를
  숨기고(로컬 해상도·dpr 의존이라 무의미) 본문 diff 에 렌더 환경 차이 경고를 단다.
- 브라우저 클라이언트 캡처: 확장이 캡처한 스냅샷은 "브라우저 캡처"·"불완전"
  뱃지로 표시한다. 캡처/적재(ingest) 메커니즘과 REST API 상세는
  `.claude/rules/api-extension.md` 참조.

> 권한 가드(`web.permissions.has_permission`·역할 프리셋)는 `.claude/rules/authentication.md`,
> 화면이 노출하는 DB 테이블 상세는 `.claude/rules/database.md` 참조.
