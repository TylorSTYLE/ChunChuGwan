# 외부 API (API 키)

> 외부 소프트웨어가 아카이빙을 트리거하거나 아카이빙된 데이터를 가져갈 수
> 있는 `/api/v1` REST API. 개요는 [README](../README.md) 참조.

사용자가 자신이 만든 소프트웨어(크롬 확장 포함)로 아카이빙을 트리거하거나
아카이빙된 데이터를 가져갈 수 있도록 `/api/v1` REST API 를 제공한다. 인증은
**개인 API Key** — `Authorization: Bearer <키>` 또는 `X-API-Key: <키>` 헤더로
보낸다. (인증이 꺼진 loopback 환경에서는 키 없이 접근 가능)

`/api/v1` 은 **개인 API Key 전용**이다. 사용자가 `/settings/api-keys`(개인 API Key)
에서 직접 발급하며(만료: 영구 · 1일 · 1개월 · 1년 · 사용자 지정, 최대 3650일), 발급·
사용 모두 세분 권한 `use_api_keys` 가 필요하다(빌트인 기본은 관리자·아카이브 관리·
아카이브 보유, 보기 전용 제외). 토큰 권한(보기/아카이브)은 소유자의 현재 실효 권한으로
매 요청 재평가되므로 역할 강등·권한 회수가 즉시 반영된다 — **발급 권한(`use_api_keys`)을
잃으면 그 사용자의 모든 키가 401**(크롬 확장은 이때 자동 연결 해제). 관리자 발급
시스템 키(`/system/api-keys`, owner 없음)는 `/api/v1` 인증 대상이 아니다. 권한 체계는
`docs/AUTHENTICATION.md` 참조.

키 원문은 발급 직후 한 번만 표시되고 DB 에는 SHA-256 해시만 저장된다. 폐기하면 즉시
무효화된다. 인증 실패는 IP 별 인증보호(설정의 "인증 보호", `auth_throttle`)로 무차별
대입을 막는다 — 무효 토큰을 반복하면 429(`Retry-After`).

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/api/v1/version` | (토큰만) | 서버 앱 버전 + 확장 버전(독립) — 확장이 `extension_version` 과 자기 버전을 비교해 업데이트 안내. 응답 `{"version": "...", "extension_version": "..."}` |
| GET | `/api/v1/pages` | 보기 | 페이지 목록 (`?url=` 로 단일 조회) |
| GET | `/api/v1/pages/{id}` | 보기 | 페이지 상세 + 스냅샷 히스토리 |
| GET | `/api/v1/snapshots/{id}` | 보기 | 스냅샷 메타데이터 |
| GET | `/api/v1/snapshots/{id}/file/{name}` | 보기 | 파일 다운로드 (`page.html` \| `screenshot` \| `content.md`) |
| POST | `/api/v1/archive` | 아카이브 | 아카이빙 트리거 — 본문 `{"url": "...", "force": false, "network_tag": "<id>"}`(`network_tag` 는 사설 호스트 선택) |
| POST | `/api/v1/crawl` | 아카이브 | 사이트 전체 아카이브(크롤) — 본문 `{"url": "...", "max_pages": …, "max_depth": …, "delay": …}` |
| POST | `/api/v1/auth-profiles` | 아카이브 | 로그인 세션으로 단일 페이지 1회 인증 캡처 (크롬 확장 전용) |
| GET | `/api/v1/archive/status` | 보기 | 작업·크롤 상태 일괄 조회 — `?jobs=…&crawls=…` (확장 결과 알림용) |
| POST | `/api/v1/ingest` | 아카이브 | 확장이 브라우저에서 직접 캡처한 산출물 적재 (멀티파트, 동기 응답, 사용자 토큰 전용) — 서버 무요청 |
| GET | `/api/v1/network-tags` | 아카이브 | 로컬 네트워크 태그 목록 (사설 호스트 캡처 시 선택) |
| POST | `/api/v1/network-tags` | 시스템 관리 | 로컬 네트워크 태그 생성 (`manage_system` 권한) |

```bash
curl -H "Authorization: Bearer wccg_..." http://127.0.0.1:8765/api/v1/pages
curl -X POST -H "X-API-Key: wccg_..." -H "Content-Type: application/json" \
     -d '{"url": "https://example.com"}' http://127.0.0.1:8765/api/v1/archive
```

아카이빙은 백그라운드로 실행되며(응답 202), 같은 URL 이 이미 진행 중이면
`queued: false`(크롤은 진행 중 크롤로 `merged: true`)로 응답한다. API 로
실행된 아카이빙은 로그에 `api` 출처로 기록된다. `/archive`·`/auth-profiles`
응답에는 작업 식별자 `job_id` 가, `/crawl` 응답에는 `crawl_id` 가 실려 — 아래
상태 조회로 결과를 추적할 수 있다 (`queued: false` 인 중복 등록도 기존 활성
작업의 `job_id` 를 돌려준다).

## 작업 상태 조회 / 결과 알림

`GET /api/v1/archive/status?jobs=<id,id>&crawls=<id,id>` 로 제출한 단발 작업·
크롤의 현재 상태를 일괄 조회한다 (각 최대 50개, 잘못된 id 는 무시). 상태는
**토큰 소유자**로 스코프되어 — 남이 요청한 작업 id 는 `unknown` 으로만 보인다.
단발 작업 행은 완료/최종실패 시 삭제되므로, 활성 작업이 없으면 실행 로그
(`job_id`)에서 종결 상태를 도출한다 (활성 작업이 있으면 그 상태가 우선 —
재시도 중 작업이 과거 실패 로그로 오판되지 않는다).

```json
{
  "jobs": [
    {"id": 1, "state": "in_progress", "url": "..."},
    {"id": 2, "state": "needs_human", "url": "..."},
    {"id": 7, "state": "succeeded", "outcome": "changed",
     "url": "...", "page_id": 10, "snapshot_id": 42, "http_status": 200},
    {"id": 8, "state": "failed", "url": "...", "error": "..."},
    {"id": 9, "state": "unknown"}
  ],
  "crawls": [
    {"id": 6, "status": "done", "url": "...",
     "counts": {"done": 40, "failed": 2, "pending": 0, "in_progress": 0, "total": 42}}
  ]
}
```

- job `state`: `pending` · `in_progress` · `needs_human`(사람 확인 필요) ·
  `succeeded`(+`outcome` = new/changed/unchanged/forced_same) · `failed`(+`error`) ·
  `unknown`.
- crawl `status`: `running` · `done` · `cancelled` · `unknown` (+ 상태별 페이지 수 `counts`).

크롬 확장은 이 엔드포인트를 주기 폴링해 완료·실패·사람 확인 시 데스크톱 알림을
띄운다 (확장의 **"작업이 끝나면 알림 받기"** 토글, 기본 켜짐). `notifications`·
`alarms` 권한이 필요해, 기존 사용자는 확장 업데이트 후 권한을 한 번 재승인해야 한다.

## 로그인 정보 캡처 (크롬 확장)

크롬 확장의 **"내 로그인 정보 포함"** 옵션은 현재 브라우저의 로그인 상태를 함께
보내 인증된 상태로 아카이브한다. 확장이 **로그인 방식을 자동 판단**한다 —
페이지의 `localStorage`/`sessionStorage` 에서 인증용 **JWT(Bearer 토큰)**가
감지되면 토큰을, 아니면 **세션 쿠키**를 보낸다 (비밀번호·아이디는 절대 보내지
않는다). 요청 본문 필드:

- `jwt`: 감지된 Bearer 토큰 → 서버가 1회성 `jwt` 자격증명을 만들어 대상 origin
  요청에만 `Authorization: Bearer` 헤더로 주입.
- `storage_state`: 세션 쿠키(Playwright storage_state) → 1회성 `session`
  자격증명, 쿠키는 대상 사이트 도메인으로 스코프. (둘 다 실리면 `jwt` 우선.)

단일 페이지는 `/api/v1/auth-profiles`, 사이트 전체는 `/api/v1/crawl` 으로 보낸다.
자격증명은 아카이빙(또는 크롤 전 페이지)에 적용한 뒤 폐기된다(누락분은 만료 GC
가 정리, 진행 중 크롤은 보호). 조건: **개인 API Key**(사용자 귀속) +
`WCCG_SECRET_KEY` 설정 + **https 대상**. JWT 감지를 위해 확장에 `scripting`
권한이 필요하다(업데이트 시 재승인).

## 브라우저 클라이언트 캡처 (ingest)

확장의 **"이 페이지 캡처 (브라우저)"** 는 서버를 거치지 않고 브라우저에서 직접
현재 페이지를 캡처해 `POST /api/v1/ingest` 로 올린다. 서버는 **대상 URL 을 다시
가져오지 않고**(capture 미실행) 받은 산출물에 기존 코어(추출·정규화·해시·자원
CAS·검색 색인·저장)만 적용한다. 멀티파트(`multipart/form-data`) + **동기 응답**:

- 파일: `page_html`(확장이 자원을 인라인 완성한 단일 파일), `raw_html`(추출·해시용
  DOM), `screenshot`(CDP 풀페이지 PNG, 선택), `documents`(링크 문서, 다중) +
  `document_urls`(JSON, 인덱스 정렬).
- 폼: `url`·`final_url`·`title`·`http_status`·`force`·`incomplete`·`is_document`·
  `capture_env`(JSON: viewport·dpr·ua)·`network_tag`.

조건: **개인 API Key**(사용자 귀속 — 시스템 키 불가, 스냅샷 attribution),
아카이브 권한, 업로드 본문 상한(`WCCG_INGEST_MAX_MB`, 기본 50MB — 초과 413).
업로드 바이트는 신뢰 불가로 취급해 자원은 미디어 타입 화이트리스트, 문서는 확장자
화이트리스트로 서버가 재검증한다(스크립트는 샌드박스 렌더라 실행 안 됨).

사설 IP·루프백 게이트(netcheck)는 ingest 에도 적용된다 — 루프백은 400, 사설
대역은 `network_tag` 가 없으면 **422 `{"needs_network_tag": true, "host": "..."}`**
로 응답한다. 확장은 이때 `GET /api/v1/network-tags` 로 목록을 받아 태그를 고르거나
(생성은 `manage_system` 권한 필요) 추가한 뒤 재전송한다. 적재된 페이지는 이후
서버가 다시 가져오지 않는다(스케줄·크롤·재아카이빙 차단) — 갱신은 확장 재캡처로만.
구현 설계는 `docs/EXTENSION_CLIENT_CAPTURE_PLAN.md` 참조.
