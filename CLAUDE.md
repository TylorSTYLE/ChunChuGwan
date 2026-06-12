# 춘추관 (ChunChuGwan)

개인 웹 아카이빙 시스템. URL을 받아 전체 페이지를 스냅샷으로 저장하고,
같은 URL을 다시 아카이빙하면 히스토리가 쌓이며 스냅샷 간 비교(diff)가 가능하다.

## 참고 문서 (해당 작업 시 읽을 것)

- `docs/DASHBOARD.md` — 대시보드 화면 13개의 라우트·권한·세부 동작 레퍼런스.
  웹 UI 화면을 추가/수정하기 전에 읽는다.
- `docs/ROADMAP.md` — 완료된 구현 로드맵 히스토리(M1~M8, A1~A9 상세).
  기능의 도입 배경·구현 범위가 궁금할 때 읽는다.

## 기술 스택

- Python 3.12+ / 패키지 관리: `uv` (없으면 pip + venv)
- 캡처: Playwright (chromium, headless)
- DB: SQLite (`archive/index.db`) — ORM 없이 표준 `sqlite3` 사용
- CLI: click
- 대시보드: FastAPI + Jinja2 템플릿 (읽기 전용 + 재아카이빙 트리거)
- 인증: argon2-cffi(패스워드), pyotp+qrcode(TOTP), webauthn(패스키),
  httpx+PyJWT(OIDC — Authentik)
- 테스트: pytest

## 명령어

```bash
uv sync                                  # 의존성 설치
uv run playwright install chromium       # 최초 1회
uv run wccg add <url>                    # 스냅샷 생성
uv run wccg add <url> --force            # 콘텐츠 동일해도 강제 저장
uv run wccg list                         # 전체 아카이브 현황
uv run wccg history <url>                # 해당 URL 스냅샷 목록
uv run wccg diff <url>                   # 최신 2개 스냅샷 비교
uv run wccg diff <url> --from 1 --to 3
uv run wccg delete <url>                 # 아카이브 전체 삭제 (--snapshot N 으로 하나만,
                                         #   --site 로 사이트 전체 — 페이지·크롤·스케줄 일괄)
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
uv run wccg worker [--workers N]         # 아카이빙 워커 — 스케줄·크롤 큐 소비 (worker.py,
                                         #   serve 와 분리 실행 시 WCCG_SCHEDULER=off, N=동시 크롤 수)
uv run wccg backup [dest]                # 전체 백업 tar.gz (DB·인증 포함)
uv run wccg restore <file> [--yes]       # 전체 복원 (현재 데이터를 백업 시점으로 교체)
uv run wccg export [dest]                # 아카이브 데이터만 내보내기 (인증·로그 제외)
uv run wccg import <file> --mode merge   # 가져오기 (merge | overwrite)
uv run wccg compact [--yes]              # 기존 스냅샷 저장 공간 압축 (1회성 마이그레이션)
uv run pytest                            # 테스트
cp compose.example.yaml compose.yaml     # 컴포즈 예제 복사 (최초 1회 — compose.yaml 은 gitignore, 개인 설정은 여기서)
docker compose up -d dashboard           # 대시보드 + 워커 컨테이너 (127.0.0.1:8765)
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
5. **대시보드는 기본 loopback, 외부 노출 시 인증 필수.** 기본 바인딩 127.0.0.1.
   컨테이너 등 포트포워딩이 필요한 환경에서만 `WCCG_HOST` 로 바인딩을
   오버라이드하며(compose 가 0.0.0.0 주입), 호스트 노출은 항상 127.0.0.1
   포트 매핑으로 제한한다. `WCCG_AUTH=off` 는 loopback 바인딩일 때만 허용
   (`cli.serve` 가 강제 — 컨테이너의 0.0.0.0 바인딩에서는 인증이 항상 켜진다).
   아카이빙된 HTML을 렌더링할 때는 반드시 `<iframe sandbox>` (스크립트 실행 금지)
   안에서만 보여준다. 아카이빙된 페이지의 JS를 대시보드 컨텍스트에서 실행하는
   일은 절대 없어야 한다. 허용하는 유일한 sandbox 토큰은
   `allow-top-navigation-by-user-activation` — 사이트 전체 아카이브가
   재작성한 링크(`/crawl/{id}/goto` + `target="_top"`)를 사용자가 직접
   클릭했을 때만 뷰어 전체가 다음 스냅샷으로 이동하게 한다 (스크립트로는
   불가, `allow-scripts`/`allow-same-origin` 절대 추가 금지).
   `/resource/` (공유 자원 CAS)는 유일한 인증 예외
   경로 — 샌드박스 문서의 하위 요청에는 SameSite 쿠키가 안 붙기 때문이며,
   sha256 콘텐츠 주소 이름 + 미디어 타입 화이트리스트(문서 타입 금지) +
   CSP sandbox 로만 서빙한다 (`resources.py` 보안 노트 참조). 함께 저장된
   문서 파일은 별도의 문서 CAS(`documents/`, documents.py)에 두되 /resource/
   로는 절대 합치지 않고, 인증이 걸린 라우트(`/snapshot/{id}/doc/{name}` —
   meta.json documents 목록 검증, `/document/{sha256}/{name}` — snapshot_documents
   행 검증)에서만 항상 첨부파일 다운로드(렌더링 금지)로 서빙한다. compact
   이전 구형 스냅샷의 문서는 스냅샷 안 `files/` 에서 그대로 서빙된다.
6. **인증 데이터 규칙.** 패스워드는 Argon2id 해시만, 세션·API 키는 토큰의
   SHA-256 만 저장 (세션은 서버사이드). 2FA(TOTP·패스키)는 패스워드 로그인에만 적용하고 SSO(OIDC)는
   IdP 의 2FA 를 신뢰한다. 패스키는 공개키만 저장하며 RP ID/origin 은
   `WCCG_PUBLIC_URL` 에서 파생(미설정 시 localhost). 환경변수 목록은
   README "인증" 절 참조.
7. **사설 IP·루프백 게이트.** 아카이빙 대상 호스트의 네트워크 대역은
   `netcheck.py` 가 판정한다(IP 리터럴·localhost 는 즉시, 호스트명은 서버
   리졸버 해석 + TTL 캐시, 해석 실패는 공인 취급). 루프백은 항상 거부 —
   대시보드 자신이 아카이브로 새는 것을 막는다. 사설 대역(RFC1918·링크
   로컬·ULA)은 시스템 설정의 로컬 네트워크 태그(`network_tags`, id 는
   GUID)를 지정해야 한다. 강제는 코어(pipeline `_resolve_network_tag` —
   캡처 전 + 리다이렉트 최종 URL 재검증, crawler `_check_network_tag`)가
   하고, 웹 폼·REST API 는 같은 정책을 동기 검증으로 미리 보여준다.
   공개 주소에 태그를 넘기면 무시된다.

## 저장 구조

```
archive/
├── index.db
├── resources/                       # 스냅샷 간 공유 자원 CAS (resources.py)
│   └── {sha256 앞 2자}/{sha256}{확장자}   # 이미지·폰트·CSS, 콘텐츠 주소라 불변.
│                                    #   참조(snapshot_resources)가 0 이 되면 삭제(GC)
├── documents/                       # 문서 파일 CAS (documents.py — 인증 라우트 전용)
│   └── {sha256 앞 2자}/{sha256}{확장자}   # PDF·워드·한글 등, 같은 내용은 한 번만.
│                                    #   참조(snapshot_documents)가 0 이 되면 삭제(GC)
└── sites/
    └── {domain}/
        └── {slug}-{url_hash8}/
            └── {timestamp ISO, 콜론은 - 로}/
                ├── page.html.gz    # 단일 HTML (gzip). 큰 자원은 /resource/ 참조,
                │                   #   작은 자원(<4KB)은 data URI 인라인 유지
                ├── raw.html.gz     # 렌더링 후 DOM 소스 (gzip)
                ├── content.md      # 추출+정규화 텍스트
                ├── screenshot.webp # 전체 페이지 (변환 실패 시 screenshot.png 유지)
                ├── files/          # (구형 스냅샷만) 문서 파일 — wccg compact 가
                │                   #   문서 CAS 로 이전한다. 신규 스냅샷은 없음
                └── meta.json       # url, final_url, 시각, 해시, http 정보,
                                    #   documents 목록(문서 서빙 화이트리스트)
```

`wccg compact` 이전의 구형 스냅샷(page.html / raw.html / screenshot.png)도
그대로 읽힌다 — 대시보드 파일 라우트가 신/구 이름을 모두 해석한다.

## DB 스키마

`chunchugwan/db.py`의 `SCHEMA` 참조. 핵심 테이블:
- `sites` — 서브도메인 단위 그룹 (site_key UNIQUE = `storage.site_key` —
  www 제거 호스트 + 기본 외 포트, IP 는 그대로). 모든 페이지·크롤·크롤
  스케줄은 사이트에 속한다 (`site_id` FK — 생성 시 자동 연결, 기존 데이터는
  `db._migrate` 의 `_backfill_sites` 가 자동 백필). www 와 apex 는 같은
  사이트, 다른 서브도메인은 다른 사이트. 마지막 소속 행이 사라지면 사이트
  행도 자동 삭제(prune). 사이트 단위 삭제는 `deletion.delete_site`
  (`wccg delete <url> --site`) — 소속 페이지·크롤 회차·크롤 스케줄 일괄
- `pages` — 정규화된 URL 단위 (1 URL = 1 row). 사설 대역 페이지는
  `network_tag_id` 로 로컬 네트워크 태그를 참조 (crawls·crawl_schedules 도
  같은 컬럼 보유 — 크롤 페이지·스케줄 재실행에 태그가 이어진다)
- `network_tags` — 로컬 네트워크 태그 (id 는 GUID 자동 발급, 이름 유일,
  설명). 사설 IP 대역 아카이빙은 태그 지정이 필수, 루프백은 항상 금지
  (아키텍처 원칙 7 · netcheck.py). 참조 중인 태그는 삭제 거부
- `snapshots` — 스냅샷 단위, `pages.id` FK, content_hash 보관
- `checks` — 중복으로 저장 생략된 확인 기록
- `snapshot_resources` — 스냅샷이 /resource/ CAS 로 참조하는 공유 자원
  인덱스 (CAS 이름 = sha256+확장자, 원본 url — 모를 수 있음). 캡처가
  기록하고(인라인 자원의 sha256 은 브라우저 crypto.subtle 로 계산), 삭제 시
  참조가 0 이 된 CAS 파일은 deletion.py 가 GC 한다. 자원 인라인 실패 시
  같은 url 의 과거 캡처본을 재사용하는 폴백(pipeline._resource_fallback)의
  조회 인덱스이기도 하다. 참조가 기록되지 않은 구형 스냅샷은 저장공간
  최적화(compact)의 백필이 채운다
- `snapshot_documents` — 스냅샷의 문서 파일 참조 (url·정제 파일명·bytes·
  sha256·content_type). 파일 본체는 문서 CAS — 같은 sha256 은 한 번만
  저장되고, 삭제 시 참조가 0 이 된 CAS 파일은 deletion.py 가 GC 한다.
  대시보드 `/documents` 통합 목록의 데이터 소스
- `archive_logs` — 아카이브 실행 로그 (성공/실패, 단계별 소요시간 JSON,
  출처 cli/web/schedule/api/crawl)
- `schedules` — 페이지별 주기적 재아카이빙 (주기 1시간~1개월, 다음 실행 시각,
  1일 단위 주기는 `run_at_time` HH:MM 으로 실행 시각 지정 — 서버 로컬 시간)
- `crawls` / `crawl_pages` — 사이트 전체 아카이브의 실행 회차. 크롤(범위
  host+path 프리픽스 — 호스트 비교는 사이트 키 기준이라 www↔apex 를
  넘나든다, 옵션, 상태)과 페이지 큐(pending/in_progress/done/failed,
  시도 횟수·재시도 시각, 확인된 snapshot_id 참조). 큐가 DB 에 있어 재시작
  후에도 이어지고, 클레임은 원자적 UPDATE 라 serve/워커/CLI 동시 실행에
  안전. 같은 크롤은 한 번에 한 페이지만 처리(클레임이 in_progress 배제 +
  next_page_at 간격) — `wccg worker` 의 크롤 스레드 수만큼 서로 다른
  크롤이 병렬 진행된다.
  같은 시작 URL 의 크롤이 진행 중이면 새 등록은 그 크롤로 자동 병합
  (`start_crawl` 이 기존 크롤 + merged=True 반환, 새 옵션은 버림).
  실패 재시도 대기·횟수는 `settings` 의 `crawl_retry_backoff_seconds` 기준
- `crawl_schedules` — 사이트 전체 아카이브의 주기적 재실행 (시작 URL 별
  크롤 옵션 + 주기 1시간~1개월·`run_at_time`). 기한이 되면 같은 옵션으로
  새 크롤을 등록(source=schedule)하되, 같은 URL 의 크롤이 진행 중이면 끝날
  때까지 미룬다. serve 크롤러 스레드·`wccg worker`·`wccg schedule run`/
  `crawl run` 이 실행하며 next_run_at 갱신은 원자적 클레임이라 동시 실행에 안전
- `users` / `identities` / `sessions` / `oidc_states` — 인증 (사용자, OIDC 연결,
  서버사이드 세션, OIDC state 1회용 기록). `users.role` 은
  admin(관리자)/archiver(아카이빙 가능)/viewer(보기 전용)/pending(권한없음 —
  가입 승인 대기, 로그인은 되지만 `/pending` 안내 페이지 외 접근 불가)/
  blocked(차단)/withdrawn(탈퇴 — 본인 탈퇴로만 진입, 로그인 거부.
  관리자가 부여하거나 되돌릴 수 없고, 사용자 관리에서 계정 정보를
  삭제(대상 이메일 입력 확인)해야 같은 이메일 재가입·초대가 풀린다).
  신규 가입·SSO 자동 생성의 초기 권한은 `settings` 의
  `signup_default_role` (pending/viewer/archiver, 기본 pending — 관리자가
  사용자 관리에서 권한을 부여해 승인). `users.is_founder` 는 최초 등록
  관리자로 권한 변경 불가
- `settings` — 대시보드에서 변경하는 key-value 런타임 설정. 가입 설정
  (`signup_enabled` on/off 기본 on — off 면 `/signup` 차단 + 로그인 화면
  가입 링크 숨김(초대 가입은 허용), `signup_default_role`)과 사이트 아카이브
  설정 (`crawl_default_max_pages`/`crawl_default_max_depth`/
  `crawl_default_delay_seconds` — 새 크롤 옵션 기본값,
  `crawl_retry_backoff_seconds` — 실패 재시도 대기 쉼표 목록(초), 최대 시도
  = 길이 + 1, 진행 중 크롤에도 즉시 적용. 해석·검증은
  `crawler.crawl_defaults`/`retry_backoff`, 오염 시 config 기본값 폴백)
- `webauthn_credentials` — 패스키 공개키 자격증명 (2FA 용)
- `api_keys` — 외부 소프트웨어용 API 키 (`/api/v1` REST API 인증).
  관리자만 발급, 모든 관리자가 공동 관리. 키마다 보기/아카이브 권한과
  만료 시각(NULL=영구), 토큰은 SHA-256 해시만 저장 (원문은 발급 시 1회 표시)

## 코딩 컨벤션

- 타입 힌트 필수, docstring은 한국어로 간결하게
- 외부 입력(URL, 파일 경로)은 항상 검증/정규화 후 사용 — path traversal 주의
- 네트워크 요청에는 타임아웃 필수 (페이지 로드 기본 30s)
- 새 기능 = 해당 테스트 추가. 네트워크 의존 테스트는 로컬 fixture HTML 사용
- 커밋은 기능 단위로 작게

## 대시보드 디자인 방향

- 화면 14개 — 현황(`/`), 목록(`/archives` — 사이트(서브도메인) 단위),
  사이트 상세(`/sites/{id}` — 소속 페이지·크롤 회차·스케줄·사이트 삭제),
  문서(`/documents` — 문서 파일 통합 목록), 새 아카이빙(`/archive/new`),
  사이트 아카이브 진행(`/crawls/{id}` — 크롤 회차 상세), 스케줄(`/schedules`),
  타임라인, 스냅샷 뷰어, diff 뷰어, 로그, 시스템, 사용자, API 키.
  화면별 라우트·권한·세부 동작은 `docs/DASHBOARD.md` 참조.
- 도구다운 밀도 있는 UI. 모노스페이스로 해시/시각 표기, 변경 상태는 색 뱃지
  (변경=amber, 동일=gray, 신규=green). 과한 장식/그라데이션 금지.
- 다국어(ko/en): `web/i18n.py` — 한국어 원문이 메시지 키(gettext msgid 방식),
  언어별 "원문 → 번역" dict 로 확장. 로케일은 `wccg_lang` 쿠키(헤더의 언어
  선택, `POST /lang`) → Accept-Language → ko. 템플릿은 `_("…")`, 라우트는
  `i18n.t(request, "…")`. 새 UI 문자열 추가 시 en 카탈로그도 채울 것 —
  템플릿 리터럴 키 누락은 `tests/test_i18n.py` 가 검사한다. CLI 는 한국어 유지.
- diff 뷰: 텍스트 side-by-side + 스크린샷 비교(슬라이더 또는 토글)

## 구현 로드맵

M1~M8, A1~A10 전 마일스톤 완료 — 상세 내역은 `docs/ROADMAP.md` 참조.
새 마일스톤은 진행 중인 항목만 여기에 두고, 완료되면 ROADMAP.md 로 내린다.
각 마일스톤 완료 시: 테스트 통과 확인 → 체크박스 갱신 → 커밋.

### A11 — 사이트 단위 아카이브 구조 (진행 중)

- [x] 사이트 논리 모델 — sites 테이블, site_key(www 통합·포트 포함),
  자동 마이그레이션(백필), in_scope www 통합, 사이트 단위 삭제(CLI `--site`)
- [x] 대시보드 사이트 재편 — /archives 사이트 그룹핑, 사이트 상세 화면 신설,
  /documents·타임라인 사이트 표시, 사이트 삭제 UI, 현황 사이트 수
- [x] 자원 참조 추적 — snapshot_resources(url 포함), 캡처 시 기록, 삭제 시
  고아 자원 GC, 인라인 실패 시 같은 URL 의 과거 캡처본 재사용
- [ ] 저장공간 최적화 — compact 에 참조 백필 + 고아 sweep 단계 추가,
  시스템 메뉴 라벨 "저장공간 최적화"로 변경 (CLI 명령명은 유지)
