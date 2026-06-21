# 개발

> 테스트 실행, PyCharm 구성, 모듈 구성을 다룬다. 아키텍처 원칙·DB
> 스키마·코딩 컨벤션은 [CLAUDE.md](../CLAUDE.md) 참조.

```bash
uv run pytest                            # 테스트 (네트워크 불필요, ~10초)
```

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
