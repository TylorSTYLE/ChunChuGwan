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

기능 PR 은 `develop` 을 베이스로 머지한다(main 직행 금지). 버전 산출·릴리스 PR
생성·병합·태깅은 **GitFlow 스킬**(`.claude/skills/gitflow/release.md`)이 담당한다 —
직전 태그 이후 커밋의 Conventional Commits 프리픽스(`feat:`=minor,
`fix:`/`chore:`/`hotfix:`=patch, `BREAKING CHANGE`·`!`=major)로 다음 버전을
자동 산출해 확인 게이트에서 승인받고, develop→main PR(제목 `release: v[버전]`)을
만든다. 이 PR 은 develop 가 main 의 조상으로 남도록 **merge 커밋으로 머지**한다
(squash 면 다음 릴리스부터 diff 가 발산한다). 병합 후 main HEAD 에 `v[버전]`
태그를 달아 push하면(병합·태그 모두 사용자 수행이 기본, 명시적 위임 시 대행
가능) `docker-image.yml` 이 태그 push 로 발화해 이미지를 빌드·스모크·게시하고,
`release.yml` 이 SBOM 생성·라이선스 게이트 + GitHub Release(자동 노트) 등록을
수행한다(둘 다 CI 가 버전을 스스로 결정하지 않는다 — 태그 push 이후만 자동화).
버전 출처는 설치 메타데이터(`chunchugwan.__version__` / `wccg --version`).

`develop` 에 푸시되면 `docker-image.yml` 이 `:develop` 이미지를 빌드한다 —
플랫폼별 네이티브 빌드(amd64/arm64) → 임시 스테이징 태그로만 push →
컨테이너 기동 스모크(CLI `--version` + 대시보드 8765 응답) 통과 후에만
가변 태그로 재태깅해 공개한다(스모크 실패 시 깨진 이미지가 `:develop` 으로
노출되지 않는다 — 릴리스 태그 push 도 동일 게이트). 도커 이미지 태그:
`:latest`·`:main`·`:vX.Y.Z`(릴리스 태그 push), `:develop`(develop push),
`:sha-<커밋>`(불변 롤백 식별자, 양쪽 다).

## 릴리스 노트 (업데이트 안내 모달)

표시 내용은 **GitHub Release 기준**으로 릴리스 PR 준비 단계에서 생성한다(수동
작성 불필요). GitFlow 스킬 release.md 4단계("버전 파일 범핑 + 릴리스 노트
준비")가 `gh api …/releases/generate-notes`(대상: develop HEAD, 예정 태그명)로
그 버전 노트를 미리 받아 `scripts/gen_release_notes.py`(=
`release_notes.parse_github_notes`)로 변환 — 수정자(`@user`)·원본 링크 제거,
봇 항목 제외, **PR 번호/URL 만 유지** — 후 `chunchugwan/web/release_notes.json`
의 그 버전 키에 써넣어 develop 대상 준비 PR(`chore/prep-v[버전]`)에 포함한다.
이 준비 PR 이 develop 에 병합된 뒤에야 릴리스 PR 을 진행하므로, main 에
병합·태그되는 커밋에는 항상 그 버전의 노트가 이미 포함돼 있다(태그 이미지에
동봉 → 런타임 외부 호출 0). 대시보드는 로그인 후 현재 버전(`__version__`)
항목이 있을 때만 모달을 1회 띄우고 각 항목에 `#번호` PR 링크를 건다 — 항목이 없으면
조용히 안 뜬다(오류 아님). 표시 동작 상세는 `docs/DASHBOARD.md` 업데이트 안내 모달 절.
(수동 갱신/백필이 필요하면: `gh release view vX.Y.Z --json body -q .body | python3
scripts/gen_release_notes.py X.Y.Z`.)

## Docker 명령

컨테이너 실행 명령(`docker compose up`/`run`, 운영은 `docker-compose.yml`, develop 은
별개 독립 파일 `docker-compose.dev.yml` — 디버그 진단 포트 포함)은 CLAUDE.md `## 명령어`
참조. 운영·환경변수·배포 상세는 `docs/DOCKER.md`.
