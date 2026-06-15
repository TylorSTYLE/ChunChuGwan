# 춘추관 (ChunChuGwan)

개인 웹 아카이빙 시스템. URL을 받아 전체 페이지를 스냅샷으로 저장하고,
같은 URL을 다시 아카이빙하면 히스토리가 쌓이며 스냅샷 간 비교(diff)가 가능하다.

## 참고 문서 (해당 작업 시 읽을 것)

- `docs/DASHBOARD.md` — 대시보드 화면 21개의 라우트·권한·세부 동작 레퍼런스.
  웹 UI 화면을 추가/수정하기 전에 읽는다.
- `docs/ROADMAP.md` — 완료된 구현 로드맵 히스토리(M1~M8, A1~A11 상세).
  기능의 도입 배경·구현 범위가 궁금할 때 읽는다.

사용자용 기능 문서는 `docs/` 에 주제별로 나눠져 있다 (CRAWLING·STORAGE·
SEARCH·DOCKER·API·AUTHENTICATION·DEVELOPMENT — README 는 빠른 시작 + 링크만 둔다).
해당 기능의 동작·옵션·CLI 를 바꾸면 README 요약과 함께 그 docs 파일도 갱신한다.

## 기술 스택

- Python 3.12+ / 패키지 관리: `uv` (없으면 pip + venv)
- 캡처: Playwright (chromium, headless)
- DB: SQLite (`archive/index.db`) — ORM 없이 표준 `sqlite3` 사용
  (전문 검색은 FTS5 trigram 가상테이블 — searchindex.py)
- 문서 본문 추출(검색 색인): pypdf(PDF) + 표준 zipfile/XML(docx·pptx·xlsx·
  odf·hwpx·epub) — doctext.py
- CLI: click
- 대시보드: FastAPI + Jinja2 템플릿 (읽기 전용 + 재아카이빙 트리거)
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
                │                   #   작은 자원(<4KB)은 data URI 인라인 유지.
                │                   #   큰 인라인 <style>(사이트 공통 CSS)도
                │                   #   /resource/*.css 로 추출해 스냅샷 간 공유
                ├── raw.html.gz     # 렌더링 후 DOM 소스 (gzip)
                ├── content.md      # 추출+정규화 텍스트
                ├── screenshot.webp # 전체 페이지 (WebP 한도 초과·역효과면
                │                   #   screenshot.png 유지 + .keep 마커 — 카운트 제외)
                ├── files/          # (구형 스냅샷만) 문서 파일 — wccg compact 가
                │                   #   문서 CAS 로 이전한다. 신규 스냅샷은 없음
                └── meta.json       # url, final_url, 시각, 해시, http 정보,
                                    #   documents 목록(문서 서빙 화이트리스트)
```

`wccg compact` 이전의 구형 스냅샷(page.html / raw.html / screenshot.png)도
그대로 읽힌다 — 대시보드 파일 라우트가 신/구 이름을 모두 해석한다.

URL 자체가 파일 다운로드(download.php?file=...pdf 등)면 페이지 캡처 대신
**문서 스냅샷**으로 저장된다 (capture 가 `CaptureDownloadError` 로 감지 →
pipeline `_archive_document_url` → `documents.download_direct`). 파일 본체는
문서 CAS 에, 스냅샷 디렉토리에는 생성된 안내 page.html.gz + 문서 메타데이터
content.md(파일 sha256 포함 — 같은 파일이면 unchanged) + meta.json 만 남고
raw.html·스크린샷은 없다 (뷰어는 스크린샷 탭을 숨긴다). 파일명은
Content-Disposition(EUC-KR 모지바케 복구 포함) → URL 경로 → 쿼리 값 →
content-type 순으로 결정하며, 문서 화이트리스트 확장자를 못 정하면 실패.

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
  같은 컬럼 보유 — 크롤 페이지·스케줄 재실행에 태그가 이어진다).
  `credential_id` 는 아카이빙 시 쓸 로그인 자격증명(`site_credentials`)을
  가리킨다 — 새 아카이빙 폼에서 도메인의 자격증명을 골라 연결하면 저장되고
  재아카이빙·스케줄에도 이어진다 (network_tag_id 와 같은 경로로
  `archive_url`→`get_or_create_page` 가 설정, 자격증명 삭제 시 NULL).
  명시적 http URL 은 신규 등록 시 https 지원(유효 인증서 + 응답 <400,
  HSTS 의 리다이렉트 포함)을 확인해 https 로 승격한다
  (`pipeline.upgrade_http_to_https` — 크롤·크롤 스케줄 등록도 동일,
  기존 http 페이지는 히스토리 유지를 위해 그대로 둔다). 캡처 폴백 사슬은
  https(검증) → 인증서 오류면 https(검증 무시 — 자체 서명 NAS 등, 실행
  로그에 기록, 문서 다운로드도 verify 해제) → http (스킴 생략 입력 또는
  연결 실패에 한해)
- `network_tags` — 로컬 네트워크 태그 (id 는 GUID 자동 발급, 이름 유일,
  설명). 사설 IP 대역 아카이빙은 태그 지정이 필수, 루프백은 항상 금지
  (아키텍처 원칙 7 · netcheck.py). 참조 중인 태그는 삭제 거부. 같은 사설
  IP·포트(= 같은 site_id) 집합을 가리키는 두 태그는 시스템 화면에서 병합 가능
  (출처→대상으로 참조 이전 후 출처 삭제 — `db.merge_network_tags`)
- `site_certificates` — https 아카이빙 때 받은 서버 리프 인증서의 버전
  이력 (`certs.py` — 캡처와 별도 핸드셰이크로 수집·파싱, 실패해도
  아카이빙은 진행). 버전 식별은 (site_id, host, DER sha256 지문) — 같은
  인증서는 last_seen 갱신, 갱신된 인증서는 새 행이 되고 이전 버전은
  보존된다. PEM 원문 보관(`/sites/{id}/certificates/{cert_id}.pem` 첨부
  다운로드), verified 는 캡처의 인증서 검증 통과 여부(자체 서명 구분).
  콘텐츠 동일(unchanged) 실행에서도 기록된다. 사이트 삭제·prune 시 함께 정리
- `snapshots` — 스냅샷 단위, `pages.id` FK, content_hash 보관.
  `search_indexed` 는 텍스트 검색 인덱스(snapshot_fts) 반영 여부 — 0 이면
  `wccg search reindex` 백필 대상 (resources_indexed 와 같은 패턴)
- `checks` — 중복으로 저장 생략된 확인 기록
- `snapshot_resources` — 스냅샷이 /resource/ CAS 로 참조하는 공유 자원
  인덱스 (CAS 이름 = sha256+확장자, 원본 url — 모를 수 있음). 캡처가
  기록하고(인라인 자원의 sha256 은 crypto.subtle, http 등 비보안 컨텍스트는
  expose_function 으로 노출된 Python hashlib 바인딩으로 폴백), 삭제 시
  참조가 0 이 된 CAS 파일은 deletion.py 가 GC 한다. 자원 인라인 실패 시
  같은 url 의 과거 캡처본을 재사용하는 폴백(pipeline._resource_fallback)의
  조회 인덱스이기도 하다. 참조가 기록되지 않은 구형 스냅샷은 저장공간
  최적화(compact)의 백필이 채우고, 인라인 <style> 이 추출되지 않은 구형
  스냅샷(snapshots.css_externalized=0)은 같은 최적화가 공통 CSS 를
  /resource/*.css 로 추출한다
- `snapshot_documents` — 스냅샷의 문서 파일 참조 (url·정제 파일명·bytes·
  sha256·content_type). 파일 본체는 문서 CAS — 같은 sha256 은 한 번만
  저장되고, 삭제 시 참조가 0 이 된 CAS 파일은 deletion.py 가 GC 한다.
  대시보드 `/documents` 통합 목록의 데이터 소스
- `snapshot_fts` — 전문 검색 FTS5 가상테이블 (rowid=snapshots.id, 컬럼
  content/title/url, tokenize=trigram). 색인 본문 = content.md(정규화 텍스트)
  + 첨부 문서 본문(doctext.py: PDF·OOXML·ODF·HWPX·EPUB). 쓰기/조회 SQL 은
  db.py 가 소유하고(원칙 1), 텍스트 조립·쿼리 해석·스니펫은 searchindex.py.
  신규 스냅샷은 pipeline 이 저장 시 색인(search_indexed=1), 구형·가져온·실패
  스냅샷은 `wccg search reindex` 백필. 삭제는 db.delete_snapshot/delete_page
  가 함께 제거. 한국어는 trigram 부분문자열(3글자+), 1~2글자는 LIKE 폴백.
  FTS5 없는 SQLite 빌드에서는 생성이 실패해도 검색만 비활성(graceful) —
  기존 아카이빙은 영향 없음. 재생성 가능한 파생 데이터라 `export` 제외
- `archive_logs` — 아카이브 실행 로그 (성공/실패, 단계별 소요시간 JSON,
  출처 cli/web/schedule/api/crawl). `requested_by` 는 직접 요청한 사용자
  (web·확장 토큰 소유자) — '내 아카이브'(`/settings/archives`)의 필터 기준.
  큐(archive_jobs.requested_by)를 거쳐 이어지며, cli/schedule/crawl 은 NULL
- `system_logs` — 앱 동작 로그 (`system_log.py` 의 logging 핸들러가
  chunchugwan 네임스페이스의 INFO 이상 레코드를 적재 — 레벨·로거·출처
  serve/worker/cli·트레이스백). 비차단 큐 + 쓰기 스레드, 보관 한도
  (`WCCG_SYSTEM_LOG_MAX_ROWS`) 초과분 자동 정리. 대시보드 `/system/logs`
  (관리자 전용)의 데이터 소스
- `archive_jobs` — 단발(즉시) 아카이빙 작업 큐. 대시보드 새/재아카이빙·실패
  재시도·REST API·CLI `add` 가 캡처를 직접 실행하지 않고 이 큐에 넣으면,
  worker(또는 serve 단일 프로세스)의 `archive_worker` 가 소비해 `pipeline.archive_url`
  을 호출한다 — 캡처 실행 지점을 한 프로세스로 통일해 스텔스 캡처 설정
  (`WCCG_CAPTURE_*`)이 그 프로세스에만 있으면 되게 한다. `crawl_pages` 와 같은
  'DB 큐 + 원자적 클레임 + 폴링' 패턴(pending/in_progress, attempts·next_attempt_at·
  claimed_at·error). 같은 URL 의 활성 작업은 부분 UNIQUE 로 하나만(중복 enqueue
  무시). 회차·범위·링크추적·페이싱이 없어 단순하며, 완료/최종실패 행은 삭제하고
  결과·오류는 `archive_logs` 가 보존한다. interval 이 실리면 소비자가 캡처 후
  주기를 `schedules` 에 등록한다. `wccg worker`/serve(`WCCG_SCHEDULER`)/`wccg archive
  run` 이 소비한다. 진행 상태(`/archive/active` 폴링)의 데이터 소스.
  `WCCG_LIVE_CHALLENGE=on` 이면 자동으로 못 푼 인터랙티브 챌린지를 사람이
  대시보드에서 직접 푸는 라이브 세션 컬럼(`needs_human_at`·`live_token`·
  `live_owner_id`·`live_cancel`·`live_viewport_w/h`)을 쓴다 — worker 가 살아있는
  page 를 붙든 채(큐 진행 멈춤) 화면(스크린샷 파일 `cache/live/`)·입력
  (`live_commands` 테이블)으로 대시보드와 조율한다 (live_challenge.py, 원칙 7
  의 사설/루프백 가드를 라이브 매 폴링에 적용). 데이터센터 IP 평판으로는
  사람이 눌러도 통과가 보장되지 않는 최후 수단
- `live_commands` — 라이브 챌린지 세션의 사람 입력 명령 큐 (대시보드 INSERT →
  worker 가 seq 순으로 page.mouse/keyboard 재생, 타이밍·드래그 재현)
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
  키마다 보기/아카이브 권한과 만료 시각(NULL=영구), 토큰은 SHA-256 해시만
  저장 (원문은 발급 시 1회 표시). `owner_user_id` NULL=관리자 발급 시스템
  키(공동 관리, `/system/api-keys`), 값=그 사용자 귀속 개인 API Key(확장
  토큰, 본인이 `/settings/api-keys` 에서 발급, 권한은 _api_auth 가 소유자
  현재 역할로 매 요청 재평가)
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

## 코딩 컨벤션

- 타입 힌트 필수, docstring은 한국어로 간결하게
- 외부 입력(URL, 파일 경로)은 항상 검증/정규화 후 사용 — path traversal 주의
- 네트워크 요청에는 타임아웃 필수 (페이지 로드 기본 30s)
- 새 기능 = 해당 테스트 추가. 네트워크 의존 테스트는 로컬 fixture HTML 사용
- 커밋은 기능 단위로 작게
- **브랜치 흐름 = gitflow.** 기능 PR 은 `develop` 을 베이스로 머지한다
  (main 직행 금지). `develop` 에 푸시되면 `docker.yml` 이 `:develop` 이미지를
  빌드·스모크 테스트한 뒤, 통과하면 `develop → main` 릴리스 PR 을 자동
  생성/갱신하고 변경 diff 로 `release:*` 라벨을 자동 부여한다 (코드 변경=minor,
  docs/tests/.md/.github 만=patch, 커밋에 "BREAKING"·"호환 깨" 있으면 major).
  이 릴리스 PR 을 사람이 검토 후 머지하면 `release.yml` 이 라벨로 버전을
  결정해 pyproject.toml·uv.lock 갱신 + `vX.Y.Z` 태그 + GitHub Release 를
  자동 등록하고, develop 를 릴리스 커밋으로 FF 동기화한다. 자동 라벨이
  맞지 않으면 머지 전에 `gh pr edit <번호> --add-label release:major` 로
  직접 바꾼다. 버전 출처는 설치 메타데이터(`chunchugwan.__version__` /
  `wccg --version`). 릴리스 PR(develop→main)은 develop 가 main 의 조상으로
  남도록 **merge 커밋으로 머지**한다 (squash 면 FF 동기화가 깨진다).
  도커 이미지 태그: `:latest`·`:main`(main), `:develop`(develop),
  `:vX.Y.Z`(릴리스 태그)

## 대시보드 디자인 방향

- 화면 21개 — 현황(`/`), 목록(`/archives` — 사이트(서브도메인) 단위),
  사이트 상세(`/sites/{id}` — 소속 페이지·문서·크롤 회차·스케줄·사이트 삭제),
  사이트 로그인 자격증명(`/sites/{id}/credentials` — 관리자 전용),
  문서(`/documents` — 문서 파일 통합 목록),
  검색(`/search` — 본문·문서 전문 검색, viewer 이상), 새 아카이빙(`/archive/new`),
  사이트 아카이브 진행(`/crawls/{id}` — 크롤 회차 상세), 스케줄(`/schedules`),
  타임라인, 스냅샷 뷰어, diff 뷰어, 아카이빙 로그(`/logs` — viewer 이상),
  시스템 로그(`/system/logs` — 관리자 전용), 시스템, 사용자, API 키,
  개인 API Key(`/settings/api-keys` — 본인 확장 토큰 발급·폐기),
  내 아카이브(`/settings/archives` — 본인이 요청한 아카이빙 이력),
  사람 확인 필요(`/archive/needs-human` — 관리자 전용, `WCCG_LIVE_CHALLENGE` 켜짐 시)·
  라이브 챌린지 처리(`/archive/jobs/{id}/live` — 관리자, 스크린샷 보고 직접 클릭/입력).
  권한이 없는 메뉴는 헤더에 표시하지 않는다 (`templating._auth_context` 의
  노출 플래그). 로그(아카이빙·시스템)·관리자(사용자·시스템) 메뉴와 개인설정
  (우측 이메일/표시이름 → 계정·개인 API Key·내 아카이브·로그아웃)은 헤더에서
  같은 `<details>` 드롭다운(`.nav-group`)으로 묶는다 (base.html — 넓은 화면은
  겹침 패널, 좁은 화면은 햄버거 안 아코디언). 화면별 라우트·권한·세부 동작은
  `docs/DASHBOARD.md` 참조.
- 도구다운 밀도 있는 UI. 모노스페이스로 해시/시각 표기, 변경 상태는 색 뱃지
  (변경=amber, 동일=gray, 신규=green). 과한 장식/그라데이션 금지.
- 다국어(ko/en): `web/i18n.py` — 한국어 원문이 메시지 키(gettext msgid 방식),
  언어별 "원문 → 번역" dict 로 확장. 로케일은 `wccg_lang` 쿠키(헤더의 언어
  선택, `POST /lang`) → Accept-Language → ko. 템플릿은 `_("…")`, 라우트는
  `i18n.t(request, "…")`. 새 UI 문자열 추가 시 en 카탈로그도 채울 것 —
  템플릿 리터럴 키 누락은 `tests/test_i18n.py` 가 검사한다. CLI 는 한국어 유지.
- diff 뷰: 텍스트 side-by-side + 스크린샷 비교(슬라이더 또는 토글)

## 구현 로드맵

M1~M8, A1~A11 전 마일스톤 완료 — 상세 내역은 `docs/ROADMAP.md` 참조.
새 마일스톤은 진행 중인 항목만 여기에 두고, 완료되면 ROADMAP.md 로 내린다.
각 마일스톤 완료 시: 테스트 통과 확인 → 체크박스 갱신 → 커밋.
