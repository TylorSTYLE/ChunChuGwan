# 외부 API (API 키)

> 외부 소프트웨어가 아카이빙을 트리거하거나 아카이빙된 데이터를 가져갈 수
> 있는 `/api/v1` REST API. 개요는 [README](../README.md) 참조.

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
| POST | `/api/v1/crawl` | 아카이브 | 사이트 전체 아카이브(크롤) — 본문 `{"url": "...", "max_pages": …, "max_depth": …, "delay": …}` |
| POST | `/api/v1/auth-profiles` | 아카이브 | 로그인 세션으로 단일 페이지 1회 인증 캡처 (크롬 확장 전용) |

```bash
curl -H "Authorization: Bearer wccg_..." http://127.0.0.1:8765/api/v1/pages
curl -X POST -H "X-API-Key: wccg_..." -H "Content-Type: application/json" \
     -d '{"url": "https://example.com"}' http://127.0.0.1:8765/api/v1/archive
```

아카이빙은 백그라운드로 실행되며(응답 202), 같은 URL 이 이미 진행 중이면
`queued: false`(크롤은 진행 중 크롤로 `merged: true`)로 응답한다. API 로
실행된 아카이빙은 로그에 `api` 출처로 기록된다.

## 로그인 세션 캡처 (크롬 확장)

크롬 확장의 **"내 로그인 세션 포함"** 옵션은 현재 브라우저의 세션 쿠키
(비밀번호·아이디 제외)를 함께 보내 로그인 상태로 아카이브한다. 단일 페이지는
`/api/v1/auth-profiles`, 사이트 전체는 `/api/v1/crawl` 의 `storage_state` 필드로
보낸다. 서버는 받은 쿠키로 1회성 `session` 자격증명을 만들어 아카이빙(또는
크롤 전 페이지)에 적용한 뒤 폐기한다(누락분은 만료 GC 가 정리, 진행 중 크롤은
보호). 조건: **개인 API Key**(사용자 귀속) + `WCCG_SECRET_KEY` 설정 +
**https 대상**, 쿠키는 대상 사이트 도메인으로 스코프된다.
