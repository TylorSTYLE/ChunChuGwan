# 춘추관 (ChunChuGwan)

개인 웹 아카이빙 시스템. URL을 받아 전체 페이지를 스냅샷으로 저장하고,
같은 URL을 다시 아카이빙하면 히스토리가 쌓이며 스냅샷 간 비교(diff)가 가능하다.

## 경로별 규칙 (해당 경로 작업 시 `.claude/rules/` 의 해당 파일을 읽을 것)

도메인별 상세 규칙(DB 스키마·저장 구조·대시보드 등)은 `.claude/rules/*.md` 로 분리돼 있다.
각 파일 frontmatter 의 `paths:` 가 적용 경로이며, **아래 표가 정본 인덱스**다 — 해당 경로를
만지기 전에 그 규칙 파일을 읽는다 (여러 칸에 걸치는 파일은 해당하는 규칙을 모두 읽는다).

| 작업 경로 | 규칙 파일 |
|---|---|
| `chunchugwan/db.py` | `.claude/rules/database.md` — DB 스키마 전체·코어 테이블·마이그레이션 |
| `chunchugwan/storage.py`·`resources.py`·`documents.py`·`deletion.py`·`optimize.py`·`backup.py` | `.claude/rules/storage.md` — 저장 구조·CAS·문서 스냅샷·compact |
| `chunchugwan/web/**`·`frontend/**`·`differ.py` | `.claude/rules/dashboard.md` — 대시보드 디자인·SvelteKit SPA·렌더링 보안(원칙 5)·i18n·diff |
| `chunchugwan/auth.py`·`oidc.py`·`credentials.py`·`crypto.py`·`mailer.py`·`web/auth_routes.py`·`web/permissions.py` | `.claude/rules/authentication.md` — 인증 데이터 규칙(원칙 6)·역할/권한·자격증명·SMTP |
| `chunchugwan/capture.py`·`pipeline.py`·`crawler.py`·`extract.py`·`scheduler.py`·`archive_worker.py`·`worker.py`·`certs.py`·`netcheck.py`·`browser_engine.py`·`trackers.py`·`live_challenge.py`·`ai_challenge.py`·`input_replay.py` | `.claude/rules/capture-crawl.md` — 캡처·크롤·스케줄·네트워크 게이트(원칙 7)·인증서·라이브·AI 챌린지 |
| `chunchugwan/searchindex.py`·`doctext.py` | `.claude/rules/search.md` — 전문 검색(FTS5 trigram)·문서 본문 추출 |
| `chunchugwan/web/api_routes.py`·`ingest.py`·`extension/**` | `.claude/rules/api-extension.md` — REST API·API 키·확장 클라이언트 캡처 |
| `tests/**` | `.claude/rules/testing.md` — 테스트 컨벤션 |
| `.github/workflows/**`·`Dockerfile`·`docker-compose*.yml`·`pyproject.toml` | `.claude/rules/release-docker.md` — 릴리스 자동화(gitflow CI)·Docker |

사용자용 기능 문서는 `docs/` 에 주제별로 나눠져 있다 (CRAWLING·STORAGE·
SEARCH·DOCKER·API·AUTHENTICATION·DEVELOPMENT — README 는 빠른 시작 + 링크만 둔다).
해당 기능의 동작·옵션·CLI 를 바꾸면 README 요약과 함께 그 docs 파일도 갱신한다.
웹 UI 화면을 추가/수정하기 전에는 `docs/DASHBOARD.md`(대시보드 화면 24개의 라우트·권한·
세부 동작 레퍼런스)를, 기능의 도입 배경·구현 범위가 궁금하면 `docs/ROADMAP.md`(완료된
구현 로드맵 히스토리 M1~M8·A1~A15)를 읽는다.

## 기술 스택

- Python 3.12+ / 패키지 관리: `uv` (없으면 pip + venv)
- 캡처: Playwright (chromium, headless)
- DB: SQLite (`archive/index.db`) — ORM 없이 표준 `sqlite3` 사용
  (전문 검색은 FTS5 trigram 가상테이블 — searchindex.py)
- 문서 본문 추출(검색 색인): pypdf(PDF) + 표준 zipfile/XML(docx·pptx·xlsx·
  odf·hwpx·epub) — doctext.py
- CLI: click
- 대시보드: FastAPI + SvelteKit SPA (정적 셸을 루트(/)로 서빙 + `/api/web` JSON API,
  읽기 전용 + 재아카이빙 트리거). 스타일링은 **Tailwind CSS v4 + shadcn-svelte**
  (Bits UI 기반 컴포넌트는 `frontend/src/lib/components/ui`, 공통 래퍼는
  `frontend/src/lib/components`), 다크모드는 **mode-watcher**(`.dark` 클래스 +
  `app.css` 의 HSL 토큰). 프론트엔드 소스는 `frontend/`, 빌드 산출물은
  `chunchugwan/web/frontend_dist`(개발 시 `frontend/build`). C2 컷오버로 SSR(Jinja2) 제거.
- 인증: argon2-cffi(패스워드), pyotp+qrcode(TOTP), webauthn(패스키),
  httpx+PyJWT(OIDC — Authentik)
- 테스트: pytest

## 명령어

```bash
uv sync                                  # 의존성 설치
uv run playwright install chromium       # 최초 1회
uv run wccg add <url>                    # 아카이빙 작업을 큐에 등록 (worker 가 캡처)
uv run wccg add <url> --force            # 콘텐츠 동일해도 강제 저장
uv run wccg archive run                  # 기한이 된 단발 아카이빙 작업 1회 처리 (cron 용,
                                         #   serve/worker 가 돌고 있으면 자동 처리되어 불필요)
uv run wccg list                         # 전체 아카이브 현황
uv run wccg history <url>                # 해당 URL 스냅샷 목록
uv run wccg diff <url>                   # 최신 2개 스냅샷 비교
uv run wccg diff <url> --from 1 --to 3
uv run wccg search <검색어>              # 본문·첨부 문서 전문 검색 (FTS5 trigram,
                                         #   여러 단어=AND, --domain·--latest·--limit)
uv run wccg search status                # 검색 인덱스 상태 / 미색인 스냅샷 수
uv run wccg search reindex [--all]       # 미색인 스냅샷 백필 (--all 은 전체 재색인)
uv run wccg search verify [--repair]     # 인덱스 정합성 점검(과소 색인·orphan) / 교정
uv run wccg delete <url>                 # 아카이브 삭제 (--snapshot N 으로 하나만,
                                         #   --site 로 사이트 전체). 휴지통이 켜져 있으면
                                         #   페이지·사이트 삭제는 휴지통으로 이동(--hard 면 즉시 영구삭제,
                                         #   --snapshot 은 항상 즉시)
uv run wccg trash list                   # 휴지통 목록 / restore <id|URL> 복원 /
                                         #   purge <id|URL>|--expired|--all 영구삭제
uv run wccg schedule add <url> --every 12h  # 주기적 재아카이빙 등록 (1h ~ 1mo)
uv run wccg schedule add <url> --every 1d --at 09:00  # 1일 단위 주기는 실행 시각(서버 로컬) 지정 가능
uv run wccg schedule next <url> <시각>       # 다음 실행 시각 변경 (ISO, 타임존 없으면 로컬)
uv run wccg schedule list                # 스케줄 목록 / remove <url> 로 해제
uv run wccg schedule run                 # 기한이 된 스케줄 1회 실행 (cron 용, 크롤 스케줄 포함)
uv run wccg crawl add <url>              # 사이트 전체 아카이브 (같은 호스트, 경로 프리픽스 이하)
uv run wccg crawl add <url> --max-pages 50 --max-depth 3 --delay 10 [--no-wait]
uv run wccg crawl list                   # 크롤 목록 / run 으로 기한 된 페이지 처리 (cron 용)
uv run wccg crawl schedule add <url> --every 1w  # 주기적 사이트 재아카이빙 (--at·크롤 옵션 지정 가능)
uv run wccg crawl schedule list          # 크롤 스케줄 목록 / remove <url> 로 해제
uv run wccg serve                        # 대시보드 (127.0.0.1:8765)
uv run wccg serve --host 0.0.0.0         # 외부 노출 (인증 켜진 상태에서만 허용)
uv run wccg worker [--workers N]         # 아카이빙 워커 — 단발 아카이빙·스케줄·크롤 큐
                                         #   소비 (worker.py, serve 와 분리 시 WCCG_SCHEDULER=off,
                                         #   N=동시 크롤 수)
uv run wccg backup [dest]                # 전체 백업 tar.gz (DB·인증 포함)
uv run wccg restore <file> [--yes]       # 전체 복원 (현재 데이터를 백업 시점으로 교체)
uv run wccg export [dest]                # 아카이브 데이터만 내보내기 (인증 데이터 제외)
uv run wccg import <file> --mode merge   # 가져오기 (merge | overwrite)
uv run wccg compact [--yes]              # 저장공간 최적화 — 압축 변환 + 인라인 스타일 추출
                                         #   + 자원 참조 백필 + 고아 공유 자원 정리 (멱등)
uv run pytest                            # 테스트
docker compose up -d dashboard           # 대시보드 + 워커 (:latest, 127.0.0.1:8765 — 개인 설정은 docker-compose.override.yml 에)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d dashboard  # develop(테스트) 이미지로
docker compose run --rm cli add <url>    # 컨테이너에서 스냅샷 생성
```

## 아키텍처 원칙 (중요 — 반드시 지킬 것)

1. **쓰기는 코어 모듈을 통해서만.** 스냅샷 생성/삭제는 `storage.py` + `db.py`를
   거친다. 대시보드든 외부 에이전트든 직접 파일/DB를 조작하지 않는다.
2. **스냅샷은 불변(immutable).** 한번 저장된 스냅샷 디렉토리는 수정하지 않는다.
   변경 = 새 스냅샷. 유일한 예외는 `wccg compact` — 저장 형태만 바꾸는
   내용 보존 변환(자원 CAS 추출·gzip·WebP)으로, 스냅샷이 담는 정보는 그대로다.
3. **콘텐츠 해시 기반 중복 제거.** 정규화된 텍스트의 SHA-256이 직전 스냅샷과
   같으면 새 스냅샷을 만들지 않고 `checks` 테이블에 "확인했음" 기록만 남긴다.
   (`--force` 시 예외)
4. **비교는 정규화된 텍스트 기준.** 타임스탬프, CSRF 토큰, 광고 등 노이즈는
   `extract.py`의 정규화 단계에서 제거한 후 해시/diff 한다.
5. **대시보드는 기본 loopback, 외부 노출 시 인증 필수.** 아카이빙된 HTML 은 항상
   `<iframe sandbox>` 안에서만 렌더(스크립트 실행 금지) — `allow-scripts`/
   `allow-same-origin` 절대 금지. `/resource/` (공유 자원 CAS)만 유일한 인증 예외.
   (loopback·바인딩·iframe·CAS 서빙 상세 → `.claude/rules/dashboard.md`)
6. **인증 데이터 규칙.** 춘추관이 사용자를 인증하는 데이터(비밀번호·세션·API 키·
   패스키)는 단방향(Argon2id·SHA-256) 저장만. 아카이빙 대상 사이트에 로그인하기
   위한 외부 자격증명만 replay 가 필요해 **예외적으로 대칭 암호화**(`WCCG_SECRET_KEY`,
   `export` 제외)로 저장한다 — 사용자 인증 데이터엔 절대 금지.
   (상세 → `.claude/rules/authentication.md`)
7. **사설 IP·루프백 게이트.** 아카이빙 대상 호스트의 네트워크 대역은 `netcheck.py`
   가 판정한다. 루프백은 항상 거부(대시보드 누수 방지), 사설 대역(RFC1918·링크
   로컬·ULA)은 로컬 네트워크 태그(`network_tags`) 지정이 필수. 강제는 코어
   (pipeline·crawler). (상세 → `.claude/rules/capture-crawl.md`)

## 코딩 컨벤션

- 타입 힌트 필수, docstring은 한국어로 간결하게
- 외부 입력(URL, 파일 경로)은 항상 검증/정규화 후 사용 — path traversal 주의
- 네트워크 요청에는 타임아웃 필수 (페이지 로드 기본 30s)
- 새 기능 = 해당 테스트 추가 (네트워크 의존 테스트·실행은 `.claude/rules/testing.md`)
- 커밋은 기능 단위로 작게
- **브랜치 흐름 = gitflow.** 작업 시작 시 반드시 `git fetch origin` 후 원격 최신
  `origin/develop` 에서 분리한다(로컬 develop 캐시 사용 금지). 기능 PR 은 `develop`
  을 베이스로 머지한다(main 직행 금지). develop 에 병합 후 CI 가 develop→main 릴리스
  PR 을 자동 생성하면 **사람이 검토 후 merge 커밋으로 머지**한다(squash 금지 —
  develop 가 main 의 조상으로 남아야 FF 동기화가 유지됨). CI 자동화·`release:*`
  라벨·버전 결정 상세는
  `.claude/rules/release-docker.md` 참조.

## 구현 로드맵

M1~M8, A1~A15 전 마일스톤 완료 — 상세 내역은 `docs/ROADMAP.md` 참조.
새 마일스톤은 진행 중인 항목만 여기에 두고, 완료되면 ROADMAP.md 로 내린다.
각 마일스톤 완료 시: 테스트 통과 확인 → 체크박스 갱신 → 커밋.
