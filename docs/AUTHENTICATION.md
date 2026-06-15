# 인증

> 사용자 인증·권한·초대·2FA·SSO(OIDC)와 관련 환경변수를 다룬다.
> 개요와 최초 구동(관리자 등록) 요약은 [README](../README.md#인증) 참조.

## 최초 구동 (관리자 등록)

사용자가 한 명도 없으면 최초 구동으로 판단한다.
`WCCG_ADMIN_EMAIL` / `WCCG_ADMIN_PASSWORD` 가 설정되어 있으면 그 값으로
관리자 계정이 자동 등록되고, 없으면 브라우저 첫 접속 시 `/setup` 관리자 등록
페이지로 이동한다. `/setup` 은 관리자 등록이 끝나면 페이지·API 모두 차단된다
(추가 계정은 일반 `/signup` 으로만).

## 사용자 권한

사용자는 다섯 가지 역할 중 하나를 가진다.

| 역할 | 설명 |
|---|---|
| `admin` (관리자) | 전체 기능 + 시스템 메뉴(백업/복원) + 사용자 관리 |
| `archiver` (아카이브) | 열람 + 신규/재아카이빙 트리거 |
| `viewer` (보기 전용) | 열람만 — 아카이빙 버튼이 숨겨지고 API 도 403 |
| `pending` (권한없음) | 가입 승인 대기 — 로그인은 되지만 안내 페이지(`/pending`) 외 접근 불가 |
| `blocked` (차단됨) | 로그인 거부, 기존 세션도 즉시 차단 |

신규 가입(`/signup`)과 SSO 자동 생성 계정의 초기 권한은 시스템 메뉴의
**가입 설정**에서 정한다 (권한없음/보기 전용/아카이브 중 선택, 기본
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

`WCCG_SMTP_HOST` 가 설정되어 있으면 초대 메일을 발송하고, 없으면 초대
링크가 화면에 표시되므로 관리자가 직접 전달하면 된다.

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
| `WCCG_SMTP_HOST` | (없음) | 초대 메일 발송 SMTP 호스트 — 미설정 시 초대 링크를 화면에 표시 |
| `WCCG_SMTP_PORT` | `587` | SMTP 포트 |
| `WCCG_SMTP_USER` | (없음) | SMTP 로그인 사용자 (없으면 인증 생략) |
| `WCCG_SMTP_PASSWORD` | (없음) | SMTP 로그인 패스워드 |
| `WCCG_SMTP_FROM` | `WCCG_SMTP_USER` | 발신자 주소 |
| `WCCG_SMTP_TLS` | `starttls` | `starttls` \| `ssl` \| `off` |
| `WCCG_INVITE_TTL_DAYS` | `7` | 초대 링크 수명 (일) |
| `WCCG_SCHEDULER` | `on` | `off` 면 serve 가 스케줄·크롤을 실행하지 않음 — `wccg worker` 나 cron 으로 대체 |
| `WCCG_CRAWL_WORKERS` | `2` | `wccg worker` 의 크롤 스레드 수 = 동시 진행 크롤(사이트) 수 (1~8) |
| `WCCG_SYSTEM_LOG_MAX_ROWS` | `20000` | 시스템 로그(`/system/logs`) 보관 한도 행 수 — 초과분은 오래된 것부터 자동 정리 |
| `WCCG_LOG_FILE` | (없음) | 설정 시 콘솔 로그(INFO 이상)를 그 경로에 회전 파일로도 남긴다 — 도커는 볼륨에 마운트해 호스트에서 읽는다. 프로세스(dashboard/worker/cli)별로 다른 파일을 쓸 것(회전 경합 방지). `WCCG_LOG_FILE_MAX_BYTES`(기본 10MB)·`WCCG_LOG_FILE_BACKUPS`(기본 5)로 회전 조정 |
| `WCCG_CAPTURE_ENGINE` | `playwright` | `patchright` 면 스텔스 캡처 엔진 사용 (Cloudflare 등의 `Runtime.enable` 봇 탐지 우회). 도커 이미지에 포함, 비도커는 `uv sync --extra stealth`. 미설치 시 playwright 로 자동 폴백 |
| `WCCG_CAPTURE_HEADFUL` | `off` | `on` 이면 헤드리스 대신 헤드풀로 캡처 — 서버(디스플레이 없음)에서는 Xvfb 가 필요하다 (도커 엔트리포인트가 `xvfb-run` 으로 자동 래핑). Turnstile 류는 헤드풀이 사실상 필수 |
| `WCCG_CAPTURE_CHANNEL` | (없음) | `chrome` 이면 번들 chromium 대신 시스템 real Chrome 사용 (TLS/HTTP2 지문이 진짜라 네트워크 레벨 탐지에 강함). 도커는 amd64 에만 Chrome 이 설치됨 — arm64 는 Chrome 이 없어 자동으로 번들 chromium 으로 폴백한다(동작은 하되 stealth 가 다소 약함). amd64·arm64 혼용이면 그냥 `chrome` 으로 둬도 안전 |
| `WCCG_CAPTURE_FORCE_UA` | `off` | 헤드풀일 때 기본은 고정 UA(`config.USER_AGENT`)를 해제해 real Chrome UA/Client Hints 와 맞춘다. `on` 이면 헤드풀에서도 고정 UA 를 강제 |
| `WCCG_CHALLENGE_WAIT_SECONDS` | `25` | 스텔스 캡처에서 챌린지 감지 시 자동 통과(비상호작용)를 기다리는 최대 시간(초). 풀리면 그 콘텐츠로 진행, 초과면 차단 처리 |
| `WCCG_LIVE_CHALLENGE` | `off` | `on` 이면 자동으로 못 푼 인터랙티브 챌린지를 관리자가 대시보드(`/archive/needs-human`)에서 직접 클릭/입력해 통과시키는 최후 수단 활성. 스텔스(patchright/headful)일 때만 의미. worker 가 해당 작업을 멈추고 사람을 기다린다 — **데이터센터 IP 에서는 사람이 눌러도 통과 미보장** |
| `WCCG_LIVE_CHALLENGE_TIMEOUT_SECONDS` | `300` | 라이브 세션에서 사람 입력을 기다리는 하드 타임아웃(초). 초과 시 차단 처리 |

OIDC 변수 3개가 모두 설정되면 로그인 페이지에 "Authentik으로 로그인" 버튼이
나타난다. HTTPS 종료(HSTS 포함)는 리버스 프록시 책임이다.

> **스텔스 캡처 주의.** `WCCG_CAPTURE_*` 로 Cloudflare Turnstile 같은 봇 차단
> 페이지 통과를 *시도*할 수 있으나, 보장되지 않는다. (1) 헤드리스만으로는
> 부족해 `WCCG_CAPTURE_HEADFUL=on` + `WCCG_CAPTURE_CHANNEL=chrome`(amd64) 조합이
> 사실상 필요하고, (2) 서버의 데이터센터 IP 평판이 진짜 차단 요인이면 엔진으로는
> 못 고친다. (3) 봇 차단 우회는 대상 사이트·Cloudflare ToS 의 회색지대다.
> 통과하지 못한 차단 페이지는 감지되어 깨끗한 실패로 기록되고 아카이브를
> 오염시키지 않는다 (`/logs` 에 "봇 차단/사람 확인 챌린지 감지").
>
> **사람 보조(최후 수단).** `WCCG_LIVE_CHALLENGE=on` 이면 자동으로 못 푼
> 인터랙티브 챌린지를 관리자가 대시보드(`/archive/needs-human` → 라이브 화면)에서
> 스크린샷을 보고 직접 클릭/입력해 통과시킬 수 있다. worker 가 그 작업을 멈추고
> 사람을 기다린다. 봇월이 아닌 인터랙티브 페이지(로그인·동의)엔 확실히 듣지만,
> Cloudflare 는 데이터센터 IP 평판으로 사람이 눌러도 막을 수 있다.

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
