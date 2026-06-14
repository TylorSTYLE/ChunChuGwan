# 춘추관 (ChunChuGwan)

개인 웹 아카이빙 시스템. URL을 받아 전체 페이지를 스냅샷(단일 HTML + 스크린샷 +
추출 텍스트)으로 저장하고, 같은 URL을 다시 아카이빙하면 히스토리가 쌓이며
스냅샷 간 텍스트/스크린샷 비교(diff)가 가능하다.

> ⚠️ **개발 진행 중 — 프로덕션/서비스 사용 주의.** 이 프로젝트는 현재
> 활발하게 개발이 진행 중이라 기능·DB 스키마·저장 구조가 예고 없이 바뀔 수
> 있고 호환성이 보장되지 않는다. 실제 서비스 용도로 쓰기에는 아직 안정성·검증이
> 충분하지 않으니, 사용한다면 개인·테스트 환경에서 정기 백업을 전제로 사용할 것.

- 콘텐츠 해시 기반 중복 제거 — 본문이 그대로면 새 스냅샷 대신 "확인했음" 기록만 남음
- 타임스탬프·상대시각·광고 줄 등 노이즈는 정규화 단계에서 제거 후 비교
- 이미지/CSS/폰트를 보존하는 단일 page.html — 큰 자원은 스냅샷 간 공유
  저장소(CAS)로 중복 제거, HTML 은 gzip, 스크린샷은 WebP 로 저장 공간 절약
- 페이지가 링크한 문서 파일(PDF·워드·파워포인트·한글·키노트 등)도 함께 저장 —
  같은 내용은 한 번만 저장(문서 CAS)하고, 스냅샷 뷰어의 "첨부 문서" 목록과
  대시보드 "문서" 통합 목록(`/documents`)에서 다운로드. 참조하는 스냅샷이
  모두 삭제되면 문서 파일도 자동 정리
- 사이트(섹션) 전체를 링크 따라 수집하는 크롤, 페이지/사이트 주기적 재아카이빙
- 전문(full-text) 검색 — 페이지 본문 + 첨부 문서(PDF·워드·한글 등) 본문을
  SQLite FTS5(trigram, 한국어 부분문자열)로 검색 (CLI `wccg search`, 대시보드 `/search`)
- 읽기 전용 대시보드 (목록/타임라인/스냅샷 뷰어/diff 뷰어/검색/로그 + 재아카이빙·삭제 버튼)
- 아카이브 실행 로그 — 모든 실행(성공/실패)을 단계별 소요시간과 함께 DB에 기록
- 사용자 인증 — 이메일/패스워드(+선택 TOTP 2FA), Authentik OIDC SSO 지원
- 역할 기반 권한 — 관리자/아카이브/보기 전용/권한없음(가입 승인 대기)/차단,
  대시보드에서 사용자 관리 + 가입 설정(회원 가입 허용·초기 권한)
- 외부 소프트웨어 연동용 `/api/v1` REST API (API 키)

## 문서

빠른 시작은 이 README 에, 자세한 내용은 주제별 문서로 나눠져 있다.

| 문서 | 내용 |
|---|---|
| [docs/CRAWLING.md](docs/CRAWLING.md) | 사이트 전체 아카이브(크롤) · 주기적 자동 재아카이빙 |
| [docs/STORAGE.md](docs/STORAGE.md) | 저장 구조 · 도메인별 정규화 룰 · 백업/복원·내보내기 |
| [docs/SEARCH.md](docs/SEARCH.md) | 전문 검색(FTS5 trigram) · 한국어 검색 특성 · 문서 본문 색인 |
| [docs/DOCKER.md](docs/DOCKER.md) | 도커 / 도커 컴포즈 실행 (상세) |
| [docs/AUTHENTICATION.md](docs/AUTHENTICATION.md) | 인증 · 권한 · 초대 · 2FA · OIDC · 환경변수 |
| [docs/API.md](docs/API.md) | 외부 API (API 키) 레퍼런스 |
| [docs/DASHBOARD.md](docs/DASHBOARD.md) | 대시보드 화면별 라우트·권한·세부 동작 |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | 개발 · PyCharm 구성 · 모듈 구성 |
| [docs/ROADMAP.md](docs/ROADMAP.md) | 완료된 구현 로드맵 히스토리 |
| [CLAUDE.md](CLAUDE.md) | 아키텍처 원칙 · DB 스키마 · 코딩 컨벤션 |

## 설치

```bash
uv sync                                  # 의존성 설치
uv run playwright install chromium       # 최초 1회
```

## 사용법

```bash
uv run wccg add <url>                # 스냅샷 생성
uv run wccg add <url> --force        # 콘텐츠 동일해도 강제 저장
uv run wccg list                     # 전체 아카이브 현황
uv run wccg history <url>            # 해당 URL 스냅샷 목록 (번호는 diff에 사용)
uv run wccg diff <url>               # 최신 2개 스냅샷 비교 (+ 스크린샷 픽셀 diff)
uv run wccg diff <url> --from 1 --to 3
uv run wccg search <검색어>          # 본문·첨부 문서 전문 검색 (docs/SEARCH.md)
uv run wccg search reindex          # 기존/가져온 스냅샷을 검색 인덱스에 색인
uv run wccg delete <url>             # 아카이브 전체 삭제 (모든 스냅샷, 확인 후 진행)
uv run wccg delete <url> --snapshot 2  # history 번호의 스냅샷 하나만 삭제
uv run wccg serve                    # 대시보드 (http://127.0.0.1:8765)
uv run wccg serve --host 0.0.0.0     # 외부 노출 (인증 켜진 상태에서만 허용)
uv run wccg worker                   # 아카이빙 워커 — 스케줄·크롤을 별도 프로세스에서 실행
uv run wccg -v add <url>             # 단계별 상세 로그를 stderr 로 출력
```

삭제는 대시보드의 목록(아카이브 전체)·타임라인(스냅샷 하나) 화면에서도 할 수
있다 — 인증이 켜진 환경에서는 아카이빙 권한이 있는 사용자(admin/archiver)만
가능하다 (보기 전용·차단 계정은 불가). 스냅샷 하나를
지우면 바로 다음 스냅샷의 변경 표시(변경/동일)가 새 직전 스냅샷 기준으로 자동
보정되고, 실행 로그(`/logs`)는 이력으로 남는다.

사이트 전체 아카이브(크롤)와 주기적 자동 재아카이빙은
[docs/CRAWLING.md](docs/CRAWLING.md) 참조.

## 도커 빠른 시작

로컬에 Python/uv 를 설치하지 않고 Docker Compose 로 실행할 수 있다. 예제
파일을 복사해 로컬 전용 `compose.yaml` 을 만들고(개인 설정은 거기서만 수정),
대시보드 + 워커를 띄운다.

```bash
cp compose.example.yaml compose.yaml   # 예제 복사 (최초 1회)
docker compose up -d dashboard         # 대시보드 + 워커 (http://127.0.0.1:8765)
docker compose run --rm cli add <url>  # 스냅샷 생성
docker compose down                    # 대시보드 중지
```

포트는 호스트의 127.0.0.1 에만 바인딩되고, 컨테이너 대시보드는 인증이 항상
켜진다. 도커 단독 실행·공통 사항·이미지 빌드 등 자세한 내용은
[docs/DOCKER.md](docs/DOCKER.md) 참조.

## 대시보드

`wccg serve` 후 http://127.0.0.1:8765 접속. 기본은 loopback 바인딩이며,
아카이빙된 HTML은 항상 `<iframe sandbox>` 안에서만 렌더링되어 원본 페이지의
스크립트가 대시보드 컨텍스트에서 실행되지 않는다. 재아카이빙 버튼은
백그라운드로 코어 파이프라인을 호출한다.

- **아카이빙 로그**(`/logs`, viewer 이상) — 모든 실행을 성공/실패와 관계없이
  단계별(normalize → capture → extract → hash → store) 소요시간과 함께 기록.
  도메인·페이지·스냅샷·상태(신규/변경/동일/실패)로 필터.
- **시스템 로그**(`/system/logs`, 관리자 전용) — 앱 자체의 동작 기록(serve·
  worker·CLI 의 경고/오류와 INFO 로그). 보관 한도 초과분은 자동 정리
  (`WCCG_SYSTEM_LOG_MAX_ROWS`, 기본 2만 행).
- **다국어(i18n)** — 한국어·영어. 헤더의 언어 선택은 `wccg_lang` 쿠키에
  저장되고, 선택이 없으면 `Accept-Language` 를 따른다(기본 한국어). 번역
  카탈로그는 `chunchugwan/web/i18n.py`.

화면별 라우트·권한·세부 동작은 [docs/DASHBOARD.md](docs/DASHBOARD.md) 참조.

## 인증

인증이 켜진 상태에서 사용자가 한 명도 없으면 최초 구동으로 판단한다.
`WCCG_ADMIN_EMAIL` / `WCCG_ADMIN_PASSWORD` 가 설정되어 있으면 그 값으로
관리자 계정이 자동 등록되고, 없으면 브라우저 첫 접속 시 `/setup` 관리자
등록 페이지로 이동한다. 이후 사용자는 `/signup` 또는 관리자의 이메일 초대로
가입한다.

역할은 다섯 가지 — `admin`(관리자) / `archiver`(아카이브) / `viewer`(보기
전용) / `pending`(권한없음, 가입 승인 대기) / `blocked`(차단). 패스워드
로그인에는 선택적으로 TOTP·패스키 2FA 를 걸 수 있고, Authentik OIDC SSO 도
지원한다.

권한·초대·2FA·OIDC 설정과 환경변수 전체 목록은
[docs/AUTHENTICATION.md](docs/AUTHENTICATION.md) 참조.

## 개발

```bash
uv run pytest                            # 테스트 (네트워크 불필요, ~10초)
```

PyCharm 실행/디버그 구성과 모듈 구성은 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md),
아키텍처 원칙·DB 스키마·코딩 컨벤션은 [CLAUDE.md](CLAUDE.md) 참조.
