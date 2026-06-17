# 성능 최적화 분석 및 실행 계획

> 작성: 2026-06-17 · 대상: 전체 코드베이스(~23,000 LOC, 42개 모듈) · 상태: **계획(미구현)**
>
> 코드 정적 분석 기반 제안 모음이다. 실측 타이밍은 포함하지 않는다(아래 "측정·검증" 참조).
> 각 Phase 를 독립 PR(기능 단위, gitflow `develop` 베이스)로 진행하며, 완료 시 체크박스를 갱신한다.

## 검증으로 확정한 전제

- DB 커넥션은 요청·작업마다 **새로 열고 닫는다**(풀링 없음). `journal_mode=WAL`(영구)·
  `synchronous=NORMAL`·`foreign_keys=ON`만 설정되고 **`cache_size`·`mmap_size`·`temp_store`
  는 미설정**이다 (`chunchugwan/db.py:716`).
- `snapshots` 테이블에 용량(`bytes`) 컬럼이 없다 — 용량은 매번 파일시스템 stat 로 계산한다.
  단, `resources_indexed`/`css_externalized`/`search_indexed` 플래그 + `_migrate` 백필 패턴이
  이미 존재해 같은 방식으로 컬럼을 추가할 수 있다 (`chunchugwan/db.py:92`).
- 인덱스 부재 확정(`CREATE INDEX` 전수 대조): `crawl_pages.url`(단독)·`crawls.start_url`·
  `crawl_pages.snapshot_id`·`archive_logs.snapshot_id`·`archive_logs.started_at`(단독)·
  `sessions.expires_at`·`*.credential_id`.
- `idx_snapshots_page(page_id, taken_at)` 는 존재한다(검색 `--latest` 를 일부만 지원 —
  `id` 동률 정렬은 인덱스 밖).

## 핵심 결론 — 가장 큰 레버

분석 전체를 관통하는 지배적 비효율은 **"매 페이지 로드마다 아카이브 파일시스템을 통째로
stat/순회"** 다. DB 와 웹 영역 분석이 독립적으로 같은 결론에 도달했다.

| # | 테마 | 효과 | 비용 |
|---|---|---|---|
| 1 | **`snapshots.bytes` 비정규화** — 현황·목록·사이트상세·타임라인·로그의 파일시스템 N+1 일괄 제거 | ★★★ | 중간(마이그레이션) |
| 2 | **SQLite PRAGMA + 인덱스 묶음** — 모든 쿼리에 깔리는 고정 비용 절감 | ★★★ | 작음 |
| 3 | **요청당 반복 작업 제거** — count_users·커넥션 재사용·권한 1회 계산 | ★★ | 작음~중간 |

## 우선순위 마스터 표

| 순위 | 항목 | 위치 | 심각도 | 난이도 | Phase |
|---|---|---|---|---|---|
| 1 | SQLite PRAGMA (`cache_size`/`mmap_size`/`temp_store`) | `db.py:716` | 높음 | 작음 | 0 |
| 2 | `snapshots.bytes` 비정규화 (stat N+1 제거) | `db.py` + web 5개 라우트 | 높음 | 중간 | 2 |
| 3 | 인덱스 묶음 추가 | `db.py` `_migrate` | 중간 | 작음 | 1 |
| 4 | `count_users` 래치 (매 요청 COUNT 제거) | `web/app.py:171` | 높음 | 작음 | 0 |
| 5 | 브라우저 유휴 close/재기동 스래싱 | `archive_worker.py:206`, `crawler.py:631` | 높음 | 작음 | 0 |
| 6 | 요청 단위 커넥션 재사용 + `_require_archive` 통합 | `db.py:696`, `api_routes.py:94` | 높음 | 중간 | 4 |
| 7 | 검색 SELECT 가 본문 전문 로드 (스니펫 260자만 사용) | `db.py:1775` | 높음 | 중간 | 5 |
| 8 | 문서 추출 전체 메모리 적재 + char 상한 사후 적용 | `doctext.py:37,54,157` | 높음 | 작음~중간 | 5 |
| 9 | 제목(title) 비정규화 (meta.json 반복 파싱 제거) | `web/app.py:397` | 중간 | 중간 | 3 |
| 10 | `_api_auth` 권한 중복계산 + `touch_api_key` 스로틀 | `api_routes.py:56-81` | 중간 | 작음 | 4 |
| 11 | Jinja `auto_reload=False`/`cache_size` | `web/templating.py:133` | 중간 | 작음 | 0 |
| 12 | `_auth_context` 권한 9회 재계산 + needs_human 별도 커넥션 | `web/templating.py:14` | 중간 | 중간 | 4 |
| 13 | 백업이 이미 압축된 CAS 를 gzip 재압축 | `backup.py:148` | 높음 | 중간 | 6 |
| 14 | 캡처 전 `get_page` 3중 조회 + `page.content()`/`title()` 중복 | `pipeline.py:286`, `capture.py:638` | 중간 | 작음 | 0 |
| 15 | `latest_only` 상관 서브쿼리 (검색마다 ×2) | `db.py:1784` | 높음 | 중간 | 5 |
| 16 | `extract.normalize` 도메인 룰 정규식 재컴파일 | `extract.py:213` | 중간 | 작음 | 0 |
| 17 | CLI 최상단 heavy import (PIL·capture 그래프) | `cli.py:13` | 중간 | 작음 | 0 |
| 18 | optimize CAS 전체 2회 스캔 + style/backfill 중복 해제 | `optimize.py:123` | 중간 | 중간 | 6 |
| 19 | CSS 존재 확인 전에 gzip 압축 완료 (재아카이빙 낭비) | `resources.py:145` | 중간 | 작음 | 0 |
| 20 | ingest 멀티파트 전체 메모리 적재 (OOM 위험) | `api_routes.py:666`, `ingest.py:217` | 중간 | 중간~큼 | 7 |
| 21 | `archive_disk_usage`/`/documents` legacy 전체 순회 | `storage.py:286`, `web/app.py:1569` | 낮음 | 작음 | 2 |
| 22 | `snapshot_file` 캐시 헤더 부재 (불변인데 매번 재전송) | `web/app.py:1478` | 낮음 | 작음 | 0 |
| 23 | 문서 직렬 다운로드 병렬화 | `documents.py:264` | 낮음 | 중간 | 7 |
| 24 | 사이트 삭제 `IN()` 대량 파라미터 + N+1 | `deletion.py:104` | 낮음 | 중간 | 7 |

---

## 영역별 상세

### 테마 A — 파일시스템 stat 폭발 (최대 임팩트)

`/`(현황), `/archives`(목록), `/sites/{id}`(상세), `/timeline`, `/logs` 가 용량·제목을 보여주려고
**표시 항목 수와 무관하게 전체 스냅샷을 순회**한다. 페이지네이션이 무력화된다.

- `dashboard()`(`web/app.py:1100`)·`index()`(`web/app.py:411`)가 `db.list_snapshot_dirs(conn)` 로
  **LIMIT 없이 전체 스냅샷**을 가져온 뒤, 스냅샷마다 `_snapshot_dir_size()` → `storage.snapshot_files()`
  가 **스냅샷당 ~10회 `is_file()`/`stat()` + `files/` iterdir** 를 수행한다. 스냅샷 N개 →
  수십×N 회 syscall/요청.
- `archive_disk_usage()` 는 `sites/`·`resources/`·`documents/` 를 각각 `rglob("*")` 로 전체 트리
  재귀 순회한다 (`storage.py:286`). `_tree_bytes` 중복 정의도 `resources.py:507` 에 있다.
- 제목은 사이트마다 최신 5개 `meta.json` 을 `read_text`+`json.loads` 한다 (`web/app.py:397`).

**제안 (불변성 원칙 2 와 정합 — 스냅샷은 한 번 계산하면 영구 유효):**
1. `snapshots.bytes INTEGER` 컬럼 추가. 캡처/compact 시 1회 기록, 집계는 SQL `SUM(bytes)` 로 대체.
   `list_sites_overview` 의 기존 상관 서브쿼리에 합류 가능. → 순위 2
2. 제목을 `pages` 또는 `sites` 에 비정규화 저장 → meta.json 파싱 제거. → 순위 9
3. `archive_disk_usage` 표시값은 짧은 TTL 캐시(파생값이라 부정확 허용). → 순위 21

> 이 하나로 높음 3건(현황·목록·상세)이 동시에 해소된다. 단일 최대 개선.

### 테마 B — SQLite 설정·인덱스 (가성비 최고)

**PRAGMA (순위 1, `db.py:716` 부근)** — 런타임 PRAGMA 라 커넥션마다 적용(journal_mode 처럼 영구
저장 아님), WAL+NORMAL 과 충돌 없음:

```python
conn.execute("PRAGMA cache_size = -16000")    # ~16MB 페이지 캐시 (현재 기본 ~2MB)
conn.execute("PRAGMA mmap_size = 268435456")  # 256MB — read() syscall 대신 메모리 매핑
conn.execute("PRAGMA temp_store = MEMORY")     # ORDER BY/GROUP BY 임시정렬을 메모리에서
```

값은 배포 환경(도커 메모리 한도)에 맞춰 조정한다.

**인덱스 묶음 (순위 3, `_migrate` 에 멱등 추가):**

```sql
-- 아카이빙 핫패스 (pipeline 이 아카이빙마다 crawl_pages.url 전체 스캔)
CREATE INDEX IF NOT EXISTS idx_crawl_pages_url       ON crawl_pages(url);
CREATE INDEX IF NOT EXISTS idx_crawls_start_url      ON crawls(start_url);
-- 삭제 경로 (현재 전체 스캔)
CREATE INDEX IF NOT EXISTS idx_crawl_pages_snapshot  ON crawl_pages(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_archive_logs_snapshot ON archive_logs(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_pages_credential          ON pages(credential_id);
CREATE INDEX IF NOT EXISTS idx_crawls_credential         ON crawls(credential_id);
CREATE INDEX IF NOT EXISTS idx_crawl_schedules_credential ON crawl_schedules(credential_id);
-- 로그 목록 무필터 정렬 / 세션 만료 정리
CREATE INDEX IF NOT EXISTS idx_archive_logs_started  ON archive_logs(started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_expires      ON sessions(expires_at);
```

`crawl_pages`·`archive_logs` 는 데이터 증가에 가장 크게 자라는 테이블이라, 삭제·로그 화면이
행 수에 비례해 느려지는 것을 막는다.

> PRAGMA `cache_size`/`mmap_size` 는 커넥션마다 콜드 시작이므로, 테마 C 의 커넥션 재사용과
> 결합할 때 효과가 배가된다.

### 테마 C — 요청당 반복 작업

- **`count_users` 래치 (순위 4):** `auth_gate` 가 매 요청 `SELECT COUNT(*) FROM users == 0` 으로
  최초 구동을 판정한다 (`web/app.py:171`). 사용자가 한 명이라도 생기면 영원히 0 이 아니다 →
  프로세스 전역 플래그로 1회 래치(복원으로 DB 가 비는 경로만 예외 보존). 즉효.
- **요청 단위 커넥션 재사용 (순위 6):** 한 API 요청이 커넥션을 3~5회, 크롤 페이지 1건이 5~7회
  열고 닫는다. `request.state` 에 커넥션 1개를 매달아 미들웨어·의존성·핸들러가 공유한다.
  `_require_archive` 의 별도 커넥션 + `migration_mode` 매번 조회(`api_routes.py:94`)도 함께 해소.
  *위험: 트랜잭션 경계(현재 컨텍스트 종료 시 commit) 정리 필요.*
- **권한 1회 계산 (순위 10, 12):** `_api_auth` 가 `effective_permissions(owner)` 를 2회 호출
  (JSON 오버라이드 파싱 2회), `_auth_context` 가 HTML 렌더마다 권한 9회 재계산 + 관리자면
  needs_human 별도 커넥션. → 요청당 1회 계산해 `request.state` 공유.
- **`touch_api_key` 스로틀 (순위 10):** 읽기 API(폴링 포함)도 매번 `last_used_at` UPDATE(쓰기
  트랜잭션). N초 이내면 생략하는 조건부 UPDATE.

### 테마 D — 캡처/워커 효율

- **브라우저 유휴 스래싱 (순위 5) — 임팩트 대비 가장 쉬움:** run_loop 가 큐가 비면 즉시
  `session.close()`(`archive_worker.py:206`, `crawler.py:631`), 폴링은 2초. 작업이 산발적이면
  2초마다 chromium 재기동. `BrowserSession` 재사용 설계가 무력화된다. → `last_active` 타이머로
  유휴 30~60초 후에만 close(한 줄 가드).
- **캡처 전 중복 조회 (순위 14):** `_archive_url` 가 같은 URL `get_page` 를 3번
  (`pipeline.py:286,304,319`) 각각 별도 커넥션으로 조회. `page.title()` 2회
  (`capture.py:645,694`), `page.content()` 도 raw + 인라인 후 재직렬화. → 조회/직렬화 1회로
  통합(title 중복은 확실히 제거 가능).
- **정규식 재컴파일 (순위 16):** `extract.normalize` 가 도메인 룰 패턴을 호출마다 `re.compile`
  (`extract.py:213`). → 룰 로딩 시 1회 컴파일 캐시.
- **CLI heavy import (순위 17):** `wccg list`/`search` 도 `crawler`·`archive_worker`·`differ`(→PIL)
  를 전부 로드 (`cli.py:13`). → 명령 함수 내 지연 import. cron 으로 자주 도는 배포에서 누적.

### 테마 E — 검색/색인 메모리

- **검색 본문 전문 로드 (순위 7):** 검색 SELECT 가 `snapshot_fts.content`(content.md + 첨부 문서
  본문 최대 2MB/문서)를 행마다 전부 가져오는데 스니펫은 260자만 사용 (`db.py:1775`,
  `searchindex.py:298`). → FTS5 내장 `snippet()` 로 DB 가 잘라 주게 변경.
  *위험: trigram 토크나이저에서 위치 정합성 테스트 필요.*
- **문서 추출 메모리 (순위 8):** `doctext` 가 PDF 전 페이지/zip 멤버를 통째로 메모리에 모은 뒤
  `SEARCH_DOC_TEXT_MAX_CHARS`(2MB) 자르기를 사후 적용 (`doctext.py:157`). → 추출 루프에 누적 길이
  가드로 조기 중단. 위험 낮음(어차피 잘리던 값).
- **`latest_only` 상관 서브쿼리 (순위 15):** `--latest` 가 후보 행마다 "그 페이지 최신 스냅샷"
  서브쿼리 실행, count 용까지 ×2 (`db.py:1784`). → 윈도우 함수 또는 `pages.latest_snapshot_id`
  비정규화.

### 테마 F — 백업/compact

- **백업 CAS 재압축 (순위 13):** `tarfile.open("w:gz")` 가 이미 압축된 `resources/`(gzip·WebP)·
  `documents/`(PDF·zip)를 다시 deflate 한다 (`backup.py:148`). 압축률 ≈0, CPU 만 소모. → 비압축
  tar + DB 만 별도 압축, 또는 `compresslevel=1`. 출력은 여전히 `.tar.gz` 라 restore 호환 유지.
  큰 아카이브 백업 시간을 좌우.
- **CSS 압축 순서 (순위 19):** `_store_css` 가 존재 확인 전에 `gzip(level=9)` 완료
  (`resources.py:145`). 재아카이빙 시 안 바뀐 CSS 를 매번 재압축. → 해시로 존재 확인 후 신규일
  때만 압축.
- **optimize 2회 스캔 (순위 18):** 절약량 계산용 CAS 전체 `rglob` 2회 + style 추출/참조 백필이
  같은 `page.html.gz` 를 각각 해제 (`optimize.py:123,145`). → 추출 자원만 stat, 두 패스 통합.

---

## 단계별 실행 계획

각 Phase 는 독립 PR(기능 단위 작은 커밋, gitflow `develop` 베이스). 위→아래 = 가성비·안전 순.

### Phase 0 — 즉효·무위험 (스키마 변경 없음)

작은 변경만 모음. 회귀 위험 거의 없음. 먼저 체감 개선.

- [x] PRAGMA 3종 추가 (순위 1)
- [x] `count_users` 래치 (순위 4)
- [x] 브라우저 유휴 grace period (순위 5)
- [x] Jinja `auto_reload=False`/`cache_size=-1` (순위 11)
- [x] `extract.normalize` 정규식 캐시 (순위 16)
- [x] CLI 지연 import (순위 17)
- [x] `snapshot_file` immutable 캐시 헤더 (순위 22)
- [x] CSS 압축-후-확인 → 확인-후-압축 (순위 19)
- [x] 캡처 `get_page`/`title` 중복 제거 (순위 14)

검증 게이트: `uv run pytest` 전체 통과 + `wccg serve` 로 현황/목록/검색 수동 확인.

> **완료 (perf/phase-0).** `cache_size`/`mmap_size`/`temp_store` PRAGMA 를 매 커넥션에
> 적용, 최초 구동 판정을 프로세스 전역 래치(`db.first_run_needed`, DB 교체 시 자동 재평가)로
> 바꿔 요청당 `COUNT(*)` 제거, archive/crawl 워커가 유휴 `BROWSER_IDLE_CLOSE_SECONDS`(60s)
> 를 넘겨야 브라우저를 내리도록 변경, Jinja `auto_reload` 끄고 템플릿 캐시 무제한, 도메인 룰
> 정규식 컴파일 캐시(`_compile_drop_patterns`), CLI 의 capture(playwright)·PIL·lxml 그래프를
> 지연 import 로 전환(list/search/add 콜드 스타트 단축), `/snapshot/{id}/file/{name}` 에
> `Cache-Control: immutable`, `_store_css` 가 존재 확인 후에만 gzip, 캡처 전 `get_page` 3중
> 조회·커넥션을 1회로 통합 + `page.title()` 중복 제거. 회귀 테스트 추가
> (`test_db.py` PRAGMA·first-run 래치, `test_resources.py` CSS 재압축 회피).

### Phase 1 — 인덱스 (멱등 마이그레이션)

- [x] 인덱스 묶음 추가 (순위 3) — `_migrate` 에 `CREATE INDEX IF NOT EXISTS`

검증: `test_migration.py` 에 신규 인덱스 존재 단언 추가, `EXPLAIN QUERY PLAN` 으로 해당 쿼리가
인덱스를 타는지 확인.

> **완료 (perf/phase-1).** `_migrate` 에 9개 인덱스를 멱등 추가:
> `idx_crawl_pages_url`·`idx_crawls_start_url`(아카이빙 핫패스),
> `idx_crawl_pages_snapshot`·`idx_archive_logs_snapshot`(삭제 시 참조 해제),
> `idx_pages_credential`·`idx_crawls_credential`·`idx_crawl_schedules_credential`
> (자격증명 삭제 NULL 처리), `idx_archive_logs_started`(로그 무필터 정렬)·
> `idx_sessions_expires`(만료 세션 정리). 인덱스 존재·`EXPLAIN QUERY PLAN` 채택을
> `test_db.py` 로 가드. 신규/기존 DB 모두 적용(멱등). 검증은 schema 마이그레이션을
> 다루는 `test_db.py` 에 둠(`test_migration.py` 는 네트워크 이전 기능 전용).

### Phase 2 — `snapshots.bytes` 비정규화 ⭐ 최대 임팩트

- [x] 컬럼 추가 + `_migrate` 백필(`resources_indexed` 패턴 그대로)
- [x] 쓰기 경로: `storage`/캡처 저장 + `optimize`(compact 가 형태 바꾸면 bytes 갱신)
- [x] 읽기 경로: 현황·목록·사이트상세의 `_snapshot_dir_size` 파일시스템 N+1 제거
      (`bytes` 컬럼 합산). 타임라인·로그는 용량을 계산하지 않아 대상 아님.
- [x] `archive_disk_usage` 표시값 TTL 캐시 (순위 21)

위험·검증: `export`/`import`/`backup`/`restore`/`compact` 후 bytes 일관성 테스트가 핵심.
백필 누락 스냅샷은 0 폴백 후 lazy 보정.

> **완료 (perf/phase-2).** `snapshots.bytes` 컬럼 추가(SCHEMA + `_migrate` ALTER,
> 컬럼 최초 추가 시 파일시스템에서 1회 백필 — `backfill_snapshot_bytes`). 쓰기 경로:
> 캡처(pipeline 페이지·문서 스냅샷)·확장 적재(ingest)가 저장 시점에 `bytes` 기록,
> `optimize.run()` 이 압축 변환·스타일 추출로 형태가 바뀌면 재계산, import 는 옮긴
> 실제 파일 기준으로 권위적 재계산(구버전 export 호환). 읽기 경로: 현황(`dashboard`)·
> 목록(`index`)·사이트상세의 스냅샷당 `stat`/`iterdir` N+1 을 `bytes` 합산으로 대체
> (`_snapshot_dir_size`/`list_snapshot_dirs`·`list_site_snapshot_dirs` 에 `bytes` 추가,
> `_snapshot_dir_size` 제거). 단일 계산 지점 `storage.snapshot_dir_bytes`.
> `archive_disk_usage` 는 루트별 30초 TTL 캐시(표시 전용 파생값). 일관성 테스트:
> 백필(`test_db.py`)·compact 갱신(`test_optimize.py`)·export/import(`test_backup.py`)
> + 읽기 경로 표시값(`test_web.py`). 전체 1184 통과.
>
> 추가 SQL 집계(`SUM(bytes)`)로 행 로드 자체를 없애는 것은 목록·상세가 같은 행으로
> 제목(meta.json)도 읽기 때문에 **Phase 3(제목 비정규화) 이후**에 자연스럽다 — 지금은
> 지배적 비용인 파일시스템 N+1 만 제거했다.

### Phase 3 — 제목 비정규화

- [x] 스냅샷 title 비정규화, 목록·상세의 meta.json 파싱 제거 (순위 9)

> **완료 (perf/phase-3).** title 은 `pages`/`sites` 가 아니라 **`snapshots.title`** 에
> 저장했다 — title 은 캡처 시점의 스냅샷 고유값(meta.json 사본)이라 여기가 자연스럽고,
> 스냅샷 삭제 시 사이트/페이지 제목을 재계산할 필요가 없으며(`_site_title` 이 남은
> 스냅샷 중 최신 제목을 고름), 목록·상세 읽기 경로가 이미 로드하는 스냅샷 행
> (`list_snapshot_dirs`·`list_site_snapshot_dirs`)에서 제목을 공짜로 얻는다.
> SCHEMA + `_migrate` ALTER(최초 추가 시 meta.json 1회 백필 — `backfill_snapshot_titles`),
> 쓰기 경로(pipeline·ingest 저장 시 기록, export/import 라운드트립), 읽기 경로
> (`_site_title` 이 `row["title"]` 사용 — 파일 IO·lookback 한도 제거, `_snapshot_title`
> 삭제). 스냅샷 뷰어는 문서 목록 때문에 meta.json 을 어차피 읽어 그대로 둔다.
> 테스트: 백필(`test_db.py`)·export/import 라운드트립(`test_backup.py`)·목록/상세 표시
> 와 폴백(`test_web.py`). 전체 1186 통과.
>
> 이로써 Phase 2 에서 보류한 "행 로드 없는 SQL 집계"의 선행 조건(제목 비정규화)이
> 풀렸다 — 다만 현 구현도 파일시스템·meta.json 접근은 0 이라 추가 SQL 집계는 큰
> 아카이브에서 필요해지면 별도로 진행한다.

### Phase 4 — 요청 단위 커넥션 + 권한 1회 계산

- [ ] `request.state` 커넥션 공유, `_require_archive`/`migration_mode` 통합 (순위 6) — **보류**
- [x] `_api_auth` 권한 중복 제거 + `touch_api_key` 스로틀 (순위 10)
- [x] `_auth_context` 권한 캐시 (순위 12, needs_human 커넥션 재사용은 순위 6 의존이라 보류)

위험: 트랜잭션 경계 변경 → 쓰기 핸들러 커밋 시점 점검. 인증/권한 테스트 전수 통과 필수.

> **완료 (perf/phase-4) — 저위험 국소 최적화만.** 순위 10: `_api_auth` 가 owner 토큰
> 검증에서 `can_use_api_keys` + `token_permissions_for_user` 로 오버라이드 JSON 을 두 번
> 파싱하던 것을 `effective_permissions(owner)` 1회 계산 후 멤버십으로 파생. `touch_api_key`
> 는 `config.API_KEY_TOUCH_THROTTLE_SECONDS`(60s) 이내면 조건부 UPDATE 로 행을 건드리지
> 않아 읽기 API 폴링의 매 요청 쓰기 트랜잭션을 없앴다. 순위 12: `_auth_context` 가 렌더마다
> `can_*` 를 9회(= `effective_permissions` 9회) 호출하던 것을 `permissions.menu_flags(user)`
> 의 1회 계산으로 모두 파생. 테스트: touch 스로틀(`test_api_keys.py`), 권한 게이트 회귀는
> 기존 인증/권한 스위트 전수 통과(1187).
>
> **보류 — 순위 6(요청 단위 커넥션 공유) + 순위 12 의 needs_human 커넥션 재사용.**
> 한 요청에 커넥션 1개를 `request.state` 로 공유하는 것은 미들웨어·의존성·핸들러·템플릿
> 렌더(컨텍스트 프로세서)에 걸쳐 커넥션 수명과 **트랜잭션 경계(commit/rollback 시점)** 를
> 재정의하는 앱 전역 변경이다. 코어의 "쓰기는 `with db.connect()` 단위로 원자 커밋"
> (아키텍처 원칙 1) 전제를 흔들 수 있어, 트랜잭션 검토를 포함한 **독립 PR** 로 신중히 다루는
> 편이 안전하다. Phase 0 의 PRAGMA(커넥션마다 콜드 시작)와 결합 시 이득이 크다는 점은
> 유효하므로 후속으로 남긴다.

### Phase 5 — 검색/색인 메모리

- [x] FTS5 `snippet()` 도입 (순위 7) + doctext 조기 중단 (순위 8)
- [x] backfill 추출을 커넥션 밖에서 (순위 8 연계) — 이미 충족(per-snapshot 커밋)
- [ ] latest_only 윈도우 함수 (순위 15) — **보류**

검증: `test_search.py` 스니펫 출력 비교 + trigram 위치 정합성.

> **완료 (perf/phase-5) — 메모리 항목.** 순위 7: FTS 검색이 결과 행마다
> `snapshot_fts.content`(첨부 문서 본문 최대 2MB/문서) 전문을 Python 으로 가져와
> 260자 스니펫만 쓰던 것을, FTS5 내장 `snippet(snapshot_fts, 0, '', '', '…', 64)`
> 으로 DB 가 매치 주변(~60자)만 잘라 주게 변경(`_SEARCH_SELECT_FTS`). LIKE 폴백
> (1~2글자, MATCH 없어 snippet() 불가)은 종전대로 content 로 스니펫 생성. trigram
> 위치 정합성은 4000자 본문 깊은 매치 테스트로 가드. 순위 8: `doctext` 추출기에
> 누적 길이 가드를 넣어 `SEARCH_DOC_TEXT_MAX_CHARS` 에 닿으면 남은 PDF 페이지·zip
> 멤버를 읽지 않는다(전 페이지/멤버를 메모리에 모은 뒤 사후 절단하던 것 제거).
> 순위 8 연계(backfill 추출을 락 밖에서)는 `backfill_all` 이 이미 스냅샷마다 커밋해
> 추출이 쓰기 락 밖(WAL 읽기)에서 일어나므로 추가 작업 불필요.
>
> **보류 — 순위 15(latest_only 상관 서브쿼리).** 이는 메모리가 아니라 쿼리 형태
> 최적화다. 윈도우 함수 재작성은 FTS5 `bm25()`/`snippet()` 가 MATCH 컨텍스트를
> 요구해 서브쿼리·윈도우와의 호환이 불확실하고, `pages.latest_snapshot_id` 비정규화는
> **스냅샷 삭제 시 최신 재계산**(deletion.py) 이 필요해 위험이 있다. 현재 상관
> 서브쿼리는 `idx_snapshots_page(page_id, taken_at)` 를 타 매치된 후보 집합에만
> 적용되므로, 별도 PR 로 신중히 다룬다.

### Phase 6 — 백업/compact

- [x] 백업 재압축 회피 (순위 13)
- [ ] optimize 2회 스캔 제거/패스 통합 (순위 18) — **보류**

검증: `test_backup.py`·`test_optimize.py` — restore 라운드트립 동일성, compact 멱등성.

> **완료 (perf/phase-6) — 순위 13.** `create_backup`·`export_archive` 의
> `tarfile.open("w:gz")` 가 이미 압축된 자원(`page.html.gz`·`screenshot.webp`·CAS
> gzip/webp·PDF/zip)을 기본 레벨(9)로 재압축해 CPU 만 쓰고 거의 못 줄이던 것을
> `compresslevel=1`(`_BACKUP_COMPRESSLEVEL`)로 낮췄다. 출력은 여전히 표준 `.tar.gz`
> 라 `restore_backup`/`import_archive` 의 `r:gz` 와 호환되고, 잘 압축되는 DB·JSON 도
> 레벨 1 로 충분하다. 큰 아카이브(원격 NAS)의 백업 시간을 좌우한다. 기존
> 백업/복원·내보내기/가져오기 라운드트립 테스트가 restorability 를 가드.
>
> **보류 — 순위 18(optimize 2회 스캔/패스 통합).** ① 절약 바이트 계산용 CAS 전체
> 스캔은 "변경 전/후 총량"을 재는 것이라 한 패스로 줄이려면 추출 파일만 stat 해야
> 하는데, 공유(dedup)된 CSS 는 신규 growth 가 아니라 정확히 귀속하기 어렵다(표시값
> 부정확 위험). ② `_externalize_styles` 와 `_backfill_refs` 의 page.html.gz 중복
> 해제는 두 패스가 **의존적**(backfill 이 externalize 가 새로 단 `/resource/*.css`
> 참조를 잡아야 함)이고 서로 다른 스냅샷 집합을 돌며, 병합은 GC·자원 폴백의 근거인
> `snapshot_resources` 인덱스를 건드려 **정합성 위험**이 있다. compact 는 드물게 도는
> 1회성 정리라 이득 대비 위험이 커, 별도 PR 로 신중히 다룬다.

### Phase 7 — 선택 (여유 시)

- [ ] ingest 스트리밍 (순위 20, OOM 방어) — **보류**
- [ ] 문서 병렬 다운로드 (순위 23) — **보류** (대상 서버 부담 정합성)
- [x] 사이트 삭제 `IN()` 한도 (순위 24, 조인화는 보류)
- [x] `/documents` legacy 스캔 캐시 / OIDC discovery·JWKS 는 이미 모듈 캐시

> **완료 (perf/phase-7) — 안전·유효 항목만.** ① `/documents` 의 `legacy_pending`
> 배너가 매 로드마다 전체 스냅샷 디렉토리를 walk(구형 문서가 없으면 short-circuit 도
> 안 됨)하던 것을 루트별 60초 TTL 캐시(`_legacy_documents_pending`)로 감쌌다(표시 전용
> 힌트, compact 후 최대 TTL 지연 허용). ② 삭제 GC 후보 수집의 `IN(?,?,…)` 을
> `_SQL_VAR_CHUNK`(900) 로 끊어, 스냅샷 수천 개 사이트 삭제에서 SQLite 변수 한도
> (구버전 999) 초과('too many SQL variables')를 막는다 — 읽기 전용 union 이라 GC
> 의미는 동일(전역 dedup). OIDC discovery/JWKS 는 `oidc.py` 가 이미 모듈 캐시
> (`_discovery`·`_jwks_client`)라 요청마다 조회하지 않아 추가 작업 불필요.
>
> **보류.** 순위 20(ingest 멀티파트 스트리밍)은 업로드 본문 파싱을 바꾸는 큰 리팩터,
> 순위 23(문서 병렬 다운로드)은 "대상 서버 부담 방지"(크롤 delay) 원칙과의 정합성
> 검증이 필요해 둘 다 위험 대비 이득이 낮다. 사이트 삭제의 페이지별 `delete_page`
> 루프 조인화는 파괴적 경로 + prune/GC 의미를 건드려 별도로 둔다. live_challenge 폴링
> 정리는 라이브 세션 동작에 영향이 가능해 보류.

---

## 측정·검증 권고

- **벤치마크 선행:** 본 분석은 코드 근거 기반이며 실측 타이밍은 없다. 스냅샷 수천 개 규모의
  시드 DB 로 `/`·`/archives`·`/search` 응답시간을 Phase 2 전후 비교하면 효과가 가장 뚜렷하다.
- **`EXPLAIN QUERY PLAN`** 으로 인덱스 적용을 확인한다 (Phase 1).
- **불변 항목 확인 필요:** ① FTS5 `snippet()` 의 trigram 매치 위치, ② 비정규화 bytes 의
  export/import 일관성 — 둘 다 테스트로 가드한다.

## 비효율로 보였으나 실제로는 의도된/괜찮은 것 (오탐 방지)

- 크롤 페이지 순차 처리(같은 크롤)는 대상 서버 부담 방지가 의도 — 서로 다른 크롤은 워커
  다중 스레드로 병렬.
- 자원 인라인은 이미 JS 워커로 병렬화(`capture.py`).
- `certs`/`netcheck` 는 TTL 캐시로 같은 호스트 반복 핸드셰이크/DNS 를 막는다.
- 문서 다운로드는 이미 청크 스트리밍(`documents._save_stream`).
- `insert_snapshot_resources`/`insert_snapshot_documents`/`delete_fts_rows` 는 `executemany` 배치.
- `i18n.py` 카탈로그는 모듈 로드 시 1회 구성 + dict O(1) 조회 — 구조적 비효율 없음.
- Argon2id 파라미터·상수시간 비교·Fernet 은 보안상 의도된 비용 — **약화 금지**.
