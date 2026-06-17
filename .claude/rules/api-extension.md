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
  소유자에게 권한 없으면 401) 양쪽. 시스템 키(owner=NULL)는 이 권한과 무관하게
  저장 컬럼만 본다. 빌트인 기본 보유는 admin·archive_manager·archiver, viewer 제외
  (상세 → `.claude/rules/authentication.md`). REST 쓰기 엔드포인트: `/archive`·`/crawl`·
  `/auth-profiles`(모두 URL 만 받아 서버가 캡처)와 **`/ingest`**(확장이 브라우저에서
  직접 캡처한 멀티파트 산출물을 받아 `ingest.py` 가 서버 무요청으로 적재 — 사용자
  귀속 토큰 전용, 동기 응답, 사설 호스트 무태그면 422 `needs_network_tag`),
  `/network-tags`(GET 목록 / POST 생성=`manage_system`). 상세는 `docs/API.md`·
  `docs/EXTENSION_CLIENT_CAPTURE_PLAN.md`
