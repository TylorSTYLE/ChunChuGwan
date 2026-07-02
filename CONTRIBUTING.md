# 기여 가이드 (Contributing)

춘추관(ChunChuGwan)에 기여해 주셔서 감사합니다. 이 문서는 브랜치·커밋·PR 규약과
개발자 원본 증명(DCO) 절차를 정리한다. 아키텍처 원칙·DB 스키마·상세 코딩 컨벤션은
[CLAUDE.md](CLAUDE.md), 개발 환경 구성은 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)를
따른다.

## 개발 환경

```bash
uv sync                               # 의존성 설치 (없으면 pip + venv)
uv run playwright install chromium    # 최초 1회 (캡처 엔진)
uv run pytest                         # 백엔드 테스트 (네트워크 불필요, ~10초)
```

프론트엔드(SvelteKit 대시보드)를 만진다면:

```bash
cd frontend
pnpm install
pnpm run check   # svelte-check 타입 검사
pnpm run lint    # eslint
pnpm run build   # 정적 빌드
```

## 브랜치·PR 흐름 (GitFlow)

- **`main`·`develop` 에 직접 push 금지** — 병합은 PR 로만 하고, 병합은 메인테이너가 수행한다.
- 작업 브랜치는 항상 `git fetch` 후 원격 최신 `origin/develop` 에서 분리한다.
  - `feature/*` — 새 기능 · `bugfix/*` — 버그 수정 · `chore/*` — 유지보수(CI·문서·의존성 등)
  - 위 세 유형은 모두 **`develop` 을 베이스로 PR** 을 올린다(`main` 직행 금지).
  - `hotfix/*` 만 `main` 을 베이스로 하며, 병합 후 `main → develop` back-merge 가 뒤따른다.
- 작업 PR 은 **squash merge**, 릴리스 PR(`develop → main`)은 **merge 커밋**으로 병합한다.
- 이번 작업 범위 밖의 기존 코드 수정, 무관한 설정·lock 파일 변경은 PR 에 섞지 않는다.

## 커밋 규약 (Conventional Commits)

PR 제목·커밋 메시지는 [Conventional Commits](https://www.conventionalcommits.org/) 프리픽스를
따른다. squash 시 **PR 제목이 커밋 메시지가 되고**, 릴리스 버전(SemVer)이 이 프리픽스로
자동 산출되므로 정확히 붙인다.

| 프리픽스 | 용도 | 버전 영향 |
|---|---|---|
| `feat:` | 기능 추가 | minor |
| `fix:` / `chore:` / `hotfix:` | 버그 수정 / 유지보수 / 긴급 수정 | patch |
| `feat!:` 등 `!` 또는 `BREAKING CHANGE` | 호환성 파괴 | major |

새 기능·버그 수정에는 해당 테스트를 함께 추가한다(네트워크 의존 테스트 규칙은
[.claude/rules/testing.md](.claude/rules/testing.md)). 타입 힌트는 필수, docstring 은
한국어로 간결하게 쓰고, 외부 입력(URL·파일 경로)은 검증·정규화 후 사용한다.

## 개발자 원본 증명 (DCO)

이 프로젝트는 [Developer Certificate of Origin](https://developercertificate.org/) 1.1 을
사용한다. **모든 커밋에 sign-off 가 필요하다** — 커밋 시 `-s` 플래그를 붙이면 커밋 메시지
끝에 다음 트레일러가 추가된다:

```bash
git commit -s -m "fix: ..."
# → Signed-off-by: Your Name <your.email@example.com>
```

이 한 줄은 "내가 이 기여를 제출할 권리가 있으며, 프로젝트 라이선스로 공개됨에 동의한다"는
증명이다(DCO 전문은 위 링크). 실명과 유효한 이메일을 쓰고, **Signed-off-by 이메일은 커밋
author(또는 committer) 이메일과 일치**해야 한다. 이미 만든 커밋에 sign-off 를 빠뜨렸다면:

```bash
git commit --amend -s          # 마지막 커밋 1개
git rebase --signoff <base>    # 여러 커밋 (이후 git push --force-with-lease)
```

PR 의 모든 커밋은 `.github/workflows/dco.yml` 이 자동 검사하며, 누락·불일치가 있으면
어떤 커밋인지 로그에 표시하고 실패한다(병합 커밋은 제외).

## 이슈 · 보안 리포트

- 버그·기능 제안은 GitHub Issues 로 등록한다. 재현 절차·기대 동작·실제 동작을 적으면 좋다.
- **보안 취약점은 공개 이슈로 올리지 말 것** — 제보 절차는 [SECURITY.md](SECURITY.md) 를
  따른다(GitHub Private Vulnerability Reporting 또는 메인테이너 이메일 폴백).
