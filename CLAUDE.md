# Web Archiver

개인 웹 아카이빙 시스템. URL을 받아 전체 페이지를 스냅샷으로 저장하고,
같은 URL을 다시 아카이빙하면 히스토리가 쌓이며 스냅샷 간 비교(diff)가 가능하다.

## 기술 스택

- Python 3.12+ / 패키지 관리: `uv` (없으면 pip + venv)
- 캡처: Playwright (chromium, headless)
- DB: SQLite (`archive/index.db`) — ORM 없이 표준 `sqlite3` 사용
- CLI: click
- 대시보드: FastAPI + Jinja2 템플릿 (읽기 전용 + 재아카이빙 트리거)
- 테스트: pytest

## 명령어

```bash
uv sync                                  # 의존성 설치
uv run playwright install chromium       # 최초 1회
uv run archiver add <url>                # 스냅샷 생성
uv run archiver add <url> --force        # 콘텐츠 동일해도 강제 저장
uv run archiver list                     # 전체 아카이브 현황
uv run archiver history <url>            # 해당 URL 스냅샷 목록
uv run archiver diff <url>               # 최신 2개 스냅샷 비교
uv run archiver diff <url> --from 1 --to 3
uv run archiver serve                    # 대시보드 (127.0.0.1:8765)
uv run pytest                            # 테스트
```

## 아키텍처 원칙 (중요 — 반드시 지킬 것)

1. **쓰기는 코어 모듈을 통해서만.** 스냅샷 생성/삭제는 `storage.py` + `db.py`를
   거친다. 대시보드든 외부 에이전트든 직접 파일/DB를 조작하지 않는다.
2. **스냅샷은 불변(immutable).** 한번 저장된 스냅샷 디렉토리는 수정하지 않는다.
   변경 = 새 스냅샷.
3. **콘텐츠 해시 기반 중복 제거.** 정규화된 텍스트의 SHA-256이 직전 스냅샷과
   같으면 새 스냅샷을 만들지 않고 `checks` 테이블에 "확인했음" 기록만 남긴다.
   (`--force` 시 예외)
4. **비교는 정규화된 텍스트 기준.** 타임스탬프, CSRF 토큰, 광고 등 노이즈는
   `extract.py`의 정규화 단계에서 제거한 후 해시/diff 한다.
5. **대시보드는 localhost 전용.** 기본 바인딩 127.0.0.1. 아카이빙된 HTML을
   렌더링할 때는 반드시 `<iframe sandbox>` (스크립트 실행 금지) 안에서만 보여준다.
   아카이빙된 페이지의 JS를 대시보드 컨텍스트에서 실행하는 일은 절대 없어야 한다.

## 저장 구조

```
archive/
├── index.db
└── sites/
    └── {domain}/
        └── {slug}-{url_hash8}/
            └── {timestamp ISO, 콜론은 - 로}/
                ├── page.html       # 자원 인라인된 단일 HTML
                ├── raw.html        # 렌더링 후 DOM 소스
                ├── content.md      # 추출+정규화 텍스트
                ├── screenshot.png  # 전체 페이지
                └── meta.json       # url, final_url, 시각, 해시, http 정보
```

## DB 스키마

`archiver/db.py`의 `SCHEMA` 참조. 핵심 테이블:
- `pages` — 정규화된 URL 단위 (1 URL = 1 row)
- `snapshots` — 스냅샷 단위, `pages.id` FK, content_hash 보관
- `checks` — 중복으로 저장 생략된 확인 기록

## 코딩 컨벤션

- 타입 힌트 필수, docstring은 한국어로 간결하게
- 외부 입력(URL, 파일 경로)은 항상 검증/정규화 후 사용 — path traversal 주의
- 네트워크 요청에는 타임아웃 필수 (페이지 로드 기본 30s)
- 새 기능 = 해당 테스트 추가. 네트워크 의존 테스트는 로컬 fixture HTML 사용
- 커밋은 기능 단위로 작게

## 대시보드 디자인 방향

- 화면 4개: 목록(index) / 타임라인(timeline) / 스냅샷 뷰어(snapshot) / diff 뷰어(diff)
- 도구다운 밀도 있는 UI. 모노스페이스로 해시/시각 표기, 변경 상태는 색 뱃지
  (변경=amber, 동일=gray, 신규=green). 과한 장식/그라데이션 금지.
- diff 뷰: 텍스트 side-by-side + 스크린샷 비교(슬라이더 또는 토글)

## 구현 로드맵 (이 순서로 진행할 것)

- [x] **M1 코어 저장소**: `config.py`, `db.py`, `storage.py` 완성 + 테스트.
      URL 정규화(쿼리 정렬, fragment 제거, 트래킹 파라미터 utm_* 제거 등) 포함.
- [ ] **M2 캡처**: `capture.py` — Playwright로 렌더링 → raw.html, 전체 스크린샷,
      자원 인라인 page.html(이미지/CSS를 base64 인라인. 1차 버전은 스타일시트와
      이미지까지만, 폰트는 M5). `extract.py` — 본문 텍스트 추출(trafilatura) +
      정규화. `cli.py`의 `add` 연결. 실제 URL 1개로 수동 검증.
- [ ] **M3 히스토리/diff**: `differ.py` — difflib unified + side-by-side 데이터,
      변경 요약(추가/삭제 줄 수). `cli.py`의 `history`, `diff`, `list` 연결.
- [ ] **M4 대시보드**: `web/app.py` + 템플릿 4종. 재아카이빙 버튼은
      BackgroundTasks로 코어 호출.
- [ ] **M5 고도화**: 스크린샷 픽셀 diff(Pillow), 폰트 인라인, 도메인별 정규화
      룰(셀렉터 제거 목록) 설정 파일, robots.txt 존중 옵션.

각 마일스톤 완료 시: 테스트 통과 확인 → 위 체크박스 갱신 → 커밋.
