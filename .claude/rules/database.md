---
description: DB 스키마 — 테이블 정의·마이그레이션·코어 테이블. db.py 를 만질 때 읽는다.
paths:
  - "chunchugwan/db.py"
---

# DB 스키마

`chunchugwan/db.py`의 `SCHEMA` 참조. 쓰기/조회 SQL 은 모두 db.py 가 소유한다(아키텍처 원칙 1).
기존 데이터 마이그레이션은 `db._migrate`(예: `_backfill_sites`)가 멱등 처리한다.

## 전체 테이블 인덱스 (상세 위치)

| 테이블 | 상세 |
|---|---|
| `sites`·`pages`·`snapshots`·`checks`·`settings`·`archive_logs`·`system_logs` | 이 파일 |
| `snapshot_resources`·`snapshot_documents` | `.claude/rules/storage.md` |
| `snapshot_fts` | `.claude/rules/search.md` |
| `network_tags`·`site_certificates`·`archive_jobs`·`live_commands`·`schedules`·`crawls`/`crawl_pages`·`crawl_schedules` | `.claude/rules/capture-crawl.md` |
| `users`/`identities`/`sessions`/`oidc_states`·`permission_groups`·`email_verifications`·`webauthn_credentials`·`site_credentials` | `.claude/rules/authentication.md` |
| `api_keys` | `.claude/rules/api-extension.md` |

## 코어·구조 테이블

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
  연결 실패에 한해). `client_captured` 는 확장(브라우저) 클라이언트 캡처로
  적재된 페이지 표식(`ingest.py` 가 1 로 설정) — 1 이면 서버가 그 URL 을 다시
  가져오지 않는다(스케줄·크롤·재시도·재아카이빙·enqueue 모두 차단:
  `pipeline._archive_url` 캡처 전 백스톱 + `db.enqueue_archive_job` 가드). 갱신은
  확장 재캡처(ingest)로만 (캡처 폴백·https 승격·client_captured 동작 상세는
  `.claude/rules/capture-crawl.md`·`.claude/rules/api-extension.md`)
- `snapshots` — 스냅샷 단위, `pages.id` FK, content_hash 보관.
  `search_indexed` 는 텍스트 검색 인덱스(snapshot_fts) 반영 여부 — 0 이면
  `wccg search reindex` 백필 대상 (resources_indexed 와 같은 패턴). `origin`
  은 캡처 출처 `server`(기본) | `extension`(브라우저 클라이언트 캡처), `incomplete`
  은 일부 자원·프레임·스크린샷 수집이 실패한 불완전 캡처 표식 — 둘 다 뷰어·
  타임라인 뱃지("브라우저 캡처"·"불완전")로 표시되고, 한쪽이라도 extension 이면
  diff 의 스크린샷 비교를 숨기고(해상도 의존) 본문 diff 에 경고를 단다. 캡처 환경
  (viewport·dpr·ua)은 meta.json `capture_env` 에 기록
- `checks` — 중복으로 저장 생략된 확인 기록
- `archive_logs` — 아카이브 실행 로그 (성공/실패, 단계별 소요시간 JSON,
  출처 cli/web/schedule/api/crawl). `requested_by` 는 직접 요청한 사용자
  (web·확장 토큰 소유자) — '내 아카이브'(`/settings/archives`)의 필터 기준.
  큐(archive_jobs.requested_by)를 거쳐 이어지며, cli/schedule/crawl 은 NULL.
  `job_id` 는 이 로그를 만든 archive_jobs.id (작업은 완료 시 삭제되므로 FK
  없는 상관 키) — 확장이 요청한 작업의 결과를 `GET /api/v1/archive/status`
  로 되찾는 데 쓴다
- `system_logs` — 앱 동작 로그 (`system_log.py` 의 logging 핸들러가
  chunchugwan 네임스페이스의 INFO 이상 레코드를 적재 — 레벨·로거·출처
  serve/worker/cli·트레이스백). 비차단 큐 + 쓰기 스레드, 보관 한도
  (`WCCG_SYSTEM_LOG_MAX_ROWS`) 초과분 자동 정리. 대시보드 `/system/logs`
  (관리자 전용)의 데이터 소스
- `settings` — 대시보드에서 변경하는 key-value 런타임 설정. 가입 설정
  (`signup_enabled` on/off 기본 on — off 면 `/signup` 차단 + 로그인 화면
  가입 링크 숨김(초대 가입은 허용), `signup_default_role`)과 이메일 본인 인증
  설정 (`email_verification_enabled` on/off 기본 off, `email_verification_ttl_minutes`
  — 코드 만료 분, SMTP 미설정이면 켜도 무시)과 사이트 아카이브
  설정 (`crawl_default_max_pages`/`crawl_default_max_depth`/
  `crawl_default_delay_seconds` — 새 크롤 옵션 기본값,
  `crawl_retry_backoff_seconds` — 실패 재시도 대기 쉼표 목록(초), 최대 시도
  = 길이 + 1, 진행 중 크롤에도 즉시 적용. 해석·검증은
  `crawler.crawl_defaults`/`retry_backoff`, 오염 시 config 기본값 폴백)과
  캡처 설정 (`mobile_screenshot_enabled` on/off 기본 off — 켜면 캡처가
  데스크탑 외에, 같은 URL 을 안드로이드 크롬으로 위장한 모바일 컨텍스트
  (UA·뷰포트 390×844·isMobile/hasTouch)로 한 번 더 열어 screenshot-mobile 을
  찍는다. UA 가 컨텍스트 옵션이라 재네비게이션이 필요하다. pipeline 이
  `db.mobile_screenshot_enabled` 로 읽어 capture 에 전달, 켠 뒤 새 스냅샷에만
  적용. 모바일 UA 로만 다른 호스트의 루프백으로 리다이렉트되면 모바일
  스크린샷만 생략한다 — 대시보드 누수 방지, 원칙 7·netcheck)과 문서 아카이브 설정
  (`document_max_count`/`document_max_mb`/`document_fetch_timeout_seconds` —
  페이지가 링크한 문서를 받을 때의 스냅샷당 수·문서 1개 크기(MB)·다운로드
  타임아웃(초). 해석·클램핑은 `documents.limits`(오염·범위 밖이면 config
  기본값), pipeline 이 읽어 `documents.download_documents`/`download_direct`
  에 전달, 이후 저장되는 스냅샷에 적용)과 메일(SMTP) 설정
  (`smtp_host`/`smtp_port`/`smtp_user`/`smtp_password`/`smtp_from`/`smtp_tls` —
  초대 메일 발송 서버. 시스템 화면 또는 `WCCG_SMTP_*` 환경변수로 두며 DB 값이
  우선, 해석·환경변수 폴백은 `mailer.resolve_config`. `smtp_password` 는
  `crypto` 로 대칭 암호화한 암호문만 저장 — 외부 SMTP 에 replay 해야 하므로
  복원 가능, 원칙 6 예외이며 사이트 로그인 자격증명과 같은 처리)
