# 구현 로드맵 (완료 — 히스토리)

모든 마일스톤이 완료되어 CLAUDE.md 에서 이 문서로 옮겨졌다.
향후 새 마일스톤이 생기면 진행 중인 항목만 CLAUDE.md 에 두고,
완료되면 여기로 내린다.

각 마일스톤 완료 시: 테스트 통과 확인 → 체크박스 갱신 → 커밋.

- [x] **M1 코어 저장소**: `config.py`, `db.py`, `storage.py` 완성 + 테스트.
      URL 정규화(쿼리 정렬, fragment 제거, 트래킹 파라미터 utm_* 제거 등) 포함.
- [x] **M2 캡처**: `capture.py` — Playwright로 렌더링 → raw.html, 전체 스크린샷,
      자원 인라인 page.html(이미지/CSS를 base64 인라인. 1차 버전은 스타일시트와
      이미지까지만, 폰트는 M5). `extract.py` — 본문 텍스트 추출(DOM 가시 텍스트,
      2026-06 trafilatura 에서 교체 — 기사·게시글 제목/본문 유실 때문) + 정규화. `cli.py`의 `add` 연결. 실제 URL 1개로 수동 검증.
- [x] **M3 히스토리/diff**: `differ.py` — difflib unified + side-by-side 데이터,
      변경 요약(추가/삭제 줄 수). `cli.py`의 `history`, `diff`, `list` 연결.
- [x] **M4 대시보드**: `web/app.py` + 템플릿 4종. 재아카이빙 버튼은
      BackgroundTasks로 코어 호출.
- [x] **M5 고도화**: 스크린샷 픽셀 diff(Pillow), 폰트 인라인, 도메인별 정규화
      룰(셀렉터 제거 목록) 설정 파일, robots.txt 무시.
- [x] **A1 인증 코어**: users/sessions 스키마, `auth.py`(argon2·세션·TOTP).
- [x] **A2 로그인/가입**: `web/auth_routes.py`, 인증·CSRF 미들웨어, 라우트 보호.
- [x] **A3 TOTP 2FA**: QR 등록/해제, 2단계 로그인 (패스워드 로그인에만 적용).
- [x] **A4 OIDC SSO**: `oidc.py` — Authentik Authorization Code Flow, 계정 연결.
- [x] **A5 외부 노출 준비**: `serve --host`, auth-off×외부 바인딩 거부, 보안 헤더.
- [x] **A7 최초 구동 부트스트랩**: 사용자 0명이면 `WCCG_ADMIN_*` 환경변수로
      관리자 자동 등록, 없으면 `/setup` 등록 페이지 (등록 후 페이지·API 차단).
- [x] **A8 패스키 2FA**: WebAuthn 자격증명 등록/삭제(`/settings/passkey`),
      2단계 로그인에서 TOTP 와 병행 (둘 중 하나만 있어도 2단계 발동).
- [x] **M6 백업/복원**: `backup.py` — 전체 백업/복원(`wccg backup`/`restore`:
      DB 일관 복사 + sites + rules.json 을 tar.gz 로, 인증 데이터 포함, 복원은
      루트 전체 교체). 아카이브 데이터만 내보내기/가져오기(`wccg export`/
      `import --mode merge|overwrite`: pages·snapshots·checks + 스냅샷 파일만 —
      인증 테이블·실행 로그 제외, merge 는 dir_name 기준 중복 스킵).
      대시보드 시스템 메뉴(`/system`, 관리자 전용)에서도 동일 기능 제공.
- [x] **M7 주기적 재아카이빙**: `scheduler.py` — 페이지별 반복 주기(최소 1시간
      ~ 최대 1개월) 등록, `schedules` 테이블. CLI `wccg schedule
      add/list/next/remove/run`, serve 프로세스의 백그라운드 폴링 스레드
      (`WCCG_SCHEDULER=off` 로 비활성), 대시보드 타임라인에서 설정/해제 +
      다음 실행 시각 직접 변경.
      실행은 pipeline 공유 (archive_logs source='schedule').
- [x] **A9 사용자 권한**: `users.role`(admin/archiver/viewer/blocked) +
      `is_founder`(최초 관리자 — 권한 변경 불가). 신규 가입·SSO 자동 생성은
      viewer(이후 A10 에서 설정 가능한 초기 권한으로 대체 — 기본 pending).
      viewer 는 아카이빙 트리거·아카이브 삭제 403 (삭제는 admin/
      archiver 만 가능), blocked 는 로그인 거부 + 기존
      세션도 미들웨어가 차단. 관리자 전용 사용자 관리 화면(`/system/users`)
      에서 권한 조정 (차단 시 대상 세션 즉시 삭제). 권한 판정은
      `web/permissions.py` 헬퍼로 일원화 (라우트 가드·템플릿 노출 공용).
- [x] **M8 웹 UI 다국어**: `web/i18n.py` — ko/en 카탈로그(한국어 원문 키),
      쿠키(`wccg_lang`) + Accept-Language 로케일 결정, 헤더 언어 선택
      (`POST /lang`), 주기 표기 로케일화(`i18n.format_interval`). 템플릿 전체
      `_()` 적용 + 라우트 메시지 `i18n.t()` 번역. 향후 언어 추가 = dict 추가.
- [x] **A10 가입 승인**: `users.role` 에 pending(권한없음 — 가입 승인 대기)
      추가. pending 계정은 로그인 후 `/pending` 안내 페이지·로그아웃·언어
      전환만 가능 (미들웨어가 그 외 전부 `/pending` 으로 리다이렉트).
      `settings` 테이블(key-value) 신설 — 시스템 화면의 가입 설정에서 회원
      가입 허용(`signup_enabled`, off 면 `/signup` 차단 + 로그인 화면 가입
      링크 숨김, 초대 가입은 허용)과 가입 초기 권한(`signup_default_role`:
      pending/viewer/archiver, 기본 pending) 관리. SSO 자동 프로비저닝도
      같은 초기 권한을 따른다 (승인 절차 우회 방지). 승인 = 관리자가
      사용자 관리에서 권한 부여.
- [x] **A11 사이트 단위 아카이브 구조**: `sites` 테이블 — 서브도메인 단위
      그룹(site_key = www 제거 호스트 + 기본 외 포트, `storage.site_key`).
      모든 페이지·크롤·크롤 스케줄이 사이트에 속하고(생성 시 자동 연결,
      기존 데이터는 `db._migrate` 가 자동 백필), 크롤 범위(in_scope)도
      www↔apex 를 같은 사이트로 취급. `/archives` 를 사이트 단위로 재편 +
      사이트 상세 화면(`/sites/{id}`) 신설, 사이트 단위 삭제(웹 +
      `wccg delete --site`, 마지막 소속 행 삭제 시 사이트 행 자동 정리).
      `snapshot_resources` — 스냅샷의 공유 자원 참조 인덱스(캡처 시 원본
      URL 포함 기록, 삭제 시 참조 0 인 CAS 파일 GC, 자원 인라인 실패 시
      같은 URL 의 과거 캡처본 재사용 폴백). 저장공간 최적화 — `wccg
      compact` 를 압축 변환 + 참조 백필 + 고아 자원 정리(sweep, 유예 창 +
      삭제 직전 재확인)로 확장, 시스템 메뉴 라벨을 "저장공간 최적화"로 변경.
- [x] **A12 확장 결과 알림**: 크롬 확장이 요청한 아카이빙의 결과를 데스크톱
      알림으로 받는다. 단발 작업은 `archive_logs.job_id`(작업 행은 완료 시
      삭제되므로 FK 없는 상관 키, `_RunLog`→`insert_archive_log` 로 적재)로,
      크롤은 `crawls.requested_by`(요청자 귀속)로 이어, 신규
      `GET /api/v1/archive/status?jobs=…&crawls=…`(소유자 스코프)가 완료/실패/
      사람확인·크롤 완료/취소 상태를 돌려준다 (활성 작업이 있으면 우선,
      없으면 로그로 종결 상태 도출). `/archive`·`/auth-profiles` 응답에 `job_id`
      추가. 확장은 `chrome.alarms` 주기 폴링 + `chrome.notifications`(전이 시
      1회, 배지·딥링크 클릭, "작업이 끝나면 알림 받기" 토글, 기본 켜짐)로 표시 —
      manifest 에 notifications·alarms 권한 추가(업데이트 시 재승인).
- [x] **A13 확장 — 로컬 시간 표시 + 로그인 방식 자동 판단**: (1) 확장 히스토리의
      스냅샷 시각을 브라우저(시스템) 시간대로 표시(`formatLocalTime`, ISO→
      `toLocaleString`). (2) '로그인 정보 포함' 캡처 시 페이지의 인증용
      JWT(localStorage/sessionStorage 의 Bearer 토큰)를 감지하면 jwt, 아니면
      세션 쿠키를 보내 방식을 자동 판단 — 확장은 `scripting` 권한으로
      `chrome.scripting.executeScript` 스캔(인증 힌트 키의 JWT 만 채택),
      서버 `/api/v1/auth-profiles`·`/crawl` 에 `jwt` 필드 추가 +
      `_ephemeral_credential` 이 jwt/session 1회성 자격증명 생성(캡처가 jwt 는
      대상 origin Authorization: Bearer 로 주입). 토글에 감지 방식 미리보기 표시.
- [x] **A14 브라우저 클라이언트 캡처**: 크롬 확장이 서버를 거치지 않고 현재
      페이지를 브라우저에서 직접 캡처(`chrome.debugger` CDP 풀페이지 +
      자원·문서 재요청, `cache:'force-cache'`)해 인라인 완성한 산출물을
      `POST /api/v1/ingest`(멀티파트, 동기) 로 올리면, 서버 `ingest.py` 가 대상
      URL 을 다시 가져오지 않고(capture 미실행) 기존 코어(extract·resources·
      storage·db·searchindex)로 적재. 업로드 바이트는 자원 미디어 타입·문서
      확장자 화이트리스트로 재검증, 자격증명 미저장. `snapshots.origin/incomplete`·
      `pages.client_captured` 컬럼, "브라우저 캡처"·"불완전" 뱃지, 로컬 캡처가
      낀 diff 는 스크린샷 비교 비활성+경고. 적재 페이지는 서버 재요청 차단
      (`pipeline._archive_url` 백스톱 + `enqueue_archive_job` 가드). 사설 호스트는
      `/api/v1/network-tags` 로 태그 선택/추가(생성 `manage_system`). 설계:
      `docs/EXTENSION_CLIENT_CAPTURE_PLAN.md`. 서버측은 테스트로 검증(test_ingest·
      test_extension_api), 확장 런타임(CDP·업로드)은 언팩 로드 후 수동 테스트 필요.
- [x] **A15 아카이브 휴지통**: 페이지·사이트 삭제를 즉시 영구삭제 대신
      **휴지통(소프트 삭제)**으로 보낸다 (`deletion.py` — soft/hard/restore/purge).
      `trash_entries` 테이블 + pages/crawls/crawl_schedules 의 `trash_id` 컬럼으로
      삭제 보류 항목을 표시하고, 모든 목록·검색·뷰어·문서·서빙에서 숨기되 스냅샷
      파일·공유 자원/문서 CAS 는 보존한다. 시스템 설정의 보관 기간
      (`trash_retention_days`, 기본 30일, 0=자동삭제 끔)이 지나면
      `scheduler.run_due` 자동 purge 훅이 영구삭제하고, 그때 비로소 참조 0 인 CAS
      를 GC 한다. 휴지통 기능 자체는 `trash_enabled`(기본 on)로 끌 수 있고(끄면
      삭제가 즉시 영구삭제 — 종전 동작), 단일 스냅샷 삭제(`--snapshot`·
      `delete_snapshot`)는 범위 밖(항상 즉시 삭제). CLI `wccg delete --hard`(즉시
      영구삭제) + 새 그룹 `wccg trash list/restore/purge`. 대시보드 휴지통 화면
      (`/archive/trash`, 새 세분 권한 `manage_trash` — 기본 admin)에서 열람·복원·
      영구삭제, 시스템 설정에 '휴지통' 섹션(`POST /system/trash-settings`).
      전체 백업은 휴지통 항목을 보존, 내보내기(`export`)는 제외. 휴지통에 있는
      URL 을 다시 아카이빙하면 자동 복원(숨겨진 페이지에 스냅샷 누적 방지).

## SvelteKit SPA 전환 (#13 — C2 빅뱅 컷오버 완료)

- [x] **Phase A~C1 + 보강**: 기존 Jinja2 SSR 대시보드 전 화면을 SvelteKit SPA(`frontend/`,
      Svelte 5 + adapter-static)로 재구축하고, 데이터 계층을 `/api/web/*` JSON API
      (`web_api_routes`·`web_auth_routes`)로 분리. 인증(로그인·2FA·패스키·가입·이메일
      인증·최초설정·네트워크 이전)·읽기 10화면·쓰기 액션·관리(사용자·권한그룹·시스템
      설정·네트워크태그·SMTP·백업/복원/내보내기/가져오기·compact/재색인·이전 모드)를
      JSON+SPA 로 보강. 인증 라우팅은 SPA 루트 레이아웃이 `/api/web/me` 로 단일 결정.
- [x] **C2 빅뱅 컷오버**: `svelte.config.js` base `/ui`→`''`, SPA 를 루트(/)로 서빙
      (`app.py` catch-all). SSR 전면 제거 — Jinja 템플릿 43개·`templating.py`·SSR HTML
      라우트(app/auth/system_routes)·`system_routes.py`·`jinja2` 의존 삭제. `auth_gate`
      미들웨어 단순화(리다이렉트 제거→SPA 권위, pending /api 게이트), 아카이브 자원
      라우트에 `_require_viewer` 가드, 초대 수락 SPA 흐름 추가, 실패 재시도·사이트 export
      를 `/api/web` 으로 보강. 공용 헬퍼(tar_download·재색인 상태)는 `web/maintenance.py`.
      SSR 테스트는 `/api/web` 스위트로 대체.
