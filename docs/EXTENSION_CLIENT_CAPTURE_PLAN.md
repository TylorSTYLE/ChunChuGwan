# 브라우저 확장 클라이언트 캡처 — 구현 계획서

> 상태: **설계 확정 / 구현 전.** 이 문서는 검토 단계의 결정 로그 + 구현 계획이다.
> 구현이 끝나면 사용자용 내용은 `docs/API.md`·`docs/DASHBOARD.md`·`docs/STORAGE.md`·
> README 로 나누고, 완료 항목은 `docs/ROADMAP.md` 로 내린다(CLAUDE.md 컨벤션).

## 1. 개요 & 목표

크롬 확장이 **현재 보고 있는 단일 페이지를 브라우저 내부에서 직접 캡처**하고,
서버를 통하지 않고 결과를 만들어 `POST /api/v1/ingest` 로 업로드한다. 서버는 그
바이트에 대해 **기존 코어 모듈(extract·resources·storage·db·searchindex)을 그대로
재사용**해 추출·정규화·해시·중복판정·CAS 분리·검색 색인·저장을 수행한다.

**왜 이 구조인가** — "서버와 동일한 아카이빙 로직"을 클라이언트(JS)에서 바이트
단위로 재현하는 것은 `extract.py`(lxml + Python 정규식)·`resources.py`(인라인
임계값·CAS 네이밍·CSS 절대화) 패리티 위험이 커서 비현실적이다. 대신 **확장은
브라우저만 할 수 있는 일(렌더된 DOM·교차출처 프레임·이미 로드된 자원·풀페이지
스크린샷)만 수집**하고, **정규 로직은 서버가 동일하게 실행**한다. 그 결과 "동일
로직"이 정말 중요한 지점(추출/정규화/해시/CAS/색인)에서 정의상 보장된다.

### 핵심 불변식 (반드시 지킬 것)

1. **서버는 대상 URL 을 네트워크로 가져오지 않는다.** ingest 경로는 `capture.py`
   를 호출하지 않는다. 호스트 분류용 DNS 조회(`netcheck`)만 허용 — 페이지/자원
   fetch 금지.
2. **확장 캡처 페이지는 서버가 재요청하지 않는다.** 스케줄·크롤·실패 재시도·
   대시보드 "재아카이빙" 버튼이 클라이언트 출처 페이지를 서버 캡처하지 못하게
   가드한다. 갱신은 확장 재캡처로만.
3. **업로드 바이트는 신뢰 불가 입력.** 서버가 MIME 화이트리스트·경로 검증을
   재수행한다(원칙 5). 렌더는 항상 샌드박스 iframe(스크립트 금지) 안에서만.
4. **자격증명을 받지도 저장하지도 않는다.** 확장이 사용자의 실제 브라우저 세션
   으로 자원을 재요청하므로 ingest 경로에 자격증명이 흐르지 않는다(원칙 6 예외
   처리 불필요).

## 2. 결정 로그

| 주제 | 결정 |
|------|------|
| 캡처 방식 | **방법 B — `chrome.debugger` CDP `Page.captureScreenshot{captureBeyondViewport:true}`** 풀페이지 단일 페인트(서버 Playwright `full_page` 와 동일 엔진). 모바일 스크린샷 없음 |
| 보안 프로그램 안내 | 캡처 전 "웹 보안 프로그램이 있으면 캡처가 감지/방해되어 제대로 안 될 수 있음" 안내 표시 |
| 교차출처 iframe | **CDP 프레임 순회(2b)** — `Page.getFrameTree` + `Target.setAutoAttach`(OOPIF). 콘텐츠 스크립트 SecurityError 우회, 전부 브라우저 내부 처리 |
| 크로스오리진 자원 | **확장이 재요청해 인라인 충실도 확보.** `cache:'force-cache'` 우선 |
| 문서 | **문서 링크/문서 URL은 문서 처리 경로로(3b)** — 페이지가 링크한 문서까지 기존 `document_max_count`/`document_max_mb` 한도 내에서 **확장이 받아** 업로드 |
| 사설망 게이트 | **사설 대역은 network_tag 필수**, loopback 거부. 확장이 `/api/v1/network-tags` 로 목록 조회·신규 추가 |
| ingest 응답 | **동기(4a)** — 서버가 즉시 처리해 결과 반환, 폴링 불필요 |
| host 권한 | **설정 시 1회 `*://*/*` 광범위 요청(5a)** |
| 업로드 크기 | 본문 상한 + **초과 시 사용자 안내(6)** |
| 출처 공존 | **서버 스냅샷 + 확장 스냅샷 공존(B)** — 스냅샷 단위 provenance |
| 열람 권한 | **모든 viewer 공개(C)** — 캡처 시 "민감 정보가 다른 사용자에게 공개될 수 있음" 명시 |
| diff | **텍스트 diff 허용 + 경고(D).** 단, **로컬(확장) 캡처가 한쪽이라도 끼면 스크린샷 diff 비활성**(해상도·dpr·zoom 의존) |
| 재아카이빙 | 클라이언트 페이지는 서버 재아카이빙 비활성, **확장 재캡처로 안내. 단 Force(강제 저장) 유지** |
| 부분 실패 | **불완전 캡처도 "불완전" 마커와 함께 저장(1b)** |
| 권한 | manifest 에 `debugger` 권한 추가(G) |
| 캡처 불가 페이지 | `chrome://`·웹스토어·`view-source:`·타 확장 페이지 등은 캡처 버튼 비활성 + 안내(i) |
| 캡처 일관성 | 순서 고정: ① DOM 직렬화+자원 URL 수집 → ② CDP 스크린샷 → ③ 자원/문서 재요청(iii) |
| 캡처 환경 메타 | `meta.json` 에 viewport·dpr·zoom·UA 기록, 뷰어가 "로컬 캡처 (WxH @Nx)" 라벨(iv) |

### 확인 필요 — ②(재요청 차단)와 B(공존)의 해석

본 계획은 다음으로 reconcile 한다(이견 시 알려줄 것):

- provenance 는 **스냅샷 단위**(`snapshots.origin`). 한 URL 이 과거 서버 스냅샷과
  확장 스냅샷을 **함께 보유**할 수 있다(공존 = 과거 이력의 공존).
- 단 **`pages.client_captured` 플래그**가 한번 켜지면(그 페이지에 확장 스냅샷이
  처음 들어오는 순간), 그 페이지에 대한 **서버발 캡처(스케줄/크롤/재시도/대시보드
  재아카이빙 버튼)는 차단**된다. 이후 갱신은 확장 재캡처로만. 기존 서버 스냅샷은
  그대로 보이고 diff 대상이 된다.
- 근거: 확장으로 캡처하는 페이지는 LAN·로그인 상태 등 **서버가 다시 가져오면
  실패하거나 누수가 생기는** 페이지일 수 있으므로, 서버 자동 재요청을 막는 것이
  안전(불변식 1·2).

## 3. 데이터 모델 변경

### 3.1 스키마 (`chunchugwan/db.py` `SCHEMA` + `_migrate`)

- `snapshots.origin TEXT NOT NULL DEFAULT 'server'` — `server` | `extension`.
- `snapshots.incomplete INTEGER NOT NULL DEFAULT 0` — 불완전 캡처 여부.
- `pages.client_captured INTEGER NOT NULL DEFAULT 0` — 서버 재요청 차단 플래그
  (확장 스냅샷이 처음 저장될 때 1 로 설정).
- 마이그레이션: `_migrate` 가 `ALTER TABLE ... ADD COLUMN` 으로 추가(기존
  스냅샷 = `server`/complete, 기존 페이지 = 0). 기존 데이터·동작 무영향.

### 3.2 `meta.json` 추가 필드

- `origin`: `"extension"`
- `incomplete`: bool, `incomplete_reasons`: [string]
- `capture_env`: `{viewport_w, viewport_h, dpr, zoom, ua}`
- (기존 필드 url·final_url·captured_at·content_hash·documents 등은 그대로)

### 3.3 백업/내보내기 (`chunchugwan/backup.py`)

- `export_archive` 의 snapshots 직렬화에 `origin`·`incomplete` 추가, `pages` 에
  `client_captured` 추가(라운드트립).
- `import_archive` 는 누락 시 기본값(`server`/0)으로 보정.

## 4. 서버: ingest 엔드포인트 & 파이프라인

### 4.1 `POST /api/v1/ingest` (`chunchugwan/web/api_routes.py`)

- **인증**: `_api_auth`(API 키) + `_require_archive`(실효 권한) + **사용자 귀속
  토큰 필수**(`owner_user_id` not null — auth-profiles 와 동일, 시스템 키 불가).
- **요청**: `multipart/form-data`
  - `url`, `final_url`, `title`
  - `raw_html` — 최상위 문서 outerHTML(+doctype). 교차출처 iframe 은 프레임
    맵(아래 4.2 참조)으로 동봉
  - `frames[]` — `{frame_url, html}` (CDP 로 직렬화한 하위 프레임)
  - `resources[]` — `{url, content_type, bytes}` (재요청한 하위 자원)
  - `documents[]` — `{url, filename, content_type, bytes}` (링크/대상 문서)
  - `screenshot` — 풀페이지 PNG(선택)
  - `captured_at`, `capture_env`(JSON), `incomplete`(bool), `incomplete_reasons[]`
  - `force`(bool), `network_tag`(id, 선택), `is_document`(bool)
- **크기 가드**: 본문 상한(`WCCG_INGEST_MAX_MB`, 기본값 + 시스템 설정). 초과
  413 + 메시지. **`_save_upload`(무제한) 재사용 금지** — 별도 캡 적용. gzip 동봉
  시 해제 크기 한도(zip bomb 방지).

### 4.2 서버 ingest 파이프라인 (신규 `chunchugwan/ingest.py`)

`pipeline.archive_url` 과 분리된 신규 진입점 `ingest.ingest_capture(...)`. 캡처
(네트워크)만 빠지고 나머지 코어는 동일하게 재사용한다.

1. **URL 정규화·식별**: `storage.normalize_url`→`url_to_slug`/`site_key`/`page_dir`
   를 **서버가 산출**(클라이언트 바이트 패리티 불필요).
2. **네트워크 게이트(원칙 7)**: `netcheck.classify_host(host)` — DNS 조회만(페이지
   fetch 아님). loopback → 403 거부. 사설 → `network_tag` 필수(존재·사이트 스코프
   검증), 공개 → 태그 무시. 미지정 사설이면 `{needs_network_tag:true, host}` 응답.
3. **업로드 바이트 보안 재검증(원칙 5)**: 자원/문서 각각 content-type 재판정 →
   `resources._MIME_EXT` 화이트리스트 + `resources.is_valid_name`(경로 탈출) 강제.
   `text/html` 류는 `/resource` CAS 진입 거부. 문서는 문서 화이트리스트.
   수·크기는 `documents.limits` 와 자원 한도로 클램프.
4. **스냅샷 디렉토리 조립(네트워크 없음, 코어 재사용)**:
   - `raw.html.gz` — 업로드된 outerHTML(+프레임 병합).
   - `page.html` — `ingest.assemble_page_html(raw_html, resource_map, final_url)`:
     DOM 의 자원 참조(img/srcset·link css·CSS `url()`/`@import`·폰트)를 업로드된
     **resource_map(url→bytes)** 과 매칭해, 작은 자원(<`RESOURCE_MIN_BYTES` 4KB)은
     data URI 인라인, 큰 자원은 `resources` CAS(`_store`/`_store_css`,
     `_absolutize_css_refs`)로 외부화. **capture.py 가 fetch 로 하던 인라인을
     서버가 supplied bytes 로 동일하게 수행** → page.html.gz.
   - `content.md` — `extract.extract_text` + `extract.normalize`(+도메인
     `rules.json`) → **content_hash 는 서버가 계산**.
   - `screenshot.webp` — 업로드 PNG → `resources._screenshot_to_webp`(기존).
   - `meta.json` — origin/capture_env/incomplete 포함. http_status 는 확장이
     CDP `Network` 로 얻으면 기록, 못 얻으면 null.
5. **중복판정(원칙 3)**: `db.last_snapshot`(페이지 직전 스냅샷, **출처 무관**)
   content_hash 와 비교. 동일 + `force=false` → `checks` 기록(새 스냅샷 없음).
   아니면 진행.
6. **저장(원칙 1·2)**: `db.insert_snapshot(origin='extension', incomplete=...)`,
   `snapshot_resources`/`snapshot_documents` 기록, `page_dir` 에 디렉토리 작성
   (불변). 문서 본체는 문서 CAS.
7. **검색 색인**: `searchindex` 로 **즉시 색인(`search_indexed=1`)** — 익스포트
   임포트 경로의 미색인 문제 없음.
8. **`pages.client_captured = 1`** 설정(불변식 2). 첫 확장 스냅샷일 때.
9. **로그**: `archive_logs`(source=`extension`, requested_by=토큰 소유자, 단계별
   소요시간). 동기 처리라 `job_id` 없음.
10. **응답(동기, 4a)**: 200 `{page_id, snapshot_id, changed|unchanged, incomplete,
    url, view_url}`. 게이트 미충족 시 403 `{needs_network_tag, host}`.

### 4.3 문서 URL / 문서 모드 (3b)

- `is_document=true` 또는 자원이 아닌 대상 파일이면 `documents.download_direct`
  에 준하는 경로를 **supplied bytes 버전**으로 재사용: 문서 CAS 저장 + 안내
  page.html.gz + content.md(파일 sha256) + meta.json(raw/스크린샷 없음). 같은
  sha256 은 unchanged.
- 페이지가 링크한 문서는 확장이 한도 내 받아 `documents[]` 로 동봉 → 서버가 문서
  CAS + `snapshot_documents` 기록.

### 4.4 서버 재요청 차단 (불변식 2)

- 스케줄 실행(`scheduler`)·크롤(`crawler`)·실패 재시도(`archive_worker`)·대시보드
  "재아카이빙" 트리거·REST `/archive`·CLI `add` 에서 대상 페이지가
  `client_captured=1` 이면 **서버 캡처를 enqueue 하지 않고 거부/스킵**(메시지:
  "확장으로 캡처된 페이지 — 확장에서 재캡처하세요").
- Force(강제)는 **확장 재캡처 흐름의 ingest `force`** 로만 존재(서버 강제 재요청
  아님).

## 5. `GET/POST /api/v1/network-tags` (`api_routes.py`)

- `GET /api/v1/network-tags` — `{id, name, description}[]`. 권한: `_require_archive`
  (태그 사용 주체).
- `POST /api/v1/network-tags` — `{name, description}`. **`manage_system` 실효 권한
  필요**(없으면 403 — 기존 태그 선택만 가능). `db.merge_network_tags` 와 동일
  모듈(`db`)을 통해 생성.

## 6. 확장 변경 (MV3, `chunchugwan/extension/`)

### 6.1 manifest / 권한

- `permissions` 에 `"debugger"` 추가(G).
- `optional_host_permissions: ["*://*/*"]` 유지 — **설정 시 1회 요청(5a)**.
  온보딩에서 광범위 권한 + 디버깅 배너를 설명.

### 6.2 새 동작 (popup/background)

- 기존 "서버로 아카이빙"(URL 전송)과 **별도로** "이 페이지 캡처 (브라우저)" 액션
  추가.
- **사전 점검**: 캡처 불가 페이지(`chrome://`·웹스토어·`view-source:`·타 확장)
  감지 → 버튼 비활성 + 안내(i).
- **사전 안내**: "보안 프로그램이 있으면 캡처가 감지/방해될 수 있음" + "로그인
  상태로 캡처되어 민감 정보가 모든 viewer 에게 공개될 수 있음"(C).

### 6.3 캡처 시퀀스 (순서 고정 iii)

1. 콘텐츠 스크립트: 최상위 DOM 직렬화(`documentElement.outerHTML`+doctype),
   자원 URL·링크 문서 URL 수집(img/srcset·link css·CSS url()/@import·폰트).
2. `chrome.debugger` attach → CDP:
   - `Target.setAutoAttach`(flatten) + `Page.getFrameTree` → **교차출처 iframe
     직렬화**(OOPIF), 프레임 맵 구성(2b).
   - `Page.getLayoutMetrics` → 전체 콘텐츠 크기, `Page.captureScreenshot
     {captureBeyondViewport:true, fromSurface:true, clip:<full>}` → 풀페이지 PNG.
     (가시 리사이즈 최소화 위해 `clip` 사용)
   - detach.
3. 자원 재요청(background, host 권한, `cache:'force-cache'` v): 교차출처 OK,
   사용자 세션 쿠키는 브라우저 fetch 가 자동 포함. 실패는 incomplete 누적(ii).
4. 문서 재요청: 링크 문서를 한도 내 수집. 현재 URL 자체가 문서(PDF 뷰어 등 —
   content-type 판정)면 파일 바이트 + `is_document=true`(vi).
5. multipart 페이로드 구성 → **크기 캡 확인, 초과 시 안내(6)**.
6. `POST /api/v1/ingest`. 403 `needs_network_tag` → `GET /network-tags` 후 선택/
   추가(`POST`) → 태그 실어 재전송. 200 → 알림 + 스냅샷 딥링크.
- **불완전(1b)**: 일부 실패해도 `incomplete=true`+사유로 업로드.

## 7. 뷰어 / 목록 / 대시보드 (`web/templates`, `web/app.py`)

- **provenance 뱃지**: 스냅샷 목록·뷰어·타임라인에 "브라우저 캡처"(extension) 표시.
- **불완전 뱃지**: `incomplete` 스냅샷에 "불완전 캡처".
- **diff 뷰(D + 신규)**: 한쪽이라도 `origin=extension` 이면 **스크린샷 비교 UI
  숨김**(해상도 의존). 텍스트 diff 는 **경고 배너와 함께 허용**("출처/렌더 환경이
  달라 변경이 과장될 수 있음"). server↔server 는 스크린샷 비교 그대로.
- **재아카이빙 버튼**: `client_captured` 페이지는 서버 재아카이빙 버튼·스케줄 추가
  숨김/비활성 + "확장에서 재캡처" 안내.
- **민감정보 고지(C)**: 확장 캡처 뷰어에 "로그인 상태로 캡처되어 민감 정보 포함
  가능, 모든 viewer 에게 보임" 고지.
- **캡처 환경 라벨(iv)**: meta `capture_env` 로 "로컬 캡처 (1920×1080 @2x)".
- **i18n**: 신규 문자열 ko/en 카탈로그(`web/i18n.py`) 채움(`tests/test_i18n.py`).

## 8. 보안 점검 (아키텍처 원칙 매핑)

| 원칙 | 적용 |
|------|------|
| 1 쓰기는 코어로만 | ingest 는 storage/db/resources/extract/searchindex 코어만 통해 씀 |
| 2 불변 스냅샷 | 서버가 dir 작성, 이후 불변. client_captured 로 서버 재요청 차단 |
| 3·4 해시 중복제거 | content_hash 를 **서버가 계산**, 정규화 텍스트 기준 |
| 5 렌더 안전 | 업로드 자원 MIME 화이트리스트·is_valid_name 재검증, /resource 에 text/html 금지, 샌드박스 iframe 렌더 유지 |
| 6 자격증명 | ingest 는 자격증명 미수신·미저장 |
| 7 사설/루프백 | ingest 에서 netcheck 게이트, loopback 거부·사설 태그 필수 |
| (DoS) | 본문/해제 크기 상한, `_save_upload` 미사용 |
| (인증) | API 키 + archive 실효권한 + 사용자 귀속 토큰 |

## 9. 테스트 계획 (`tests/`)

- **단위**: `ingest.assemble_page_html`(resource_map→단일파일+CAS, CSS 절대화),
  netcheck 게이트(loopback 거부·사설 태그 필수·공개 태그 무시), MIME 화이트리스트
  거부(text/html→/resource 차단), 중복판정(force vs unchanged), 문서-URL ingest,
  incomplete 플래그, content_hash 가 서버 extract/normalize 와 일치.
- **API**: 인증(키 없음 401·archive 없음 403·시스템 키 거부·사용자 키 허용),
  크기 캡(413), `network-tags` GET/POST 권한(manage_system).
- **마이그레이션**: `origin`/`incomplete`/`client_captured` 추가·백필, 기존 DB
  업그레이드 무손상.
- **재요청 차단**: client_captured 페이지에 스케줄/크롤/재시도/대시보드 트리거가
  서버 캡처를 enqueue 하지 않음.
- **i18n**: 신규 문자열 누락 검사.
- 확장은 fixture 페이로드 기반 서버 단위테스트로 커버, 브라우저 동작은 수동
  체크리스트.

## 10. 마일스톤 (각 단계: 테스트 통과 → 커밋, 기능 PR 은 develop 베이스)

- **M-EXT1 — 데이터 모델/표시 기반**: DB 마이그레이션(origin·incomplete·
  client_captured), 뷰어/목록 provenance·불완전 뱃지, diff 규칙(스크린샷 비활성·
  텍스트 경고). 서버 측만, 기존 데이터 무영향.
- **M-EXT2 — 서버 ingest 코어**: `chunchugwan/ingest.py`(assemble_page_html·게이트·
  보안 재검증·dedup·색인·문서 모드). 확장 없이 fixture 페이로드 단위테스트 우선.
- **M-EXT3 — REST**: `POST /api/v1/ingest`(동기·크기캡·인증) + `GET/POST
  /api/v1/network-tags`.
- **M-EXT4 — 재요청 차단**: scheduler/crawler/archive_worker/대시보드/REST/CLI 가
  client_captured 페이지 서버 캡처 거부.
- **M-EXT5 — 확장**: debugger 권한·CDP 풀페이지·프레임 순회·자원/문서 재요청·
  `*://*/*` 설정 요청·불완전 처리·보안프로그램/민감정보 안내·network_tag 흐름·
  업로드.
- **M-EXT6 — 문서화**: API.md·DASHBOARD.md·STORAGE.md·README·CLAUDE.md 갱신,
  ROADMAP 반영, i18n 카탈로그.

## 11. 남은 확인 항목

- §2 의 **②(재요청 차단) × B(공존) 해석**(client_captured 플래그 규칙)이 의도와
  맞는지.
- 업로드 크기 상한 기본값(예: 50MB)과 자원/문서 개당 한도 재사용 범위.
- http_status 미확보 시 null 허용 여부(확장이 CDP Network 로 얻을지).
