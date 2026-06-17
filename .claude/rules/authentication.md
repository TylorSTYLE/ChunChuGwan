---
description: 인증 — 인증 데이터 저장 규칙(원칙 6)·역할/권한·자격증명·OIDC·SMTP. 인증/자격증명/암호화 모듈을 만질 때.
paths:
  - "chunchugwan/auth.py"
  - "chunchugwan/oidc.py"
  - "chunchugwan/credentials.py"
  - "chunchugwan/crypto.py"
  - "chunchugwan/mailer.py"
  - "chunchugwan/web/auth_routes.py"
  - "chunchugwan/web/permissions.py"
  - "chunchugwan/migration.py"
  - "chunchugwan/web/migration_routes.py"
  - "docs/AUTHENTICATION.md"
---

# 인증

기술 스택: argon2-cffi(패스워드), pyotp+qrcode(TOTP), webauthn(패스키),
httpx+PyJWT(OIDC — Authentik).

## 인증 데이터 규칙 (아키텍처 원칙 6)

패스워드는 Argon2id 해시만, 세션·API 키는 토큰의
SHA-256 만 저장 (세션은 서버사이드). 2FA(TOTP·패스키)는 패스워드 로그인에만 적용하고 SSO(OIDC)는
IdP 의 2FA 를 신뢰한다. 패스키는 공개키만 저장하며 RP ID/origin 은
`WCCG_PUBLIC_URL` 에서 파생(미설정 시 localhost). 환경변수 목록은
`docs/AUTHENTICATION.md` 참조. 단, 위 단방향 저장 규칙은 춘추관이 **사용자를**
인증하는 데이터(로그인 비밀번호·세션·API 키·패스키)에 한한다.
아카이빙 대상 사이트에 춘추관이 **로그인하기 위한 외부 자격증명**(세션
쿠키·Basic 인증·Bearer 토큰)은 재생(replay)이 필요해 복원 가능해야
하므로, 예외적으로 **대칭 암호화**로 저장한다 (평문·해시 금지). 암호화
키는 환경변수(`WCCG_SECRET_KEY`)에서만 오고 DB·저장소에 들어가지
않으며, 키 부재 시 이 기능만 비활성화되고 기존 아카이빙은 영향받지
않는다. 변조 감지가 되는 인증 암호화(AES-GCM 등)를 쓰고, 자격증명은
사이트 단위로 스코프한다. `backup` 에는 다른 인증 데이터처럼 포함하되
`export` 에는 제외한다. 이것이 양방향(복원 가능) 저장을 허용하는
**유일한** 예외이며, 사용자 인증 데이터에는 절대 적용하지 않는다.

## 최초 설정 · 춘추관 간 이전

최초 구동(사용자 0명 + `WCCG_ADMIN_*` 미설정)의 `/setup` 은 세 갈래 — 관리자
생성 / 백업 업로드 복원(`POST /setup/restore`) / **다른 춘추관에서 네트워크 이전**.
모두 `count_users==0` 일 때만 동작하고, `/setup/*` 흐름은 미들웨어 first_run
게이트가 허용한다(`web/app.py`). 이전은 받는 쪽(목적지)이 소스 URL + 토큰으로
Pull 하는 백그라운드 작업(`migration.start_pull`/`pull_status`/`retry_failed`/
`finish_pull`, 재색인 패턴의 모듈 상태+스레드). 파일 단위 전송이라 파일별 3회
재시도 후 실패 목록 → [전체 재시도]/[무시하고 종료]. 받은 DB 가 반영되면
`backup.finalize_migration` 이 루트를 교체하고 **이전 모드를 끈다**(소스 DB 가
켜진 채라 받는 쪽이 그대로 시작하지 않도록). 이전 토큰은 세션·API 키와 같이
**SHA-256 해시만 저장**(원칙 6 단방향, `db.set_migration_mode`/`get_migration_token_hash`),
원문은 발급 시 1회만 노출(소스 시스템 화면). 받는 쪽 워커는 토큰을 메모리에만
두고 `pull_status` 에서 제외한다. 스크래핑 중단 게이트는 `.claude/rules/capture-crawl.md`,
Pull 엔드포인트는 `.claude/rules/api-extension.md` 참조. `site_credentials` 는
`WCCG_SECRET_KEY` 로 암호화돼 전송되나 키는 전송 안 되므로 받는 쪽이 같은 키를
써야 복호화된다(다르면 그 기능만 비활성 — 기존 백업/복원과 동일).

## 관련 DB 테이블

- `users` / `identities` / `sessions` / `oidc_states` /
  `email_verifications` — 인증 (사용자, OIDC 연결, 서버사이드 세션, OIDC
  state 1회용 기록, 이메일 인증 코드). `users.role` 은
  admin(관리자)/archiver(아카이빙 가능)/viewer(보기 전용)/pending(권한없음 —
  가입 승인 대기, 로그인은 되지만 `/pending` 안내 페이지 외 접근 불가)/
  blocked(차단)/withdrawn(탈퇴 — 본인 탈퇴로만 진입, 로그인 거부.
  관리자가 부여하거나 되돌릴 수 없고, 사용자 관리에서 계정 정보를
  삭제(대상 이메일 입력 확인)해야 같은 이메일 재가입·초대가 풀린다).
  신규 가입·SSO 자동 생성의 초기 권한은 `settings` 의
  `signup_default_role` (pending/viewer/archiver, 기본 pending — 관리자가
  사용자 관리에서 권한을 부여해 승인). `users.is_founder` 는 최초 등록
  관리자로 권한 변경 불가. **권한은 세분 권한(`db.PERMISSIONS` — view·archive·
  delete·manage_credentials·manage_system·manage_users·view_authenticated_all·
  use_api_keys, 고정 코드 상수)을 1차 단위로, 역할은 그 묶음의 프리셋으로** 둔다.
  역할 프리셋은 코드 상수가 아니라 DB `permission_groups` 테이블이 정본 — 관리자가 시스템 →
  권한 그룹(`/system/groups`)에서 빌트인(admin/archive_manager/archiver/viewer) 권한
  묶음을 편집하거나 커스텀 그룹을 추가·삭제할 수 있다(코드 배포 불필요). 코드에서는 `db.role_presets(conn)`
  (모듈 캐시 + settings 단조 버전으로 멀티프로세스 staleness 방지, conn 없는
  `web.permissions` 는 인증 미들웨어가 워밍한 캐시를 읽음)으로 읽는다. 역할만
  쓰면 동작은 종전과 동일하고, `users.permission_overrides`(JSON {권한:bool},
  프리셋과 다른 항목만) 로 사용자별 가감한다. 실효 권한 = 프리셋 ± 오버라이드
  (`db.effective_permissions`), 모든 라우트·메뉴 가드는 `web.permissions.
  has_permission`(실효 권한)으로 판정해 한 곳의 변경이 전 경로에 반영된다.
  pending/blocked/withdrawn 은 권한 묶음이 아니라 접근 게이트 상태라
  `permission_groups` 가 아닌 코드 상수(`db.STATE_ROLES`)로 남고 삭제·편집 불가다.
  역할을 바꾸면 오버라이드는 새 프리셋으로 초기화되고, `manage_users` 마지막
  활성 보유자에게서는 그 권한을 떼거나 역할을 낮출 수 없다(잠김 방지 —
  `db.count_active_users_with_permission`). 개인 API Key(확장 토큰) 발급·사용은
  세분 권한 `use_api_keys` 로 게이트한다 — 발급 화면(`/settings/api-keys` GET/POST,
  `web.permissions.can_use_api_keys`)과 소유자 귀속 토큰의 `/api/v1` 사용
  양쪽이며, `_api_auth` 가 매 요청 소유자 실효 권한을 재평가해 권한 회수 시 기존
  토큰도 401 로 막는다. 빌트인 기본값은 admin·archive_manager·archiver 가 보유,
  viewer 는 제외(크롬 확장 캡처가 이 토큰을 쓰므로 권한 없는 그룹은 확장도 불가).
  CLI 는 권한 체계 밖(로컬 신뢰), 큐·스케줄·크롤은 등록 시점에만 검사.
  `users.email_verified` 는 이메일 본인 인증 완료 여부 — 기능(`settings` 의
  `email_verification_enabled`)이 켜지고 SMTP 가 설정됐을 때, 패스워드 계정은
  로그인 마무리 전에 메일로 받은 코드로 이메일을 검증해야 한다 (SSO 계정은
  IdP 신뢰로 제외). 인증 전 세션은 `pending_email_verify` 상태(2FA 의
  `pending_totp` 와 같은 방식)로 머무르고, 코드 확인 시 active 로 승격된다.
  강제는 `auth_routes._email_verification_required` 가 로그인·가입·2FA 마무리
  지점에서 하고, 기존 사용자는 개인 설정에서 직접 인증한다 (소급 차단 없음)
- `permission_groups` — 역할 프리셋(권한 묶음)의 정본 테이블. `name` PK(=`users.role`
  에 저장되는 정규화 키 `[a-z0-9_]`), `label`(표시 라벨), `permissions`(JSON 배열,
  `db.PERMISSIONS` 부분집합), `is_builtin`(admin/archive_manager/archiver/viewer=1 —
  삭제·개명 불가, permissions 만 편집), `sort_order`. `_migrate` 가 빌트인 4개를
  `INSERT OR IGNORE` 로 멱등 시드 — 신규 설치 기본값은 archive_manager(아카이브 관리)
  =보기·아카이빙·삭제·use_api_keys, archiver(아카이브)=보기·아카이빙·use_api_keys,
  viewer=보기. use_api_keys 는 신규 추가 권한이라 기존 설치의 그룹 JSON 에는 없어
  `_migrate_api_key_permission` 이 admin·archiver 에 멱등 보강하고(archive_manager 는
  시드가 포함), 기존 archiver 의 삭제 권한은 그대로 둔다(신규 설치만 삭제 제외). 쓰기(CRUD — `db.create/update/delete_permission_group`)마다 settings
  의 `permission_groups_version` 을 +1 해 `db.role_presets` 캐시를 무효화한다. 관리자
  화면은 `/system/groups`. 역할 목록 접근자(`permission_group_names`/`assignable_roles`/
  `invitable_roles`/`signup_roles`/`role_labels`/`all_valid_roles`)가 이 테이블을 읽어
  종전 코드 상수(ROLE_PRESETS/PERMISSION_ROLES/ASSIGNABLE_ROLES 등)를 대체한다
- `email_verifications` — 이메일 인증 코드 (user_id PK = 사용자당 1개,
  재발송 시 교체). 코드 원문은 메일로만 보내고 SHA-256 해시만 저장(만료
  시각 포함, 세션·API 키와 같은 단방향 — 원칙 6). 인증 완료·계정 삭제 시 삭제.
  재생성 가능한 휘발성 데이터라 `export` 제외
- `webauthn_credentials` — 패스키 공개키 자격증명 (2FA 용)
- `site_credentials` — 아카이빙 대상 사이트 로그인용 외부 자격증명 (사이트별,
  `kind` = http_basic/session/jwt, 라벨 UNIQUE). 비밀은 `WCCG_SECRET_KEY` 로 대칭
  암호화한 암호문(`secret`)만 저장 (`crypto.py` — 원칙 6 예외, replay 위해
  복원 가능). 관리자 전용 `/sites/{id}/credentials`(+ 새 아카이빙 화면의
  선택 섹션)에서 관리하고 쓰기는 `credentials.py` 코어 모듈을 거친다. session
  종류는 storage_state JSON 을 직접 넣는 대신 로그인 상태로 기록한 HAR 파일을
  올려 쿠키를 추출할 수 있다 (`credentials.storage_state_from_har` — HAR 의
  cookies 배열·요청 Cookie 헤더에서 최종 쿠키 상태를 모아 storage_state 로
  변환. 무관한 서드파티 쿠키가 섞이지 않게 **대상 사이트의 등록 도메인**
  쿠키만 남긴다(origin 스코프 원칙과 일관 — 외부 IdP 등 다른 등록 도메인
  SSO 는 JSON 직접 입력), localStorage 는 미포함).
  크롬 확장의 '로그인 정보 포함'은 페이지의 인증용 JWT(localStorage/
  sessionStorage 의 Bearer 토큰)를 감지하면 jwt, 아니면 세션 쿠키를 보내
  방식을 자동 판단하고, 서버가 1회성 자격증명(`jwt`/`session`)을 만든다
  (`_ephemeral_credential` — https·사용자 토큰 가드, 캡처 후 폐기).
  사이트 prune·삭제 시 함께 정리(FK), 삭제 시 이 자격증명을 연결한
  `pages.credential_id` 도 NULL 로 끊는다. 새 아카이빙 폼은 입력 URL 의
  도메인 자격증명을 조회(`/archive/credentials`)해 골라 페이지에 연결할 수
  있다. 아카이빙 시 캡처가 페이지의 자격증명을 reveal 해 Playwright
  컨텍스트에 종류별로 주입한다 — http_basic→http_credentials,
  session→storage_state, jwt→대상 origin 요청에만 Authorization 헤더
  (context.route). 자격증명이 페이지의 서드파티 하위 자원(CDN 등)으로 새지
  않게 모두 **대상 origin 으로 스코프**한다 (`capture._context_options`).
  키 부재·복호화 실패·삭제된 자격증명이면 인증 없이 진행한다(graceful).
  크롤(사이트 전체)은 `crawls.credential_id`/`crawl_schedules.credential_id` 로
  전 페이지에 적용하고(network_tag_id 와 같은 경로), 문서 다운로드는 httpx 에
  종류별 인증을 싣는다 (`credentials.httpx_auth` — Basic/Bearer 는 Authorization
  헤더, 세션은 쿠키; 모두 대상 origin 으로 스코프해 서드파티 문서로 누수 방지)

> SMTP(`smtp_*`) 등 인증 관련 런타임 설정 카탈로그는 `.claude/rules/database.md` 의
> `settings` 항목 참조. REST API 키(`api_keys`)는 `.claude/rules/api-extension.md` 참조.
