# 춘추관 (ChunChuGwan)

개인 웹 아카이빙 시스템. URL을 받아 전체 페이지를 스냅샷(단일 HTML + 스크린샷 +
추출 텍스트)으로 저장하고, 같은 URL을 다시 아카이빙하면 히스토리가 쌓이며
스냅샷 간 텍스트/스크린샷 비교(diff)가 가능하다.

- 콘텐츠 해시 기반 중복 제거 — 본문이 그대로면 새 스냅샷 대신 "확인했음" 기록만 남음
- 타임스탬프·상대시각·광고 줄 등 노이즈는 정규화 단계에서 제거 후 비교
- 이미지/CSS/폰트를 보존하는 단일 page.html — 큰 자원은 스냅샷 간 공유
  저장소(CAS)로 중복 제거, HTML 은 gzip, 스크린샷은 WebP 로 저장 공간 절약
- 페이지가 링크한 문서 파일(PDF·워드·파워포인트·한글·키노트 등)도 스냅샷에
  함께 저장 — 스냅샷 뷰어의 "첨부 문서" 목록에서 다운로드
- 읽기 전용 대시보드 (목록/타임라인/스냅샷 뷰어/diff 뷰어/로그 + 재아카이빙·삭제 버튼)
- 아카이브 실행 로그 — 모든 실행(성공/실패)을 단계별 소요시간과 함께 DB에 기록
- 사용자 인증 — 이메일/패스워드(+선택 TOTP 2FA), Authentik OIDC SSO 지원
- 역할 기반 권한 — 관리자/아카이브/보기 전용/권한없음(가입 승인 대기)/차단,
  대시보드에서 사용자 관리 + 가입 설정(회원 가입 허용·초기 권한)

## 설치

```bash
uv sync                                  # 의존성 설치
uv run playwright install chromium       # 최초 1회
```

## 사용법

```bash
uv run wccg add <url>                # 스냅샷 생성
uv run wccg add <url> --force        # 콘텐츠 동일해도 강제 저장
uv run wccg list                     # 전체 아카이브 현황
uv run wccg history <url>            # 해당 URL 스냅샷 목록 (번호는 diff에 사용)
uv run wccg diff <url>               # 최신 2개 스냅샷 비교 (+ 스크린샷 픽셀 diff)
uv run wccg diff <url> --from 1 --to 3
uv run wccg delete <url>             # 아카이브 전체 삭제 (모든 스냅샷, 확인 후 진행)
uv run wccg delete <url> --snapshot 2  # history 번호의 스냅샷 하나만 삭제
uv run wccg serve                    # 대시보드 (http://127.0.0.1:8765)
uv run wccg serve --host 0.0.0.0     # 외부 노출 (인증 켜진 상태에서만 허용)
uv run wccg -v add <url>             # 단계별 상세 로그를 stderr 로 출력
```

삭제는 대시보드의 목록(아카이브 전체)·타임라인(스냅샷 하나) 화면에서도 할 수
있다 — 인증이 켜진 환경에서는 아카이빙 권한이 있는 사용자(admin/archiver)만
가능하다 (보기 전용·차단 계정은 불가). 스냅샷 하나를
지우면 바로 다음 스냅샷의 변경 표시(변경/동일)가 새 직전 스냅샷 기준으로 자동
보정되고, 실행 로그(`/logs`)는 이력으로 남는다.

## 사이트 전체 아카이브 (크롤)

한 페이지가 아니라 사이트(섹션) 전체를 링크를 따라가며 아카이빙한다.
범위는 **같은 호스트 + 시작 URL 의 경로 프리픽스 이하** — 예를 들어
`example.com/docs/intro` 로 시작하면 `example.com/docs/` 이하만 수집한다.

```bash
uv run wccg crawl add <url>                    # 등록 후 완료까지 동기 실행
uv run wccg crawl add <url> --max-pages 50 --max-depth 3 --delay 10
uv run wccg crawl add <url> --no-wait          # 등록만 (serve 의 크롤러가 실행)
uv run wccg crawl list                         # 크롤 목록
uv run wccg crawl run                          # 기한이 된 페이지 처리 (cron 용)
```

- 페이지 간 최소 간격(`--delay`, 기본 5초)을 두어 대상 서버에 부담을 주지
  않는다. 실패한 페이지는 백오프(5분 → 15분) 후 자동 재시도하고, 3회 초과
  시 실패로 남는다 — 대시보드 진행 화면에서 일괄 재시도할 수 있다.
- 큐는 DB 에 있어 서버를 재시작해도 이어서 진행된다. `wccg serve` 가 떠
  있으면 크롤러 스레드가 큐를 자동 소비한다 (`WCCG_SCHEDULER=off` 면 cron
  에서 `wccg crawl run`).
- 페이지 저장은 일반 아카이빙과 같은 파이프라인 — 내용이 직전 스냅샷과
  같으면 새 스냅샷 없이 기존 스냅샷이 크롤 세트에 참조된다.
- 대시보드에서는 새 아카이빙 화면(`/archive/new`)의 **사이트 전체 아카이브**
  체크박스로 시작하고, **사이트** 메뉴(`/crawls`)에서 진행 현황·취소·재시도를
  관리한다.
- 크롤로 저장된 page.html 의 링크는 아카이브 리졸버(`/crawl/{id}/goto`)로
  재작성된다 — 스냅샷 뷰어에서 링크를 클릭하면 같은 크롤에서 아카이빙된
  페이지로 이동하고(없으면 그 URL 의 최신 스냅샷, 그것도 없으면 원본 링크
  안내 화면), 라이브 사이트로 조용히 새지 않는다.
- robots.txt 는 따르지 않는다 — 개인 아카이빙 용도다.

## 주기적 자동 재아카이빙

같은 페이지를 일정 시간마다 다시 아카이빙한다. 반복 주기는 최소 1시간(`1h`)
부터 최대 1개월(`1mo`, 30일)까지 — `m`(분)·`h`(시간)·`d`(일)·`w`(주)·`mo`(개월)
단위를 쓴다.

```bash
uv run wccg schedule add <url> --every 12h   # 등록/변경 (예: 90m, 12h, 3d, 1w, 1mo)
uv run wccg schedule add <url> --every 1d --at 09:00  # 1일 단위 주기는 실행 시각 지정 가능
uv run wccg schedule list                    # 등록된 스케줄 목록
uv run wccg schedule next <url> 2026-06-12T09:00  # 다음 실행 시각 변경 (로컬 시간)
uv run wccg schedule remove <url>            # 해제
uv run wccg schedule run                     # 기한이 된 스케줄 1회 실행 (cron 용)
```

- 등록 대상은 이미 아카이빙된 URL 이어야 한다 (`wccg add` 먼저).
- `--at HH:MM` 은 1일 단위 주기(`1d`, `3d`, `1w` 등)에서만 쓸 수 있고,
  서버 로컬 시간 기준으로 그 시각에 맞춰 실행된다.
- `wccg serve` 가 떠 있으면 대시보드 프로세스가 1분마다 기한을 확인해 자동
  실행한다 (`WCCG_SCHEDULER=off` 로 끌 수 있고, 그 경우 cron 에서
  `wccg schedule run` 을 돌리면 된다).
- 실행 결과는 실행 로그(`/logs`, source=`schedule`)에 남고, 콘텐츠가 동일하면
  기존 규칙대로 스냅샷 없이 확인 기록만 쌓인다.
- 대시보드 타임라인 화면에서도 페이지별로 주기를 설정/해제할 수 있고,
  새 아카이빙 화면(`/archive/new`)에서는 URL 등록과 동시에 주기를 지정할 수 있다.
  프리셋 외에 직접 입력(분/시간/일)과 1일 단위 주기의 실행 시각도 지원한다.
- 헤더의 **스케줄** 메뉴(`/schedules`)에서 등록된 자동 재아카이빙 전체를
  한눈에 보고, 주기 변경·다음 실행 시각 변경·해제도 바로 할 수 있다
  (admin/archiver 만 —
  viewer 는 목록만 보인다).

## 백업/복원

```bash
uv run wccg backup [dest]                  # 전체 백업 tar.gz (DB·인증 데이터·스냅샷 파일·rules.json)
uv run wccg restore <file> [--yes]         # 전체 복원 — 현재 데이터를 백업 시점 상태로 교체
uv run wccg export [dest]                  # 아카이브 데이터만 내보내기 (페이지·스냅샷·확인 기록 + 파일)
uv run wccg import <file>                  # 가져오기 (기본 merge — 기존 유지, 중복 스냅샷 스킵)
uv run wccg import <file> --mode overwrite # 기존 아카이브 데이터를 지우고 가져오기 (--yes 로 확인 생략)
```

- `backup`/`restore` 는 인증 데이터(사용자·세션·패스키)까지 포함한 전체 복구용.
  복원은 아카이브 루트 전체를 교체하므로 확인 프롬프트를 거친다.
- `export`/`import` 는 아카이브 데이터(pages·snapshots·checks + 스냅샷 파일)만
  다룬다 — 인증 테이블과 실행 로그(archive_logs)는 건드리지 않으므로 다른
  인스턴스로 데이터를 옮기거나 합칠 때 쓴다. `merge` 는 같은 페이지의 같은
  스냅샷 디렉토리를 스킵해 여러 번 실행해도 안전하다(멱등).
- `dest` 를 생략하면 현재 디렉토리에 `chunchugwan-{backup|export}-{시각}.tar.gz`
  로 생성된다.
- 대시보드의 **시스템** 메뉴(`/system`)에서도 같은 기능을 쓸 수 있다 — 백업·
  내보내기는 파일 다운로드, 복원·가져오기는 파일 업로드. 백업에 인증 데이터가
  포함되므로 인증이 켜진 환경에서는 관리자만 접근할 수 있다.

## 도커로 실행

로컬에 Python/uv 를 설치하지 않고 Docker / Docker Compose 로 실행할 수 있다.

### Docker Compose (권장)

리포지토리에는 예제 파일(`compose.example.yaml`)만 들어 있다. 복사해서
로컬 전용 `compose.yaml` 을 만들고, 개인 설정은 거기서만 수정한다 —
`compose.yaml`(과 `.env`, `compose.override.yaml`)은 gitignore 대상이라
시크릿이 커밋될 일이 없다.

예제는 GitHub Actions 가 main 푸시마다 빌드해 GHCR 에 게시하는 이미지
(`ghcr.io/tylorstyle/chunchugwan:latest`, amd64/arm64)를 사용한다.
로컬 소스로 직접 빌드하려면 복사한 `compose.yaml` 에서 `image:` 줄을
`build: .` 로 바꾸면 된다.

```bash
cp compose.example.yaml compose.yaml   # 예제 복사 (최초 1회)
docker compose up -d dashboard         # 대시보드 (http://127.0.0.1:8765)
docker compose run --rm cli add <url>  # 스냅샷 생성
docker compose run --rm cli list       # 아카이브 현황
docker compose run --rm cli history <url>  # 스냅샷 목록
docker compose run --rm cli diff <url>     # 스냅샷 비교
docker compose down                    # 대시보드 중지
```

설정은 복사한 `compose.yaml` 의 `environment:` 블록에서 한다. 예제 파일에
자주 쓰는 항목(관리자 자동 등록, 공개 URL, OIDC, SMTP)이 주석으로 들어 있으니
필요한 것만 주석을 해제하면 된다 — 전체 목록은 아래 [환경변수](#환경변수) 절 참조.

```yaml
    environment:
      WCCG_HOST: "0.0.0.0"             # 그대로 둘 것 (컨테이너 내부 바인딩)
      WCCG_ADMIN_EMAIL: "admin@example.com"   # 최초 구동 시 관리자 자동 등록
      WCCG_ADMIN_PASSWORD: "********"         # 8자 이상, 최초 구동 후 제거 권장
```

### Docker 단독 (compose 없이)

```bash
docker build -t chunchugwan .

# 대시보드
docker run -d --name wccg --init --shm-size 1g \
  -e WCCG_HOST=0.0.0.0 \
  -p 127.0.0.1:8765:8765 \
  -v "$PWD/archive:/data/archive" \
  --restart unless-stopped \
  chunchugwan serve

# CLI (대시보드와 같은 ./archive 를 공유)
docker run --rm --init --shm-size 1g \
  -v "$PWD/archive:/data/archive" \
  chunchugwan add <url>
```

환경변수는 `-e WCCG_...=값` 을 추가해 설정한다. 시크릿(OIDC·SMTP 등)이
명령 히스토리에 남는 게 싫으면 `--env-file` 로 gitignore 된 `.env` 파일을
넘기면 된다.

### 공통 사항

- 아카이브 데이터는 호스트의 `./archive` 디렉토리에 바인드 마운트로 저장된다
  (컨테이너를 지워도 유지되며, 로컬 `uv run wccg` 와 같은 데이터를 공유).
- 포트는 호스트의 **127.0.0.1 에만** 바인딩되어 localhost 전용 원칙이 유지된다.
  외부에 노출하려면 포트 매핑을 여는 대신 리버스 프록시(HTTPS 종료)를 앞단에
  두고 `WCCG_PUBLIC_URL` 을 설정할 것.
- 컨테이너 대시보드는 내부적으로 0.0.0.0 바인딩이라 **인증이 항상 켜진다**
  (`WCCG_AUTH=off` 는 loopback 바인딩 전용). 첫 접속 시 `/setup` 에서 관리자를
  등록하거나, `WCCG_ADMIN_EMAIL`/`WCCG_ADMIN_PASSWORD` 로 자동 등록한다.
- 컨테이너는 비루트(uid 1000)로 실행되어 chromium 샌드박스가 활성 상태로 동작한다.
  호스트의 `./archive` 소유자가 달라도(예: docker 가 root 로 자동 생성) 기동 시
  엔트리포인트가 소유자를 uid 1000 으로 보정한 뒤 비루트로 전환하므로 별도
  조치가 필요 없다. 호스트에서 같은 디렉토리를 `uv run wccg` 로 함께 쓰는
  경우에만 파일이 uid 1000 소유가 된다는 점을 참고.
- 최초 빌드는 chromium 다운로드를 포함해 수 분 걸린다 (이미지 약 1.5GB).

## 저장 구조

```
archive/
├── index.db                # SQLite 인덱스 (pages / snapshots / checks)
├── rules.json              # (선택) 도메인별 정규화 룰
├── cache/                  # 파생 산출물 (픽셀 diff 하이라이트 등, 재생성 가능)
├── resources/              # 스냅샷 간 공유 자원 CAS — 이미지·폰트·CSS,
│                           #   sha256 콘텐츠 주소라 같은 자원은 한 번만 저장
└── sites/{domain}/{slug}-{url_hash8}/{timestamp}/
    ├── page.html.gz        # 단일 HTML (gzip). 큰 자원은 /resource/ 참조,
    │                       #   작은 자원(<4KB)은 data URI 인라인 유지
    ├── raw.html.gz         # 렌더링 후 DOM 소스 (gzip)
    ├── content.md          # 추출+정규화 텍스트 (해시/diff 기준)
    ├── screenshot.webp     # 전체 페이지 (WebP 변환 실패 시 screenshot.png 유지)
    ├── files/              # 페이지가 링크한 문서 파일 (PDF·워드·한글 등 —
    │                       #   문서 링크가 없으면 생기지 않음)
    └── meta.json           # url, final_url, 시각, 해시, http 정보,
                            #   documents 목록 (files/ 의 출처 URL·해시)
```

문서 파일은 페이지당 최대 20개·개당 50MB 까지 저장하며, 링크 확장자가
문서 화이트리스트(pdf, doc(x), ppt(x), xls(x), hwp(x), odt/odp/ods, rtf,
pages, key, numbers, epub)에 있을 때만 받는다. 응답이 HTML(로그인·오류
페이지)이면 건너뛰고, 다운로드 실패는 페이지 아카이빙을 막지 않는다.

스냅샷 디렉토리는 불변이다. 변경 = 새 스냅샷. 아카이브 위치는 환경변수
`WCCG_ROOT`로 변경할 수 있다 (기본 `./archive`).

압축 저장 형태 도입 이전에 만든 스냅샷(page.html / raw.html / screenshot.png)도
그대로 읽힌다. 기존 스냅샷을 압축 형태로 변환해 저장 공간을 줄이려면:

```bash
uv run wccg compact          # 1회성 마이그레이션 (내용 보존 변환, --yes 로 확인 생략)
```

대시보드 **시스템** 메뉴(`/system`)의 "저장 공간 압축" 버튼으로도 같은 변환을
실행할 수 있다 (인증이 켜진 환경에서는 관리자 전용).

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

`wccg serve` 후 http://127.0.0.1:8765 접속. 기본은 loopback 바인딩이며,
아카이빙된 HTML은 항상 `<iframe sandbox>` 안에서만 렌더링되어 원본 페이지의
스크립트가 대시보드 컨텍스트에서 실행되지 않는다. 재아카이빙 버튼은
백그라운드로 코어 파이프라인을 호출한다.

`/logs` 페이지에서 아카이브 실행 로그를 볼 수 있다. 모든 실행은 성공/실패와
관계없이 단계별(normalize → capture → extract → hash → store) 소요시간과
함께 기록되며, 도메인·페이지·스냅샷·상태(신규/변경/동일/실패)로 필터할 수
있다. 타임라인의 "로그" 링크는 해당 페이지의 로그를, 스냅샷 뷰어의 "로그"
링크는 해당 버전을 만든 실행 기록을 보여준다.

### 다국어 (i18n)

대시보드 UI 는 한국어·영어를 지원한다. 헤더의 언어 선택으로 바꾸면
`wccg_lang` 쿠키에 저장되고, 선택이 없으면 브라우저의 `Accept-Language`
헤더를 따른다 (기본 한국어). 번역 카탈로그는 `chunchugwan/web/i18n.py` —
한국어 원문이 키이고, 새 언어는 "원문 → 번역" dict 하나를 추가하면 된다.

## 외부 API (API 키)

외부 소프트웨어가 아카이빙을 트리거하거나 아카이빙된 데이터를 가져갈 수
있도록 `/api/v1` REST API 를 제공한다. 인증은 **API 키** —
`Authorization: Bearer <키>` 또는 `X-API-Key: <키>` 헤더로 보낸다.
(인증이 꺼진 loopback 환경에서는 키 없이 접근 가능)

키는 관리자가 헤더의 **API 키** 메뉴(`/system/api-keys`)에서 발급하며,
모든 관리자가 공동으로 보고 폐기할 수 있다. 키마다 다음을 설정한다.

- **권한**: 보기(데이터 조회) / 아카이브(아카이빙 트리거) — 복수 선택 가능
- **만료**: 영구 · 1일 · 1개월 · 1년 · 사용자 지정(일 단위, 최대 3650일)

키 원문은 발급 직후 한 번만 표시되고 DB 에는 SHA-256 해시만 저장된다.
폐기하면 즉시 무효화된다.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/api/v1/pages` | 보기 | 페이지 목록 (`?url=` 로 단일 조회) |
| GET | `/api/v1/pages/{id}` | 보기 | 페이지 상세 + 스냅샷 히스토리 |
| GET | `/api/v1/snapshots/{id}` | 보기 | 스냅샷 메타데이터 |
| GET | `/api/v1/snapshots/{id}/file/{name}` | 보기 | 파일 다운로드 (`page.html` \| `screenshot` \| `content.md`) |
| POST | `/api/v1/archive` | 아카이브 | 아카이빙 트리거 — 본문 `{"url": "...", "force": false}` |

```bash
curl -H "Authorization: Bearer wccg_..." http://127.0.0.1:8765/api/v1/pages
curl -X POST -H "X-API-Key: wccg_..." -H "Content-Type: application/json" \
     -d '{"url": "https://example.com"}' http://127.0.0.1:8765/api/v1/archive
```

아카이빙은 백그라운드로 실행되며(응답 202), 같은 URL 이 이미 진행 중이면
`queued: false` 로 응답한다. API 로 실행된 아카이빙은 로그에 `api` 출처로
기록된다.

## 인증

### 최초 구동 (관리자 등록)

사용자가 한 명도 없으면 최초 구동으로 판단한다.
`WCCG_ADMIN_EMAIL` / `WCCG_ADMIN_PASSWORD` 가 설정되어 있으면 그 값으로
관리자 계정이 자동 등록되고, 없으면 브라우저 첫 접속 시 `/setup` 관리자 등록
페이지로 이동한다. `/setup` 은 관리자 등록이 끝나면 페이지·API 모두 차단된다
(추가 계정은 일반 `/signup` 으로만).

### 사용자 권한

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

### 이메일 초대

관리자는 사용자 관리 화면에서 이메일로 새 사용자를 초대할 수 있다.
초대 시 부여할 권한(관리자/아카이브/보기 전용)을 함께 지정하며, 초대받은
사람은 링크(`/invite/{token}`)에서 패스워드만 설정하면 해당 권한으로 가입된다.
초대 링크는 1회용으로 기본 7일 후 만료되고(`WCCG_INVITE_TTL_DAYS`),
같은 이메일을 다시 초대하면 새 링크로 교체된다 (이전 링크 무효화).
토큰은 세션과 동일하게 SHA-256 해시만 DB 에 저장된다.

`WCCG_SMTP_HOST` 가 설정되어 있으면 초대 메일을 발송하고, 없으면 초대
링크가 화면에 표시되므로 관리자가 직접 전달하면 된다.

### 가입 / 2FA

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

### 환경변수

> 프로젝트 이름이 춘추관(ChunChuGwan)으로 바뀌면서 기존 `ARCHIVER_*` 환경변수는
> 모두 `WCCG_*` 로 이름이 변경됐다. 기존 배포의 셸/compose 환경을 함께 갱신할 것.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `WCCG_AUTH` | `on` | `off` 로 인증 비활성화 — loopback 바인딩일 때만 허용 |
| `WCCG_ADMIN_EMAIL` | (없음) | 최초 구동 시 자동 등록할 관리자 이메일 |
| `WCCG_ADMIN_PASSWORD` | (없음) | 최초 구동 시 자동 등록할 관리자 패스워드 (8자 이상) |
| `WCCG_SESSION_TTL_DAYS` | `14` | 세션 수명 (일) |
| `WCCG_PUBLIC_URL` | (없음) | 외부 노출 시 공개 URL (예: `https://archive.example.com`) — OIDC redirect_uri 조립, Secure 쿠키 판정, 패스키 RP ID/origin 에 사용 |
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

OIDC 변수 3개가 모두 설정되면 로그인 페이지에 "Authentik으로 로그인" 버튼이
나타난다. HTTPS 종료(HSTS 포함)는 리버스 프록시 책임이다.

### Authentik 설정 절차

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

## 개발

```bash
uv run pytest                            # 테스트 (네트워크 불필요, ~10초)
```

### PyCharm

프로젝트를 열면 `.idea/runConfigurations/`에 포함된 실행/디버그 구성이
우측 상단 드롭다운에 바로 나타난다.

| 구성 | 용도 |
|---|---|
| `wccg serve` | 대시보드 실행 — `web/app.py` 라우트 디버깅 |
| `wccg add` | 아카이빙 1회 실행 — 캡처/파이프라인 디버깅 (URL은 구성 편집에서 변경) |
| `wccg list` / `wccg diff` | CLI 조회 명령 |
| `pytest: all` | `tests/` 전체를 테스트 러너로 실행 (개별 테스트 디버그/재실행 가능) |

- **인터프리터**: `uv sync`로 만든 `.venv`를 프로젝트 인터프리터로 지정한다
  (Settings → Project → Python Interpreter → Add → Existing → `.venv/bin/python`).
  uv 연동이 있는 최신 PyCharm은 자동 인식한다.
- **디버깅**: CLI 구성은 `chunchugwan.cli` 모듈 실행(`python -m`) 방식이라
  `pipeline.py`, `capture.py` 등 패키지 어디든 브레이크포인트가 동작한다.
  `serve`는 reload 없는 단일 프로세스로 떠서 라우트 핸들러 디버깅이 바로 되고,
  재아카이빙 버튼이 트리거하는 `pipeline.archive_url`은 BackgroundTasks 특성상
  응답이 끝난 뒤 브레이크포인트가 잡힌다.
- **작업 디렉토리**: 모든 구성이 프로젝트 루트 기준이라 터미널 실행과 동일한
  `./archive`를 사용한다.
- Jinja2 템플릿 디렉토리(`chunchugwan/web/templates`)가 프로젝트 설정에 등록되어
  있어 템플릿 자동완성/네비게이션이 동작한다.

아키텍처 원칙·DB 스키마·코딩 컨벤션은 [CLAUDE.md](CLAUDE.md) 참조.
모듈 구성:

| 모듈 | 역할 |
|---|---|
| `chunchugwan/storage.py` | URL 정규화, slug, 스냅샷 파일시스템 레이아웃 |
| `chunchugwan/db.py` | SQLite 인덱스 (모든 DB 접근의 단일 창구) |
| `chunchugwan/capture.py` | Playwright 렌더링, 자원 인라인, 셀렉터 제거 |
| `chunchugwan/extract.py` | 본문 추출(DOM 가시 텍스트 덤프) + 정규화 |
| `chunchugwan/differ.py` | 텍스트 diff + 스크린샷 픽셀 diff |
| `chunchugwan/pipeline.py` | 아카이빙 흐름 (CLI/대시보드 공용 쓰기 진입점) |
| `chunchugwan/auth.py` | 인증 코어 — argon2 해싱, 세션 토큰, TOTP |
| `chunchugwan/oidc.py` | Authentik OIDC 클라이언트 (httpx + PyJWT) |
| `chunchugwan/cli.py` | click CLI |
| `chunchugwan/web/` | FastAPI 대시보드 (인증 라우트 `auth_routes.py` 포함) |
