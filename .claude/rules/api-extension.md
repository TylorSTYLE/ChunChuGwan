---
description: REST API(/api/v1)·API 키·브라우저 확장 클라이언트 캡처(ingest). api_routes/ingest/extension 을 만질 때.
paths:
  - "chunchugwan/web/api_routes.py"
  - "chunchugwan/web/migration_routes.py"
  - "chunchugwan/migration.py"
  - "chunchugwan/ingest.py"
  - "chunchugwan/extension/**"
  - "docs/API.md"
  - "docs/EXTENSION_CLIENT_CAPTURE_PLAN.md"
---

# REST API · 확장 클라이언트 캡처

## 춘추관 간 이전 엔드포인트 (`/api/migration/*`)

`/api/v1`(API 키) 와 **별개**의 machine-to-machine 채널 — 받는 쪽이 소스에서
전체 데이터를 파일 단위로 Pull 한다. 인증은 API 키가 아니라 **이전 토큰**
(`X-Migration-Token` 헤더, `secrets.compare_digest`, 이전 모드일 때만 유효).
미들웨어가 `/api/` 를 세션 인증에서 면제하므로 토큰만으로 접근된다.
라우트는 `web/migration_routes.py`(소스 서빙), 받는 쪽 워커·매니페스트 빌드는
`migration.py`: `GET /info`(버전·요약) / `/manifest`(DB sha256 + 파일 목록) /
`/db`(일관 스냅샷) / `/file?path=`(단일 파일, traversal 검증 — `resolve_transfer_file`).
이전 모드 게이트·토큰 저장(`db.set_migration_mode`, SHA-256 단방향)·받는 쪽
setup 흐름은 `.claude/rules/authentication.md`·`.claude/rules/capture-crawl.md` 참조.

## 브라우저 클라이언트 캡처

크롬 확장이 현재 페이지를 CDP 로 직접 캡처해
`POST /api/v1/ingest` 로 올리면 서버가 코어를 재사용해 적재한다(서버 무요청).
스냅샷에 "브라우저 캡처"·"불완전" 뱃지, 로그인 상태 캡처라 민감 정보가 모든
사용자에게 보일 수 있음을 캡처 시 고지. 서버측 구현은 `ingest.py`, 설계는
`docs/EXTENSION_CLIENT_CAPTURE_PLAN.md` (확장은 `chunchugwan/extension/`).

## 확장 버전 (서버 앱 버전과 독립)

확장은 웹스토어 미등록 unpacked 로드라 자동 업데이트가 없다. 확장 버전은
`chunchugwan/extension/manifest.json` 의 `version` 이 정본이며 **서버 앱 버전
(`__version__`)과 독립**이다 — `/extension/download` zip 은 manifest 를 덮어쓰지 않고
원본 그대로 담는다(`app._build_extension_zip`). `GET /api/v1/version` 은
`{version: 서버앱버전, extension_version: 확장버전}`(서버가 패키지 manifest 에서 읽어
캐시 — `api_routes._extension_version`)을 주고, 확장 background(`checkVersion`)가
`chrome.runtime.getManifest().version` 과 `extension_version` 을 비교해 서버가 아는
확장 버전이 더 최신이면 팝업 배너로 재설치를 안내한다(`openDownload` →
`/extension/download` 새 탭). 구버전 서버 호환: `extension_version` 없으면 비교 생략.
비교는 더 높을 때만(오탐 방지). 조회는 권한 불필요(토큰만).

**확장 버전 자동 결정 정책.** Claude Code 가 확장(`chunchugwan/extension/**`)을 변경할
때마다 영향도로 manifest `version` 을 semver 상향한다 — **major**=재설치가 필요한 호환성
영향(권한 추가/변경·API 계약·저장 형식 파괴·기존 동작 깨짐), **minor**=하위호환 신기능,
**patch**=버그·문구·리팩토링(동작 동일). 확장 독립 버전 체계는 `1.0.0` 에서 시작했다.

## 확장 클라이언트 동작 (빠른 캡처·상태 배지·태그 사전선택·진행률·자동 해제)

- **빠른 캡처**: 팝업 없이 단축키(`commands`: `archive-page-server`/`capture-page-browser`)·
  우클릭 메뉴(`contextMenus`: 페이지 아카이브·브라우저 캡처·링크 아카이브)로 즉시 아카이브.
  연결(인증)된 동안에만 활성(`setConnectedUI` 가 메뉴 생성/제거). 서버 위임 아카이브·링크
  아카이브는 대상 host 권한 불필요, 브라우저 캡처는 `*://*/*` 보유 시만(없으면 알림 + 팝업
  유도 — 비팝업 컨텍스트는 권한 요청 불가). 결과는 notify 토글과 무관하게 즉시 알림.
- **아카이브 상태 배지**: 활성 탭 URL 을 `GET /api/v1/pages?url=` 로 조회해 아카이브됐으면
  툴바 아이콘에 per-tab `✓`(단기 캐시+디바운스, 추적 개수 배지가 있으면 양보). 토글
  `status_badge_enabled`(기본 on). "변경됨"은 라이브 해시를 서버 정규화와 동일 계산 불가라 미표시.
- **네트워크 태그 사전선택**: 사설 호스트는 팝업 진입 시 태그 picker 를 미리 띄워 캡처 전에
  고르게 한다(2회 캡처 방지). 선택값을 브라우저 캡처·`/archive`·`/crawl`·`/auth-profiles` 에 전달.
- **캡처 진행률**: 브라우저 캡처가 단계(grab→resources→inline→screenshot→documents→upload)를
  `chrome.runtime.sendMessage({type:"capture_progress"})` 로 팝업에 푸시(열렸을 때만).
- **자동 연결 해제**: 모든 `/api/v1` 호출(`apiFetch`/`uploadIngest`)이 **401**(토큰 만료·
  use_api_keys 회수)을 받으면 확장이 자동 연결 해제(`handleAuthLost`)하고 팝업에 `auth_lost`
  를 알린다(연결 검증 호출만 `noAutoLogout` 예외). 429(인증보호 차단)는 연결 유지 + 재시도 안내.

## 확장 진입 경로 (`/extension/*`)

확장이 새 탭으로 여는 대시보드 딥링크는 SPA 화면 구조(중첩 라우트 등)를 직접 알지
못하도록 **`/extension/*` 진입점만** 쓰고, FastAPI(`web/app.py`, SPA catch-all 보다
먼저 등록)가 정식 SvelteKit 화면 경로로 **302 리다이렉트**한다 — 화면 경로가 바뀌어도
확장은 무관하다. `/extension/download`(세션 인증 zip) 외에:
`/extension/page/{page_id}`(→`/archive/sites/{site}/page/{page_id}`, site_id 서버 해석)·
`/extension/crawl/{id}`(→`/crawls/{id}`)·`/extension/needs-human`·`/extension/archives`
(→`/settings/archives`)·`/extension/token`(→`/settings/api-keys#ext-token-form`)·
`/extension/go?url=`(URL 의 페이지가 있으면 타임라인, 없으면 `/archive/new?url=`).
`background.js` 의 알림 클릭·딥링크는 모두 이 경로를 만든다(`clickTarget`·`openDeepLink`).

`pages.client_captured` = 확장으로 적재된 페이지 표식(`ingest.py` 가 1 로 설정) —
1 이면 서버가 그 URL 을 다시 가져오지 않는다(스케줄·크롤·재시도·재아카이빙·enqueue 차단).
`snapshots.origin=extension`·`incomplete` 의 뷰어/타임라인 뱃지와 diff 영향은
`.claude/rules/database.md`(snapshots)·`.claude/rules/dashboard.md`(diff 뷰) 참조.

## 관련 DB 테이블

- `api_keys` — 외부 소프트웨어용 API 키 (`/api/v1` REST API 인증).
  키마다 보기/아카이브 권한과 만료 시각(NULL=영구), 토큰은 SHA-256 해시만
  저장 (원문은 발급 시 1회 표시). `owner_user_id` NULL=관리자 발급 시스템
  키(공동 관리, `/system/api-keys`, `manage_users` 권한), 값=그 사용자 귀속 개인
  API Key(확장 토큰, 본인이 `/settings/api-keys` 에서 발급, 권한은 _api_auth 가
  소유자 현재 역할로 매 요청 재평가). 개인 API Key 의 발급·사용은 세분 권한
  `use_api_keys` 가 게이트한다 — 발급 화면(GET/POST 403)과 토큰 사용(_api_auth 가
  소유자에게 권한 없으면 401) 양쪽. **`/api/v1` 은 개인 키 전용** — 시스템 키(owner=NULL)는
  인증 대상이 아니다(`_api_auth` 가 401). 인증 실패는 IP 별 인증보호(`auth_throttle` 의
  `api_key_ip` 버킷, 실패 시에만 카운트, 한도 초과 429 — `_api_auth_throttle`)로 무차별
  대입을 막는다. 빌트인 기본 보유는 admin·archive_manager·archiver, viewer 제외
  (상세 → `.claude/rules/authentication.md`). REST 쓰기 엔드포인트: `/archive`·`/crawl`·
  `/auth-profiles`(모두 URL 만 받아 서버가 캡처)와 **`/ingest`**(확장이 브라우저에서
  직접 캡처한 멀티파트 산출물을 받아 `ingest.py` 가 서버 무요청으로 적재 — 사용자
  귀속 토큰 전용, 동기 응답, 사설 호스트 무태그면 422 `needs_network_tag`),
  `/network-tags`(GET 목록 / POST 생성=`manage_system`). 상세는 `docs/API.md`·
  `docs/EXTENSION_CLIENT_CAPTURE_PLAN.md`
