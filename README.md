# Web Archiver

개인 웹 아카이빙 시스템. URL을 받아 전체 페이지를 스냅샷(단일 HTML + 스크린샷 +
추출 텍스트)으로 저장하고, 같은 URL을 다시 아카이빙하면 히스토리가 쌓이며
스냅샷 간 텍스트/스크린샷 비교(diff)가 가능하다.

- 콘텐츠 해시 기반 중복 제거 — 본문이 그대로면 새 스냅샷 대신 "확인했음" 기록만 남음
- 타임스탬프·상대시각·광고 줄 등 노이즈는 정규화 단계에서 제거 후 비교
- 이미지/CSS/폰트를 base64로 인라인한 단일 page.html (오프라인 열람 가능)
- 읽기 전용 localhost 대시보드 (목록/타임라인/스냅샷 뷰어/diff 뷰어 + 재아카이빙 버튼)

## 설치

```bash
uv sync                                  # 의존성 설치
uv run playwright install chromium       # 최초 1회
```

## 사용법

```bash
uv run archiver add <url>                # 스냅샷 생성
uv run archiver add <url> --force        # 콘텐츠 동일해도 강제 저장
uv run archiver list                     # 전체 아카이브 현황
uv run archiver history <url>            # 해당 URL 스냅샷 목록 (번호는 diff에 사용)
uv run archiver diff <url>               # 최신 2개 스냅샷 비교 (+ 스크린샷 픽셀 diff)
uv run archiver diff <url> --from 1 --to 3
uv run archiver serve                    # 대시보드 (http://127.0.0.1:8765)
```

## 저장 구조

```
archive/
├── index.db                # SQLite 인덱스 (pages / snapshots / checks)
├── rules.json              # (선택) 도메인별 정규화 룰
├── cache/                  # 파생 산출물 (픽셀 diff 하이라이트 등, 재생성 가능)
└── sites/{domain}/{slug}-{url_hash8}/{timestamp}/
    ├── page.html           # 자원 인라인된 단일 HTML
    ├── raw.html            # 렌더링 후 DOM 소스
    ├── content.md          # 추출+정규화 텍스트 (해시/diff 기준)
    ├── screenshot.png      # 전체 페이지
    └── meta.json           # url, final_url, 시각, 해시, http 정보
```

스냅샷 디렉토리는 불변이다. 변경 = 새 스냅샷. 아카이브 위치는 환경변수
`ARCHIVER_ROOT`로 변경할 수 있다 (기본 `./archive`).

## 도메인별 정규화 룰 (선택)

`archive/rules.json`에 도메인별로 비교 노이즈를 제거할 룰을 둘 수 있다.
저장 산출물(raw.html, page.html)에는 손대지 않고 해시/diff 기준 텍스트에만 적용된다.

```json
{
  "example.com": {
    "remove_selectors": [".ads", "#recommend-widget"],
    "remove_line_patterns": ["^관련 기사", "^구독하기$"]
  }
}
```

- `remove_selectors` — 본문 추출 전에 DOM에서 제거할 CSS 셀렉터
- `remove_line_patterns` — 정규화 텍스트에서 버릴 줄의 정규식 (`www.` 접두사 없는 키로도 조회됨)

## 대시보드

`archiver serve` 후 http://127.0.0.1:8765 접속. localhost 전용이며, 아카이빙된
HTML은 항상 `<iframe sandbox>` 안에서만 렌더링되어 원본 페이지의 스크립트가
대시보드 컨텍스트에서 실행되지 않는다. 재아카이빙 버튼은 백그라운드로 코어
파이프라인을 호출한다.

## 개발

```bash
uv run pytest                            # 테스트 (네트워크 불필요, ~10초)
```

아키텍처 원칙·DB 스키마·코딩 컨벤션은 [CLAUDE.md](CLAUDE.md) 참조.
모듈 구성:

| 모듈 | 역할 |
|---|---|
| `archiver/storage.py` | URL 정규화, slug, 스냅샷 파일시스템 레이아웃 |
| `archiver/db.py` | SQLite 인덱스 (모든 DB 접근의 단일 창구) |
| `archiver/capture.py` | Playwright 렌더링, 자원 인라인, 셀렉터 제거 |
| `archiver/extract.py` | 본문 추출(trafilatura) + 정규화 |
| `archiver/differ.py` | 텍스트 diff + 스크린샷 픽셀 diff |
| `archiver/pipeline.py` | 아카이빙 흐름 (CLI/대시보드 공용 쓰기 진입점) |
| `archiver/cli.py` | click CLI |
| `archiver/web/` | FastAPI 대시보드 |
