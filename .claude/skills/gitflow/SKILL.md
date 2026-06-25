---
name: gitflow
description: 이 프로젝트의 GitFlow 브랜치·PR·릴리즈 표준 절차. 작업 브랜치 생성/재개, develop 대상 PR 생성, develop→main 릴리즈 PR 생성에 사용한다. "브랜치 만들어줘/따줘", "PR 올려줘/만들어줘", "릴리즈 PR", "머지/배포 준비", feature·bugfix·chore·hotfix 작업, git 워크플로 관련 요청이면 명시적으로 언급하지 않아도 반드시 이 스킬을 사용하라. main/develop 직접 push 방지와 PR 기반 병합을 강제하는 안전 절차다.
allowed-tools: Bash(git:*) Bash(gh:*) Read Edit
---

# GitFlow 표준 절차

작업 유형을 판별하고, 해당 절차 파일을 읽어 그대로 수행한다. 각 절차는 사용자 확인 게이트를 포함하며, 게이트에서 반드시 멈추고 명시적 승인을 받은 뒤 진행한다.

## 공통 전제 (불변)
- **main**: 릴리즈 전용 — 직접 커밋/push 금지, PR 병합만. **develop**: 작업 베이스 — 직접 커밋/push 금지, PR 병합만. 병합은 항상 사용자가 직접 수행.
- 작업 브랜치: `feature/*` · `bugfix/*` · `chore/*` → 베이스·병합 **develop** / `hotfix/*` → 베이스 **main**, 병합 **main + develop**.
- 브랜치 보호(main, develop 공통): PR 승인 0명, enforce_admins: true.
- 명명: 소문자 + 하이픈. 예: `feature/user-auth`, `bugfix/login-crash`, `chore/gha-cost-cut`.
- 금지: main/develop 직접 push, 이번 작업 범위 외 기존 코드 수정, 빌드/배포 실행, 이번 작업의 명시적 목적이 아닌 `.env`·설정·lock 파일 수정.
- 공통 중단 조건: 작업 디렉터리 dirty / 리베이스 충돌 / 예상치 못한 오류 2회 연속 → 즉시 중단하고 보고.

## 라우팅
- **새 브랜치 생성 또는 기존 브랜치 재개** → 번들된 `branch.md`를 읽고 따른다.
- **작업 브랜치 → 대상 브랜치 PR 생성** → 번들된 `pr.md`를 읽고 따른다.
- **develop → main 릴리즈 PR 생성** → 번들된 `release.md`를 읽고 따른다.

요청이 모호하면(예: "정리해서 올려줘") 어느 절차인지 사용자에게 먼저 확인한다. 모든 단계 완료 후 각 절차 파일에 정의된 진행 보고/완료 요약을 출력한다.