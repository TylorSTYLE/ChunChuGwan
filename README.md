# Web Archiver

개인 웹 아카이빙 시스템. URL을 받아 전체 페이지를 스냅샷(단일 HTML + 스크린샷 +
추출 텍스트)으로 저장하고, 같은 URL을 다시 아카이빙하면 히스토리가 쌓이며
스냅샷 간 텍스트/스크린샷 비교(diff)가 가능하다.

- 콘텐츠 해시 기반 중복 제거 — 본문이 그대로면 새 스냅샷 대신 "확인했음" 기록만 남음
- 타임스탬프·상대시각·광고 줄 등 노이즈는 정규화 단계에서 제거 후 비교
- 이미지/CSS/폰트를 base64로 인라인한 단일 page.html (오프라인 열람 가능)
- 읽기 전용 대시보드 (목록/타임라인/스냅샷 뷰어/diff 뷰어 + 재아카이빙 버튼)
- 사용자 인증 — 이메일/패스워드(+선택 TOTP 2FA), Authentik OIDC SSO 지원

## 설치

```bash
uv sync                                  # 의존성 설치
uv run playwright install chromium       # 최초 1회
```

## 사용법

```bash
uv run archiver add <url>                # 스냅샷 생성
uv run archiver add <url> --force        # 콘텐츠 동일해도 강제 저장
uv run archiver list                     # 전체 아카이브 현황
uv run archiver history <url>            # 해당 URL 스냅샷 목록 (번호는 diff에 사용)
uv run archiver diff <url>               # 최신 2개 스냅샷 비교 (+ 스크린샷 픽셀 diff)
uv run archiver diff <url> --from 1 --to 3
uv run archiver serve                    # 대시보드 (http://127.0.0.1:8765)
uv run archiver serve --host 0.0.0.0     # 외부 노출 (인증 켜진 상태에서만 허용)
```

## 도커로 실행

로컬에 Python/uv 를 설치하지 않고 Docker Compose 로 실행할 수 있다.

```bash
docker compose up -d dashboard                  # 대시보드 (http://127.0.0.1:8765)
docker compose run --rm cli add <url>           # 스냅샷 생성
docker compose run --rm cli list                # 아카이브 현황
docker compose run --rm cli history <url>       # 스냅샷 목록
docker compose run --rm cli diff <url>          # 스냅샷 비교
docker compose down                             # 대시보드 중지
```

- 아카이브 데이터는 호스트의 `./archive` 디렉토리에 바인드 마운트로 저장된다
  (컨테이너를 지워도 유지되며, 로컬 `uv run archiver` 와 같은 데이터를 공유).
- 포트는 호스트의 **127.0.0.1 에만** 바인딩되어 localhost 전용 원칙이 유지된다.
- 컨테이너 대시보드는 내부적으로 0.0.0.0 바인딩이라 **인증이 항상 켜진다**
  (`ARCHIVER_AUTH=off` 는 loopback 바인딩 전용). 첫 접속 시 `/signup` 에서 가입.
- 컨테이너는 비루트(uid 1000)로 실행되어 chromium 샌드박스가 활성 상태로 동작한다.
  Linux 호스트에서 `./archive` 권한 오류가 나면 디렉토리 소유자를 uid 1000 에 맞출 것
  (macOS Docker Desktop 은 해당 없음).
- 최초 빌드는 chromium 다운로드를 포함해 수 분 걸린다 (이미지 약 1.5GB).

## 저장 구조

```
archive/
├── index.db                # SQLite 인덱스 (pages / snapshots / checks)
├── rules.json              # (선택) 도메인별 정규화 룰
├── cache/                  # 파생 산출물 (픽셀 diff 하이라이트 등, 재생성 가능)
└── sites/{domain}/{slug}-{url_hash8}/{timestamp}/
    ├── page.html           # 자원 인라인된 단일 HTML
    ├── raw.html            # 렌더링 후 DOM 소스
    ├── content.md          # 추출+정규화 텍스트 (해시/diff 기준)
    ├── screenshot.png      # 전체 페이지
    └── meta.json           # url, final_url, 시각, 해시, http 정보
```

스냅샷 디렉토리는 불변이다. 변경 = 새 스냅샷. 아카이브 위치는 환경변수
`ARCHIVER_ROOT`로 변경할 수 있다 (기본 `./archive`).

## 도메인별 정규화 룰 (선택)

`archive/rules.json`에 도메인별로 비교 노이즈를 제거할 룰을 둘 수 있다.
저장 산출물(raw.html, page.html)에는 손대지 않고 해시/diff 기준 텍스트에만 적용된다.

```json
{
  "example.com": {
    "remove_selectors": [".ads", "#recommend-widget"],
    "remove_line_patterns": ["^관련 기사", "^구독하기$"]
  }
}
```

- `remove_selectors` — 본문 추출 전에 DOM에서 제거할 CSS 셀렉터
- `remove_line_patterns` — 정규화 텍스트에서 버릴 줄의 정규식 (`www.` 접두사 없는 키로도 조회됨)

## 대시보드

`archiver serve` 후 http://127.0.0.1:8765 접속. 기본은 loopback 바인딩이며,
아카이빙된 HTML은 항상 `<iframe sandbox>` 안에서만 렌더링되어 원본 페이지의
스크립트가 대시보드 컨텍스트에서 실행되지 않는다. 재아카이빙 버튼은
백그라운드로 코어 파이프라인을 호출한다.

## 인증

### 최초 구동 (관리자 등록)

사용자가 한 명도 없으면 최초 구동으로 판단한다.
`ARCHIVER_ADMIN_EMAIL` / `ARCHIVER_ADMIN_PASSWORD` 가 설정되어 있으면 그 값으로
관리자 계정이 자동 등록되고, 없으면 브라우저 첫 접속 시 `/setup` 관리자 등록
페이지로 이동한다. `/setup` 은 관리자 등록이 끝나면 페이지·API 모두 차단된다
(추가 계정은 일반 `/signup` 으로만).

### 가입 / 2FA

이후 사용자는 `/signup` 에서 가입한다 (이메일 + 패스워드 8자 이상).
로그인 후 헤더의 **2FA** 링크에서 TOTP(Google Authenticator 등)를 등록할 수
있고, 등록하면 패스워드 로그인 시 OTP 코드가 추가로 요구된다.
SSO(OIDC) 로그인은 IdP 쪽 2FA를 신뢰하므로 OTP 단계를 건너뛴다.

세션은 서버사이드(SQLite)이며 쿠키는 HttpOnly + SameSite=Lax,
`ARCHIVER_PUBLIC_URL` 이 https 면 Secure 가 붙는다.

### 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `ARCHIVER_AUTH` | `on` | `off` 로 인증 비활성화 — loopback 바인딩일 때만 허용 |
| `ARCHIVER_ADMIN_EMAIL` | (없음) | 최초 구동 시 자동 등록할 관리자 이메일 |
| `ARCHIVER_ADMIN_PASSWORD` | (없음) | 최초 구동 시 자동 등록할 관리자 패스워드 (8자 이상) |
| `ARCHIVER_SESSION_TTL_DAYS` | `14` | 세션 수명 (일) |
| `ARCHIVER_PUBLIC_URL` | (없음) | 외부 노출 시 공개 URL (예: `https://archive.example.com`) — OIDC redirect_uri 조립과 Secure 쿠키 판정에 사용 |
| `ARCHIVER_OIDC_ISSUER` | (없음) | Authentik issuer URL (예: `https://auth.example.com/application/o/archiver`) |
| `ARCHIVER_OIDC_CLIENT_ID` | (없음) | OIDC 클라이언트 ID |
| `ARCHIVER_OIDC_CLIENT_SECRET` | (없음) | OIDC 클라이언트 시크릿 |

OIDC 변수 3개가 모두 설정되면 로그인 페이지에 "Authentik으로 로그인" 버튼이
나타난다. HTTPS 종료(HSTS 포함)는 리버스 프록시 책임이다.

### Authentik 설정 절차

1. Authentik 관리자 → **Applications → Providers** 에서 OAuth2/OpenID Provider 생성
   - Client type: `Confidential`
   - Redirect URI: `{ARCHIVER_PUBLIC_URL}/auth/oidc/callback`
     (로컬 테스트면 `http://127.0.0.1:8765/auth/oidc/callback`)
   - Scopes: `openid`, `email`, `profile`
2. Application 을 만들어 위 Provider 에 연결
3. Provider 상세의 **OpenID Configuration Issuer** 값을 `ARCHIVER_OIDC_ISSUER` 에,
   Client ID/Secret 을 각각 환경변수에 설정
4. 계정 연결: 같은 이메일(IdP 에서 검증된 경우)의 기존 로컬 계정이 있으면
   자동으로 연결되고, 없으면 SSO 전용 계정이 새로 만들어진다

## 개발

```bash
uv run pytest                            # 테스트 (네트워크 불필요, ~10초)
```

### PyCharm

프로젝트를 열면 `.idea/runConfigurations/`에 포함된 실행/디버그 구성이
우측 상단 드롭다운에 바로 나타난다.

| 구성 | 용도 |
|---|---|
| `archiver serve` | 대시보드 실행 — `web/app.py` 라우트 디버깅 |
| `archiver add` | 아카이빙 1회 실행 — 캡처/파이프라인 디버깅 (URL은 구성 편집에서 변경) |
| `archiver list` / `archiver diff` | CLI 조회 명령 |
| `pytest: all` | `tests/` 전체를 테스트 러너로 실행 (개별 테스트 디버그/재실행 가능) |

- **인터프리터**: `uv sync`로 만든 `.venv`를 프로젝트 인터프리터로 지정한다
  (Settings → Project → Python Interpreter → Add → Existing → `.venv/bin/python`).
  uv 연동이 있는 최신 PyCharm은 자동 인식한다.
- **디버깅**: CLI 구성은 `archiver.cli` 모듈 실행(`python -m`) 방식이라
  `pipeline.py`, `capture.py` 등 패키지 어디든 브레이크포인트가 동작한다.
  `serve`는 reload 없는 단일 프로세스로 떠서 라우트 핸들러 디버깅이 바로 되고,
  재아카이빙 버튼이 트리거하는 `pipeline.archive_url`은 BackgroundTasks 특성상
  응답이 끝난 뒤 브레이크포인트가 잡힌다.
- **작업 디렉토리**: 모든 구성이 프로젝트 루트 기준이라 터미널 실행과 동일한
  `./archive`를 사용한다.
- Jinja2 템플릿 디렉토리(`archiver/web/templates`)가 프로젝트 설정에 등록되어
  있어 템플릿 자동완성/네비게이션이 동작한다.

아키텍처 원칙·DB 스키마·코딩 컨벤션은 [CLAUDE.md](CLAUDE.md) 참조.
모듈 구성:

| 모듈 | 역할 |
|---|---|
| `archiver/storage.py` | URL 정규화, slug, 스냅샷 파일시스템 레이아웃 |
| `archiver/db.py` | SQLite 인덱스 (모든 DB 접근의 단일 창구) |
| `archiver/capture.py` | Playwright 렌더링, 자원 인라인, 셀렉터 제거 |
| `archiver/extract.py` | 본문 추출(trafilatura) + 정규화 |
| `archiver/differ.py` | 텍스트 diff + 스크린샷 픽셀 diff |
| `archiver/pipeline.py` | 아카이빙 흐름 (CLI/대시보드 공용 쓰기 진입점) |
| `archiver/auth.py` | 인증 코어 — argon2 해싱, 세션 토큰, TOTP |
| `archiver/oidc.py` | Authentik OIDC 클라이언트 (httpx + PyJWT) |
| `archiver/cli.py` | click CLI |
| `archiver/web/` | FastAPI 대시보드 (인증 라우트 `auth_routes.py` 포함) |
