# 인증

> 사용자 인증·권한·초대·2FA·SSO(OIDC)와 관련 환경변수를 다룬다.
> 개요와 최초 구동(관리자 등록) 요약은 [README](../README.md#인증) 참조.

## 최초 구동 (최초 설정)

사용자가 한 명도 없으면 최초 구동으로 판단한다.
`WCCG_ADMIN_EMAIL` / `WCCG_ADMIN_PASSWORD` 가 설정되어 있으면 그 값으로
관리자 계정이 자동 등록되고, 없으면 브라우저 첫 접속 시 `/setup` 최초 설정
화면으로 이동한다. 최초 설정은 세 갈래 중 하나를 고른다:

1. **관리자 계정 생성** — 이메일·패스워드로 최초 관리자(founder)를 만든다.
2. **백업 파일에서 복원** — 전체 백업(tar.gz)을 올려 그 시점 상태로 복원한다
   (`POST /setup/restore` → `backup.restore_backup`). 복원 후 백업의 계정으로 로그인.
3. **다른 춘추관에서 네트워크 이전** — 이전 모드를 켠 다른 춘추관의 주소와 토큰을
   입력해 전체 데이터를 가져온다(아래).

`/setup`(과 `/setup/*` 흐름)은 사용자가 한 명이라도 생기면 차단된다
(추가 계정은 일반 `/signup` 으로만).

## 춘추관 간 네트워크 이전 (마이그레이션)

기존 인스턴스(소스)를 새 인스턴스(목적지)로 통째로 옮기는 경로다.

- **소스**: 시스템 화면 → "다른 춘추관으로 이전"에서 **이전 모드**를 켜면 1회용
  인증 토큰이 발급된다(원문은 그때 한 번만 표시). 이전 모드인 동안 소스의 **모든
  스크래핑·스케줄·크롤이 중단**된다 (`db.migration_mode_enabled` 게이트 — 워커·
  스케줄러·크롤러·CLI·새/재아카이빙·REST 적재 모두). 토큰은 세션·API 키와 같이
  **SHA-256 해시만 저장**한다(원칙 6 단방향). 모드를 끄면 토큰이 무효화되고
  스크래핑이 재개된다.
- **목적지**: 최초 설정 화면에서 소스 주소 + 토큰을 입력하면 백그라운드로
  소스의 `/api/migration/*` 에서 데이터를 **파일 단위로 Pull** 한다 — DB·rules.json
  은 반드시 받고, 스냅샷/자원/문서 파일은 1개씩 받으며 실패 시 최대 3회 재시도한다.
  3회 실패한 파일은 실패 목록에 모으고, 전송 후 **[전체 재시도]** 또는
  **[무시하고 종료]**(빠진 파일은 뷰어에서 graceful 404)를 고른다. 받은 DB 가
  목적지에 반영되면 설정이 끝나고, 받은 쪽은 이전 모드가 자동으로 꺼진 채 시작한다.
- **인증·보안**: 이전 토큰은 이전 모드일 때만 유효하고 `X-Migration-Token` 헤더로
  검증한다(`secrets.compare_digest`). 이전은 아카이빙이 아니므로 사설 IP/루프백
  게이트(원칙 7)를 적용하지 않아 NAS·LAN 소스 주소를 쓸 수 있다. 평문 http 면
  토큰 노출 경고만 표시한다(https 권장, 강제 안 함).
- **WCCG_SECRET_KEY**: 외부 사이트 로그인 자격증명(`site_credentials`)은 이 키로
  대칭 암호화돼 전송본에 포함되지만 **키 자체는 전송되지 않는다**. 목적지가 같은
  `WCCG_SECRET_KEY` 를 써야 복호화된다(다르면 그 자격증명만 사용 불가 —
  기존 백업/복원과 동일).

## 사용자 권한

권한은 **세분 권한(permission)을 1차 단위로, 역할(role)은 그 묶음(프리셋)**으로
둔다. 즉 역할은 아래 세분 권한들을 미리 묶어 놓은 이름이고, 사용자별로
프리셋과 다른 권한을 가감하는 **세분 권한 오버라이드**를 줄 수 있다. 모든
라우트·메뉴 가드는 실효 권한(프리셋 ± 오버라이드)으로 판정하므로, 오버라이드가
한 곳에서 전 경로에 반영된다. 프리셋만 쓰면 동작은 종전 역할 체계와 같다.

기본 제공되는 빌트인 역할(권한 그룹)은 다음과 같다 (관리자가 권한 묶음을
편집하거나 커스텀 그룹을 추가할 수 있다).

| 역할 | 설명(=권한 프리셋, 신규 설치 기본값) |
|---|---|
| `admin` (관리자) | 전체 권한 (아래 8개 권한 모두) |
| `archive_manager` (아카이브 관리) | `view` + `archive` + `delete` + `use_api_keys` |
| `archiver` (아카이브) | `view` + `archive` + `use_api_keys` (삭제 없음) |
| `viewer` (보기 전용) | `view` (열람·검색·로그) — 아카이빙 버튼이 숨겨지고 API 도 403 |
| `pending` (권한없음) | 가입 승인 대기 — 로그인은 되지만 안내 페이지(`/pending`) 외 접근 불가 |
| `blocked` (차단됨) | 로그인 거부, 기존 세션도 즉시 차단 |

> 기존 설치(업그레이드)에서는 `archiver` 가 기존 `delete` 권한을 그대로
> 유지하며(신규 설치만 삭제 제외), `use_api_keys` 는 `admin`·`archiver` 에
> 자동 보강된다. 삭제 없이 아카이빙만 시키려면 `archive_manager` 가 아닌
> `archiver` 로 두거나 사용자별 오버라이드로 `delete` 를 떼면 된다.

세분 권한(`db.PERMISSIONS`)은 다음 8가지다.

| 권한 | 의미 |
|---|---|
| `view` | 아카이브 열람 + 전문 검색 + 아카이빙 로그 |
| `archive` | 아카이빙 추가·재아카이브·스케줄·크롤·재시도 |
| `delete` | 스냅샷·페이지·사이트 삭제 |
| `manage_credentials` | 사이트 로그인 자격증명 관리 + 자격증명 연결 아카이빙 |
| `manage_system` | 시스템 설정·백업·복원·네트워크 태그·시스템 로그·라이브 챌린지 |
| `manage_users` | 사용자·초대·시스템 API 키 관리 |
| `view_authenticated_all` | 다른 사용자가 로그인 캡처한 인증 스냅샷 열람 |
| `use_api_keys` | 개인 API Key(확장 토큰) 발급·사용 — 크롬 확장 캡처도 이 권한 |

사용자 관리 화면의 각 사용자 행에서 **세분 권한**(`<details>`)을 펼치면 권한별
체크박스로 가감할 수 있다 (별표 = 프리셋과 다른 항목). 예: "아카이빙은 시키되
삭제는 막기"(archive_manager − delete), "사용자 관리만 위임"(viewer + manage_users),
"보기 전용에게 확장만 허용"(viewer + use_api_keys).
**역할을 바꾸면 오버라이드는 새 역할 프리셋으로 초기화**된다. 최초 관리자
(founder)와 비활성(권한없음/차단/탈퇴) 계정은 오버라이드를 조정할 수 없다.
`manage_users` 를 가진 마지막 활성 계정에서는 그 권한을 떼거나 역할을 낮출 수
없다 (관리 잠김 방지). 개인 API Key(확장 토큰)의 발급·사용은 `use_api_keys`
권한이 게이트하고 보기/아카이브 권한은 실효 권한에서 파생되므로, 오버라이드가
토큰 권한에도 즉시 반영된다 — 권한을 회수하면 발급 화면이 막히고 기존 토큰도
다음 요청에서 거부(401)된다.

CLI(`wccg ...`)는 로컬 신뢰 실행이라 이 권한 체계의 대상이 아니며, 단발
아카이빙 큐·스케줄·크롤은 **등록(enqueue) 시점**에만 권한을 검사한다 (이미
큐에 든 작업은 실행 시점에 재검사하지 않는다).

신규 가입(`/signup`)과 SSO 자동 생성 계정의 초기 권한은 시스템 메뉴의
**가입 설정**에서 정한다 (관리자를 뺀 권한 그룹 + 권한없음 중 선택, 기본
**권한없음**). 권한없음으로 가입한 사용자는 "가입 승인 대기 중" 안내
페이지만 보게 되며, 관리자가 헤더의 **사용자** 메뉴(`/system/users`)에서
권한을 부여하면(승인) 그때부터 서비스를 이용할 수 있다.
차단하면 해당 사용자의 모든 세션이 즉시 로그아웃된다.
최초 구동 때 등록된 관리자(founder)의 권한은 누구도 변경할 수 없어,
관리자가 한 명도 없는 상태가 되지 않는다.

사용자 관리 화면에서는 권한 외에도 사용자의 **표시 이름 변경**과
**모든 세션 강제 로그아웃**이 가능하다.

## 이메일 초대

관리자는 사용자 관리 화면에서 이메일로 새 사용자를 초대할 수 있다.
초대 시 부여할 권한(관리자/아카이브/보기 전용)을 함께 지정하며, 초대받은
사람은 링크(`/invite/{token}`)에서 패스워드만 설정하면 해당 권한으로 가입된다.
초대 링크는 1회용으로 기본 7일 후 만료되고(`WCCG_INVITE_TTL_DAYS`),
같은 이메일을 다시 초대하면 새 링크로 교체된다 (이전 링크 무효화).
토큰은 세션과 동일하게 SHA-256 해시만 DB 에 저장된다.

SMTP 서버가 설정되어 있으면 초대 메일을 발송하고, 없으면 초대 링크가
화면에 표시되므로 관리자가 직접 전달하면 된다. SMTP 설정은 **시스템 메뉴의
메일(SMTP) 설정**(`/system`, 관리자 전용)에서 등록·변경하거나 아래
`WCCG_SMTP_*` 환경변수로 둘 수 있다. 둘 다 있으면 **시스템 메뉴에서 저장한
값이 우선**하고, 없는 항목만 환경변수로 폴백한다. 로그인 비밀번호는 외부
SMTP 서버에 그대로 보내야(replay) 하므로, 사이트 로그인 자격증명과 같이
`WCCG_SECRET_KEY` 로 대칭 암호화한 암호문으로만 저장된다 (평문·해시 금지 —
아키텍처 원칙 6 예외). 키가 없으면 시스템 메뉴에서 비밀번호를 저장할 수
없지만 호스트 등 다른 값은 저장되고, `WCCG_SMTP_PASSWORD` 환경변수는 그대로
쓸 수 있다. '테스트 메일 보내기'로 저장된 설정을 관리자 본인 주소로
확인할 수 있다.

## 가입 / 2FA

이후 사용자는 `/signup` 에서 가입한다 (이메일 + 패스워드 8자 이상).
로그인 화면의 회원 가입 기능은 시스템 메뉴의 **가입 설정**에서 끌 수
있다 (기본 켜짐). 꺼져 있어도 관리자의 이메일 초대로는 가입할 수 있다.
로그인 후 헤더의 **2FA** 링크에서 TOTP(Google Authenticator 등)를,
**패스키** 링크에서 패스키(WebAuthn — Touch ID, 보안 키, 휴대폰 등)를
등록할 수 있다. 둘 중 하나라도 등록되어 있으면 패스워드 로그인 시
2단계 인증(패스키 또는 OTP 코드)이 추가로 요구된다.
SSO(OIDC) 로그인은 IdP 쪽 2FA를 신뢰하므로 2단계를 건너뛴다.

패스키의 RP ID/origin 은 `WCCG_PUBLIC_URL` 에서 파생된다. 미설정 시
`localhost` 로 동작하므로 로컬에서는 `http://localhost:8765` 로 접속해야
패스키를 쓸 수 있다 (`127.0.0.1` 은 WebAuthn RP ID 로 쓸 수 없음).

세션은 서버사이드(SQLite)이며 쿠키는 HttpOnly + SameSite=Lax,
`WCCG_PUBLIC_URL` 이 https 면 Secure 가 붙는다.

## 이메일 본인 인증

시스템 메뉴의 **사용자 설정 → 이메일 본인 인증**(관리자 전용)을 켜면,
패스워드 계정이 메일로 받은 코드로 이메일 소유를 확인해야 한다 (코드 만료
시간은 분 단위로 지정, 기본 30분). **메일(SMTP) 설정이 없으면 켜더라도
동작하지 않는다** — 게이트가 그냥 통과시킨다. SSO(OIDC) 계정은 IdP 가
이메일을 검증하므로 제외된다.

- 회원 가입(`/signup`)으로 만든 계정은 가입 직후 인증 화면(`/verify-email`)
  으로 이동해 코드를 입력해야 로그인이 완료된다.
- 미인증 계정이 다시 로그인하면 (2FA 가 있으면 그 뒤에) 같은 인증 화면으로
  가며, 거기서 **코드를 다시 받을 수 있다**. 인증 전 세션은
  `pending_email_verify` 상태로 머무르고, 코드 확인 시 정식 세션으로 승격된다.
- 기능을 켜기 전부터 있던 사용자는 기존 active 세션이 유지되며(소급 차단
  없음), **개인 설정(계정 설정)의 "이메일 본인 인증" 섹션**에서 코드를 받아
  직접 인증할 수 있다.
- 사용자 관리 화면은 계정별로 이메일 인증 여부(인증됨/미인증, SSO 는 `-`)를
  표시한다.

코드는 메일로만 보내고 DB(`email_verifications`)에는 SHA-256 해시만 저장하며,
사용자당 1개라 재발송 시 교체된다. 인증 완료/계정 삭제 시 삭제된다.

## 환경변수

> 프로젝트 이름이 춘추관(ChunChuGwan)으로 바뀌면서 기존 `ARCHIVER_*` 환경변수는
> 모두 `WCCG_*` 로 이름이 변경됐다. 기존 배포의 셸/compose 환경을 함께 갱신할 것.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `WCCG_AUTH` | `on` | `off` 로 인증 비활성화 — loopback 바인딩일 때만 허용 |
| `WCCG_ADMIN_EMAIL` | (없음) | 최초 구동 시 자동 등록할 관리자 이메일 |
| `WCCG_ADMIN_PASSWORD` | (없음) | 최초 구동 시 자동 등록할 관리자 패스워드 (8자 이상) |
| `WCCG_SESSION_TTL_DAYS` | `14` | 세션 수명 (일) |
| `WCCG_PUBLIC_URL` | (없음) | 외부 노출 시 공개 URL (예: `https://archive.example.com`) — OIDC redirect_uri 조립, Secure 쿠키 판정, 패스키 RP ID/origin 에 사용 |
| `WCCG_SECRET_KEY` | (없음) | 사이트 로그인 자격증명 암호화 키(대칭). 설정해야 사이트별 로그인 자격증명을 저장·사용할 수 있다 — 임의 문자열 가능, DB·저장소엔 암호문만 남고 키는 여기에만 둔다. 바꾸면 기존 자격증명을 복호화할 수 없다 |
| `WCCG_OIDC_ISSUER` | (없음) | Authentik issuer URL (예: `https://auth.example.com/application/o/chunchugwan`) |
| `WCCG_OIDC_CLIENT_ID` | (없음) | OIDC 클라이언트 ID |
| `WCCG_OIDC_CLIENT_SECRET` | (없음) | OIDC 클라이언트 시크릿 |
| `WCCG_SMTP_HOST` | (없음) | 초대 메일 발송 SMTP 호스트 — 미설정 시 초대 링크를 화면에 표시. 시스템 메뉴 설정이 우선 |
| `WCCG_SMTP_PORT` | `587` | SMTP 포트 |
| `WCCG_SMTP_USER` | (없음) | SMTP 로그인 사용자 (없으면 인증 생략) |
| `WCCG_SMTP_PASSWORD` | (없음) | SMTP 로그인 패스워드 (시스템 메뉴 저장 시 `WCCG_SECRET_KEY` 로 암호화 보관) |
| `WCCG_SMTP_FROM` | `WCCG_SMTP_USER` | 발신자 주소 |
| `WCCG_SMTP_TLS` | `starttls` | `starttls` \| `ssl` \| `off` |
| `WCCG_INVITE_TTL_DAYS` | `7` | 초대 링크 수명 (일) |
| `WCCG_SCHEDULER` | `on` | `off` 면 serve 가 스케줄·크롤을 실행하지 않음 — `wccg worker` 나 cron 으로 대체 |
| `WCCG_CRAWL_WORKERS` | `2` | `wccg worker` 의 크롤 스레드 수 = 동시 진행 크롤(사이트) 수 (1~8) |
| `WCCG_SYSTEM_LOG_MAX_ROWS` | `20000` | 시스템 로그(`/system/logs`) 보관 한도 행 수 — 초과분은 오래된 것부터 자동 정리 |
| `WCCG_LOG_FILE` | (없음) | 설정 시 INFO 이상 로그를 그 경로에 회전 파일로도 남긴다(콘솔 출력 레벨과 무관 — 콘솔은 `serve`·`worker` 가 기본 INFO, 그 외 명령은 `-v` 시 INFO, `--quiet` 면 WARNING). 도커는 볼륨에 마운트해 호스트에서 읽는다. 프로세스(dashboard/worker/cli)별로 다른 파일을 쓸 것(회전 경합 방지). `WCCG_LOG_FILE_MAX_BYTES`(기본 10MB)·`WCCG_LOG_FILE_BACKUPS`(기본 5)로 회전 조정 |
| `WCCG_CAPTURE_ENGINE` | `playwright` | `patchright` 면 스텔스 캡처 엔진 사용 (Cloudflare 등의 `Runtime.enable` 봇 탐지 우회). 도커 이미지에 포함, 비도커는 `uv sync --extra stealth`. 미설치 시 playwright 로 자동 폴백 |
| `WCCG_CAPTURE_HEADFUL` | `off` | `on` 이면 헤드리스 대신 헤드풀로 캡처 — 서버(디스플레이 없음)에서는 Xvfb 가 필요하다 (도커 엔트리포인트가 `xvfb-run` 으로 자동 래핑). Turnstile 류는 헤드풀이 사실상 필수 |
| `WCCG_CAPTURE_CHANNEL` | (없음) | `chrome` 이면 번들 chromium 대신 시스템 real Chrome 사용 (TLS/HTTP2 지문이 진짜라 네트워크 레벨 탐지에 강함). 도커는 amd64 에만 Chrome 이 설치됨 — arm64 는 Chrome 이 없어 자동으로 번들 chromium 으로 폴백한다(동작은 하되 stealth 가 다소 약함). amd64·arm64 혼용이면 그냥 `chrome` 으로 둬도 안전 |
| `WCCG_CAPTURE_FORCE_UA` | `off` | 헤드풀일 때 기본은 고정 UA(`config.USER_AGENT`)를 해제해 real Chrome UA/Client Hints 와 맞춘다. `on` 이면 헤드풀에서도 고정 UA 를 강제 |
| `WCCG_CHALLENGE_WAIT_SECONDS` | `25` | 스텔스 캡처에서 챌린지 감지 시 자동 통과(비상호작용)를 기다리는 최대 시간(초). 풀리면 그 콘텐츠로 진행, 초과면 차단 처리 |
| `WCCG_LIVE_CHALLENGE` | `off` | `on` 이면 자동으로 못 푼 인터랙티브 챌린지를 관리자가 대시보드(`/archive/needs-human`)에서 직접 클릭/입력해 통과시키는 최후 수단 활성. 스텔스(patchright/headful)일 때만 의미. worker 가 해당 작업을 멈추고 사람을 기다린다 — **데이터센터 IP 에서는 사람이 눌러도 통과 미보장** |
| `WCCG_LIVE_CHALLENGE_TIMEOUT_SECONDS` | `300` | 라이브 세션에서 사람 입력을 기다리는 하드 타임아웃(초). 초과 시 차단 처리 |

OIDC 변수 3개가 모두 설정되면 로그인 페이지에 "SSO 로그인" 버튼이
나타난다. HTTPS 종료(HSTS 포함)는 리버스 프록시 책임이다.

> **스텔스 캡처 주의.** `WCCG_CAPTURE_*` 로 Cloudflare Turnstile 같은 봇 차단
> 페이지 통과를 *시도*할 수 있으나, 보장되지 않는다. (1) 헤드리스만으로는
> 부족해 `WCCG_CAPTURE_HEADFUL=on` + `WCCG_CAPTURE_CHANNEL=chrome`(amd64) 조합이
> 사실상 필요하고, (2) 서버의 데이터센터 IP 평판이 진짜 차단 요인이면 엔진으로는
> 못 고친다. (3) 봇 차단 우회는 대상 사이트·Cloudflare ToS 의 회색지대다.
> 통과하지 못한 차단 페이지는 감지되어 깨끗한 실패로 기록되고 아카이브를
> 오염시키지 않는다 (`/logs` 에 "봇 차단/사람 확인 챌린지 감지"). 단,
> 전면 인터스티셜(챌린지 스크립트·"Just a moment" 등)만 차단으로 보고, 정상
> 본문에 스팸 방지용으로 폼에 박힌 Turnstile 위젯(예: 그누보드 계열
> 커뮤니티의 글쓰기·검색 폼, HTTP 200)은 차단으로 보지 않고 그대로 아카이빙한다.
>
> **사람 보조(최후 수단).** `WCCG_LIVE_CHALLENGE=on` 이면 자동으로 못 푼
> 인터랙티브 챌린지를 관리자가 대시보드(`/archive/needs-human` → 라이브 화면)에서
> 스크린샷을 보고 직접 클릭/입력해 통과시킬 수 있다. worker 가 그 작업을 멈추고
> 사람을 기다린다. 봇월이 아닌 인터랙티브 페이지(로그인·동의)엔 확실히 듣지만,
> Cloudflare 는 데이터센터 IP 평판으로 사람이 눌러도 막을 수 있다.
> 단, **이 기능은 스텔스 엔진에서만 동작한다** — `WCCG_LIVE_CHALLENGE=on` 이어도
> 캡처 엔진이 `patchright` 도 `WCCG_CAPTURE_HEADFUL=on` 도 아니면(기본 headless
> playwright) 사람 단계로 넘어가지 못하고 그냥 차단 실패로 떨어지며, worker 가
> 그 이유를 시스템 로그(`/system/logs`)에 한 번 경고로 남긴다.
> **이 플래그(와 스텔스 엔진)는 캡처를 실행하는 worker 프로세스에만 필요하다** —
> 대시보드 안내는 worker 가 DB 에 남긴 대기 상태만 읽으므로 serve 프로세스의
> `WCCG_LIVE_CHALLENGE` 설정과 무관하다 (worker·serve 가 분리돼 serve 엔 플래그가
> 없어도 정상 표시). 대기 작업이 생기면 **목록 화면의 상태 배지가 "사람 확인
> 대기" 링크로 바뀌고**(서버 렌더, JS·브라우저 설정 무관), 헤더 "사람 확인 (n)"
> 메뉴와 화면 상단 전역 배너로도 안내되어, 새 아카이빙 직후 진행 화면을 보고
> 있어도 누락 없이 처리 화면으로 갈 수 있다. 사람 처리 창
> (`WCCG_LIVE_CHALLENGE_TIMEOUT_SECONDS`, 기본 5분)을 놓쳐 실패한 작업은 아카이빙
> 로그(`/logs`)에서 다시 시도하면 라이브
> 세션이 다시 열린다.

## Authentik 설정 절차

1. Authentik 관리자 → **Applications → Providers** 에서 OAuth2/OpenID Provider 생성
   - Client type: `Confidential`
   - Redirect URI: `{WCCG_PUBLIC_URL}/auth/oidc/callback`
     (로컬 테스트면 `http://127.0.0.1:8765/auth/oidc/callback`)
   - Scopes: `openid`, `email`, `profile`
2. Application 을 만들어 위 Provider 에 연결
3. Provider 상세의 **OpenID Configuration Issuer** 값을 `WCCG_OIDC_ISSUER` 에,
   Client ID/Secret 을 각각 환경변수에 설정
4. 계정 연결: 같은 이메일(IdP 에서 검증된 경우)의 기존 로컬 계정이 있으면
   자동으로 연결되고, 없으면 SSO 전용 계정이 새로 만들어진다
