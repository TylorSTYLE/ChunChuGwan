---
description: 릴리스 자동화(gitflow CI)·Docker. 워크플로/Dockerfile/compose/pyproject 를 만질 때.
paths:
  - ".github/workflows/**"
  - "Dockerfile"
  - "docker-entrypoint.sh"
  - "docker-compose.yml"
  - "docker-compose.dev.yml"
  - "pyproject.toml"
  - "chunchugwan/__init__.py"
  - "docs/DOCKER.md"
  - "docs/DEVELOPMENT.md"
---

# 릴리스 · Docker

## 브랜치 흐름 = gitflow (CI 자동화 상세)

기능 PR 은 `develop` 을 베이스로 머지한다
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

**릴리스 토큰 의존성 (함정 주의).** 릴리스 PR 생성(`docker.yml` promote 잡의
`gh pr create`)·보호 브랜치 푸시(`release.yml`)는 `secrets.RELEASE_TOKEN ||
secrets.GITHUB_TOKEN` 순으로 토큰을 쓴다. `RELEASE_TOKEN`(PAT)을 등록하지 않으면
`GITHUB_TOKEN` 으로 폴백되는데, 이때 리포 설정
`Settings → Actions → General → Allow GitHub Actions to create and approve pull
requests` 를 끄면 봇이 PR 을 못 만들어 **릴리스 자동화가 깨진다** (이 체크박스는
생성+승인을 함께 통제). 즉 PAT 없이 이 설정을 끄지 말 것. 봇 권한을 조이려면
먼저 `repo`+`workflow` 스코프 PAT 을 `RELEASE_TOKEN` 으로 등록한 뒤 끈다.

## 릴리스 노트 (업데이트 안내 모달)

표시 내용은 **GitHub Release 기준**으로 **CI 가 자동 생성**한다(수동 작성 불필요).
`release.yml` 의 버전 범프 스텝이 `gh api …/releases/generate-notes` 로 그 버전 노트를
받아 `scripts/gen_release_notes.py`(= `release_notes.parse_github_notes`)로 변환 —
수정자(`@user`)·원본 링크 제거, 봇 항목 제외, **PR 번호/URL 만 유지** — 후
`chunchugwan/web/release_notes.json` 의 그 버전 키에 써넣어 **릴리스 커밋에 포함**한다
(태그 이미지에 동봉 → 런타임 외부 호출 0). 대시보드는 로그인 후 현재 버전(`__version__`)
항목이 있을 때만 모달을 1회 띄우고 각 항목에 `#번호` PR 링크를 건다 — 항목이 없으면
조용히 안 뜬다(오류 아님). 표시 동작 상세는 `docs/DASHBOARD.md` 업데이트 안내 모달 절.
(수동 갱신/백필이 필요하면: `gh release view vX.Y.Z --json body -q .body | python3
scripts/gen_release_notes.py X.Y.Z`.)

## Docker 명령

컨테이너 실행 명령(`docker compose up`/`run`, `docker-compose.yml`·develop 은
`docker-compose.dev.yml` 오버라이드)은 CLAUDE.md `## 명령어`
참조. 운영·환경변수·배포 상세는 `docs/DOCKER.md`.
