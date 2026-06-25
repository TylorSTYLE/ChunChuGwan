# 개발

> 테스트 실행, PyCharm 구성, 모듈 구성을 다룬다. 아키텍처 원칙·DB
> 스키마·코딩 컨벤션은 [CLAUDE.md](../CLAUDE.md) 참조.

```bash
uv run pytest                            # 테스트 (네트워크 불필요, ~10초)
```

## 설정 · 시크릿 (`.env`)

`WCCG_*` 환경변수(관리자 계정·`WCCG_SECRET_KEY`·OIDC·SMTP·스텔스 캡처 등)는 셸에
직접 export 하거나 프로젝트 루트의 `.env`(`KEY=값`)에 둘 수 있다 — `uv run wccg` 가
시작 시 `config.py` 의 `load_dotenv` 로 현재 디렉토리(또는 상위)의 `.env` 를 자동
로드한다(실제 환경변수가 우선). `cp .env.example .env` 후 필요한 값만 채운다. `.env`
는 `.gitignore` 로 제외된다. 전체 변수 목록은
[AUTHENTICATION.md](AUTHENTICATION.md#환경변수).

## PyCharm

프로젝트를 열면 `.idea/runConfigurations/`에 포함된 실행/디버그 구성이
우측 상단 드롭다운에 바로 나타난다.

| 구성 | 용도 |
|---|---|
| `wccg serve` | 대시보드 실행 — `web/app.py` 라우트 디버깅 (Before launch 로 프론트엔드 자동 빌드) |
| `wccg worker` | 아카이빙 워커 — `pipeline.py`·`archive_worker.py` 디버깅 |
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
- 대시보드는 SvelteKit SPA(`frontend/`)다 — `wccg serve` 구성은 Before launch 로
  `npm run build`를 자동 실행하므로 별도 빌드 없이 바로 시작할 수 있다(빌드가 없으면 503).
  SPA 개발 중에는 `npm --prefix frontend run dev`(Vite, HMR)로 띄우고 API 는
  `wccg serve` 로 따로 돌린다.
- 스타일링은 **Tailwind CSS v4 + shadcn-svelte**(Bits UI). 디자인 토큰은
  `frontend/src/app.css`(shadcn 표준 토큰 + 춘추관 시맨틱 색, `.dark` 다크모드 —
  mode-watcher). shadcn 컴포넌트는 `src/lib/components/ui` 에 소유(복사-인)되며
  `npx shadcn-svelte@latest add <name> -y` 로 추가한다. 색은 토큰만 쓰고(직접 hex
  금지) 상태 뱃지는 `<Badge variant>` 를 쓴다 — 상세 규칙은
  `.claude/rules/dashboard.md` 디자인 방향.

## 디버그 진단 포트 (`WCCG_DEBUG`)

원격 테스트 서버(develop)의 컨테이너 내부 상태를 LAN 의 개발 PC 에서 바로 들여다보며
문제를 빠르게 진단하기 위한 **별도 HTTP 포트**다. `web/debug_server.py` 가 serve·worker
프로세스 안에서 데몬 스레드로 띄운다. **기본 off** — 릴리스 compose 는 이 토글을 주지
않으므로 포트가 열리지 않는다.

```bash
WCCG_DEBUG=on uv run wccg serve     # 로컬에서 켜기 (기본 127.0.0.1:8799)
curl http://127.0.0.1:8799/debug    # 엔드포인트 목록(자체 문서)
```

| env | 기본 | 설명 |
|---|---|---|
| `WCCG_DEBUG` | `off` | `on` 이면 진단 포트를 연다 |
| `WCCG_DEBUG_HOST` | `127.0.0.1` | 컨테이너에서 LAN 노출하려면 `0.0.0.0` (compose 가 주입) |
| `WCCG_DEBUG_PORT` | `8799` | 진단 포트 |
| `WCCG_DEBUG_TOKEN` | (빈값) | 설정 시 모든 요청에 `X-Debug-Token` 헤더 요구 (LAN 노출 시 권장) |

**엔드포인트** — `GET /debug` 가 전체 목록을 돌려준다(자체 문서). 읽기는 GET, 트리거는 POST.

진단(읽기):

| 경로 | 내용 |
|---|---|
| `/debug/health` | 프로세스 생존·버전·백그라운드 스레드(스케줄러/크롤/아카이브) 생존 |
| `/debug/queues` | 단발·크롤·스케줄 큐 상태 + `writes_paused`/이전 모드/스토리지 마이그레이션 |
| `/debug/db` | 테이블별 행 수·무결성 빠른 점검·저널 모드·파일 크기 |
| `/debug/logs?tail=N&level=&src=&q=` | 시스템 로그 tail (워커 트레이스백 포함, `q=` 본문 부분일치) |
| `/debug/search`·`/debug/storage`·`/debug/config` | 검색 인덱스·저장 백엔드+마이그레이션 진행률·유효 설정(**시크릿은 설정 여부만**) |
| `/debug/inspect?url=` | 특정 URL 의 페이지·스냅샷·최근 아카이브 로그(단계/오류) — "왜 이렇게 캡처됐나" |
| `/debug/crawls` · `/debug/crawl/{id}/failures` | 크롤 회차·상태별 페이지 수 / 실패 페이지의 url·오류·시도횟수 |
| `/debug/challenges` · `/debug/log/{id}` | 사람 확인 대기(needs_human) 작업 / 아카이브 로그 1건(steps 파싱) |

제어(트리거 — 모두 코어 함수 경유):

| 경로 | 내용 |
|---|---|
| `POST /debug/capture {url,force?}` | 1회성 캡처를 코어로 동기 실행 → netcheck/추출/스냅샷 결과 트레이스 |
| `POST /debug/run/scheduler`·`/debug/run/archive` | 페이지 스케줄·단발 큐를 1회 처리 |
| `POST /debug/run/crawl {crawl_id?}`·`/debug/run/crawl-schedules` | 크롤 페이지·크롤 스케줄을 1회 처리 (크롤이 멈춘 원인 격리) |
| `POST /debug/run/recover-stale` | 중단으로 in_progress 에 박힌 작업·크롤 페이지를 pending 복구(멱등) |
| `POST /debug/run/reindex {full?}` | 미색인 스냅샷 백필(또는 전체 재색인) |
| `POST /debug/live/{job_id}/cancel`·`/solve` | 라이브 챌린지로 멈춘 워커를 취소/강제해결로 풀어줌 |

**보안**: 시크릿 값은 절대 응답에 넣지 않고(원칙 6), 트리거 쓰기는 모두 코어
모듈(`pipeline`·`scheduler`·`crawler`·`archive_worker`·`searchindex`)을 경유한다(원칙 1) —
직접 DB 조작·임의 SQL/eval·스냅샷 변경 류는 의도적으로 두지 않는다. 비-loopback 바인딩은
경고를 남기며, LAN 노출 시 `WCCG_DEBUG_TOKEN` 으로 보호하는 것을 권장한다.
도커 노출·핫리로드는 [DOCKER.md](DOCKER.md#디버그-진단-포트--핫리로드-develop-전용) 참조.

**릴리스 빌드에는 디버그 코드가 없다.** 런타임 토글(`WCCG_DEBUG`) 위에 한 겹 더 —
릴리스(`:latest`·`:main`·`:vX.Y.Z`) 이미지 빌드는 `web/debug_server.py` 를 **물리적으로
제거**한다(Dockerfile `ARG INCLUDE_DEBUG`, CI 가 develop 빌드에만 `INCLUDE_DEBUG=1` 주입).
호출부(`web/app.py`·`worker.py`)는 파일 부재 시 `ImportError` 를 잡아 graceful no-op 하므로
릴리스 이미지에서는 `WCCG_DEBUG=on` 을 줘도 디버그 서버 코드 자체가 존재하지 않는다.
`config.py` 의 `WCCG_DEBUG*` 상수만 남지만 소비처가 없어 무동작이다.

## 빠른 수정-검증 루프 (`serve --reload`)

`wccg serve --reload` 로 띄우면 소스 변경 시 uvicorn 이 자동 재기동한다. 도커에서는
`docker-compose.reload.yml` 오버레이가 `chunchugwan/` 소스를 bind-mount + `serve --reload`
로 띄워, 재빌드 없이 코드 변경이 즉시 반영된다. 캡처/파이프라인 코드를 고친 뒤
`POST /debug/capture` 로 트리거하면 리로드된(새) 코드가 in-process 로 돌아 결과를 바로
확인할 수 있다(워커 큐를 거치지 않는 타이트 루프). 자세한 도커 사용은 DOCKER.md 참조.

## 모듈 구성

아키텍처 원칙·DB 스키마·코딩 컨벤션은 [CLAUDE.md](../CLAUDE.md) 참조.

| 모듈 | 역할 |
|---|---|
| `chunchugwan/storage.py` | URL 정규화, slug, 스냅샷 파일시스템 레이아웃 |
| `chunchugwan/db.py` | SQLite 인덱스 (모든 DB 접근의 단일 창구) |
| `chunchugwan/capture.py` | Playwright 렌더링, 자원 인라인, 셀렉터 제거 |
| `chunchugwan/extract.py` | 본문 추출(DOM 가시 텍스트 덤프) + 정규화 |
| `chunchugwan/differ.py` | 텍스트 diff + 스크린샷 픽셀 diff |
| `chunchugwan/pipeline.py` | 아카이빙 흐름 (모든 캡처 경로의 공용 코어) |
| `chunchugwan/archive_worker.py` | 단발 아카이빙 작업 큐(archive_jobs) 소비자 — worker·serve 가 폴링 |
| `chunchugwan/auth.py` | 인증 코어 — argon2 해싱, 세션 토큰, TOTP |
| `chunchugwan/oidc.py` | Authentik OIDC 클라이언트 (httpx + PyJWT) |
| `chunchugwan/cli.py` | click CLI |
| `chunchugwan/web/` | FastAPI 대시보드 (인증 라우트 `auth_routes.py` 포함) |
| `chunchugwan/web/debug_server.py` | 디버그 진단 포트 (`WCCG_DEBUG` — 별도 포트, develop 전용) |
