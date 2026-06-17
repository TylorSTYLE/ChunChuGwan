---
description: 릴리스 자동화(gitflow CI)·Docker. 워크플로/Dockerfile/compose/pyproject 를 만질 때.
paths:
  - ".github/workflows/**"
  - "Dockerfile"
  - "docker-entrypoint.sh"
  - "compose.example.yaml"
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

## Docker 명령

컨테이너 실행 명령(`docker compose up`/`run`, `compose.yaml` 복사)은 CLAUDE.md `## 명령어`
참조. 운영·환경변수·배포 상세는 `docs/DOCKER.md`.
