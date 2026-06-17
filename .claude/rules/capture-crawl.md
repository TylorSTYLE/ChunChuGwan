---
description: 캡처·크롤·스케줄·네트워크 게이트(원칙 7)·인증서·라이브 챌린지. 캡처/파이프라인/크롤러/스케줄러 등을 만질 때.
paths:
  - "chunchugwan/capture.py"
  - "chunchugwan/pipeline.py"
  - "chunchugwan/crawler.py"
  - "chunchugwan/extract.py"
  - "chunchugwan/scheduler.py"
  - "chunchugwan/archive_worker.py"
  - "chunchugwan/worker.py"
  - "chunchugwan/certs.py"
  - "chunchugwan/netcheck.py"
  - "chunchugwan/browser_engine.py"
  - "chunchugwan/trackers.py"
  - "chunchugwan/live_challenge.py"
  - "docs/CRAWLING.md"
---

# 캡처 · 크롤 · 스케줄

> 공통 아키텍처 원칙(CLAUDE.md)을 구현하는 도메인이다 — **원칙 3**(콘텐츠 해시 기반 중복 제거,
> 정규화 텍스트 SHA-256 동일 시 `checks` 기록만), **원칙 4**(비교는 `extract.py` 정규화 텍스트
> 기준 — 타임스탬프·CSRF·광고 제거 후 해시/diff)이 여기서 동작한다.
> `pages` 의 캡처 폴백 사슬·https 승격·`client_captured` 동작 정의는 `.claude/rules/database.md` 참조.

## 사설 IP·루프백 게이트 (아키텍처 원칙 7)

아카이빙 대상 호스트의 네트워크 대역은
`netcheck.py` 가 판정한다(IP 리터럴·localhost 는 즉시, 호스트명은 서버
리졸버 해석 + TTL 캐시, 해석 실패는 공인 취급). 루프백은 항상 거부 —
대시보드 자신이 아카이브로 새는 것을 막는다. 사설 대역(RFC1918·링크
로컬·ULA)은 시스템 설정의 로컬 네트워크 태그(`network_tags`, id 는
GUID)를 지정해야 한다. 강제는 코어(pipeline `_resolve_network_tag` —
캡처 전 + 리다이렉트 최종 URL 재검증, crawler `_check_network_tag`)가
하고, 웹 폼·REST API 는 같은 정책을 동기 검증으로 미리 보여준다.
공개 주소에 태그를 넘기면 무시된다.

## 이전(마이그레이션) 모드 게이트

춘추관 간 데이터 이전 중에는 소스의 **모든 스크래핑·스케줄·크롤이 중단**된다.
코어가 `db.migration_mode_enabled(conn)` 를 매 진입부에서 검사한다 —
`archive_worker.process_next`·`scheduler.run_due`·`crawler.process_next`/
`run_due_schedules` 가 즉시 빈 결과로 no-op. 등록 지점(웹 새/재아카이빙·크롤
재실행·실패 재시도, REST `_require_archive`, CLI `add`/`crawl add`/`*run`)도
이전 모드면 409/안내로 막는다(`web.app._require_not_migrating`,
`cli._warn_if_migrating`). 모드·토큰 저장은 `db.set_migration_mode`, 흐름 전체는
`chunchugwan/migration.py` 와 `.claude/rules/authentication.md` 참조.

## 관련 DB 테이블

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
- `archive_jobs` — 단발(즉시) 아카이빙 작업 큐. 대시보드 새/재아카이빙·실패
  재시도·REST API·CLI `add` 가 캡처를 직접 실행하지 않고 이 큐에 넣으면,
  worker(또는 serve 단일 프로세스)의 `archive_worker` 가 소비해 `pipeline.archive_url`
  을 호출한다 — 캡처 실행 지점을 한 프로세스로 통일해 스텔스 캡처 설정
  (`WCCG_CAPTURE_*`)이 그 프로세스에만 있으면 되게 한다. `crawl_pages` 와 같은
  'DB 큐 + 원자적 클레임 + 폴링' 패턴(pending/in_progress, attempts·next_attempt_at·
  claimed_at·error). 같은 URL 의 활성 작업은 부분 UNIQUE 로 하나만(중복 enqueue
  무시). 회차·범위·링크추적·페이싱이 없어 단순하며, 완료/최종실패 행은 삭제하고
  결과·오류는 `archive_logs` 가 보존한다 (소비 시 job_id 를 로그에 남겨 확장이
  `GET /api/v1/archive/status` 로 완료/실패/사람확인 결과를 추적). interval 이 실리면 소비자가 캡처 후
  주기를 `schedules` 에 등록한다. `wccg worker`/serve(`WCCG_SCHEDULER`)/`wccg archive
  run` 이 소비한다. 진행 상태(`/archive/active` 폴링)의 데이터 소스.
  `WCCG_LIVE_CHALLENGE=on` 이면 자동으로 못 푼 인터랙티브 챌린지를 사람이
  대시보드에서 직접 푸는 라이브 세션 컬럼(`needs_human_at`·`live_token`·
  `live_owner_id`·`live_cancel`·`live_force_solve`·`live_viewport_w/h`)을 쓴다 —
  worker 가 살아있는
  page 를 붙든 채(큐 진행 멈춤) 화면(스크린샷 파일 `cache/live/`)·입력
  (`live_commands` 테이블)으로 대시보드와 조율한다 (live_challenge.py, 원칙 7
  의 사설/루프백 가드를 라이브 매 폴링에 적용). `live_force_solve` 는 사람이
  로봇 확인을 풀었는데 잔여 마커로 자동 판정(challenge_reason)이 안 풀릴 때
  '사람 확인 완료' 로 현재 페이지를 강제 채택하게 하는 플래그. 데이터센터 IP
  평판으로는 사람이 눌러도 통과가 보장되지 않는 최후 수단
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
  실패 재시도 대기·횟수는 `settings` 의 `crawl_retry_backoff_seconds` 기준.
  `crawls.requested_by` 는 요청자(web/확장 토큰) — 확장이 크롤 완료/취소를
  `GET /api/v1/archive/status` 로 추적하는 결과 알림 귀속 (페이지 단위 로그는
  source=crawl·requested_by=NULL 이라 별도)
- `crawl_schedules` — 사이트 전체 아카이브의 주기적 재실행 (시작 URL 별
  크롤 옵션 + 주기 1시간~1개월·`run_at_time`). 기한이 되면 같은 옵션으로
  새 크롤을 등록(source=schedule)하되, 같은 URL 의 크롤이 진행 중이면 끝날
  때까지 미룬다. serve 크롤러 스레드·`wccg worker`·`wccg schedule run`/
  `crawl run` 이 실행하며 next_run_at 갱신은 원자적 클레임이라 동시 실행에 안전

> 크롤 기본값·재시도 백오프·캡처(모바일 스크린샷) 등 런타임 설정은
> `.claude/rules/database.md` 의 `settings` 항목 참조.
