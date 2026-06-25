# GitHub Actions 리포 설정 체크리스트 — TylorSTYLE/ChunChuGwan

- 측정일: 2026-06-26
- 대상: **TylorSTYLE/ChunChuGwan** (PUBLIC, 리포 1개 한정)
- 전제: 이 리포는 public이라 **호스티드 러너 무과금**이며, 아티팩트도 사실상 0MB →
  과금 절감 목적의 **자동 변경은 적용하지 않았다**(측정상 이득 0). 아래는 보안·위생 권고.

## 현재 설정 (측정값, 읽기 전용)

| 항목 | 현재 값 | API 출처 | 평가 |
|---|---|---|---|
| Actions 활성 | `enabled: true` | `GET /repos/{r}/actions/permissions` | — |
| 허용 액션 범위 | **`selected`** (화이트리스트) | 동상 | ✅ 이미 제한적 |
| SHA 핀 강제 | **`sha_pinning_required: true`** | 동상 | ✅ 공급망 위생 우수 |
| 기본 토큰 권한 | **`read`** (최소권한) | `…/actions/permissions/workflow` | ✅ 이미 최소권한 |
| 봇 PR 승인 토큰 | `can_approve_pull_request_reviews: true` | 동상 | ⚠️ 릴리스 자동화 의존(아래 주의) |
| fork PR 워크플로 승인 | **`all_external_contributors`** | `…/actions/permissions/fork-pr-contributor-approval` | ✅ 외부 기여자 전원 승인 필요 |
| 명시 아티팩트 보존 | `retention-days: 1` (docker.yml digests) | 워크플로 파일 | ✅ 이미 최소 |
| 자동 `.dockerbuild` 아티팩트 | 62 live / 각 ~0MB, ~39일 후 만료 | `…/actions/artifacts` | 🟢 무해(0MB·public 무료) |

→ **체크리스트 권고 항목 대부분이 이미 충족(✅)돼 있다.** 추가로 켤 것이 거의 없다.

## 자동 적용한 변경
**없음.** 측정상 정당화되는 비파괴 변경이 없다(스토리지 0MB·public 무료, 보안 설정은 이미 하드닝).
Stop Conditions(권한·보안·Actions 토글·재정 설정)에 해당하는 항목은 자동 변경하지 않는다.

## 권고만 (자동 변경 금지 — 사람이 판단 후 적용)

### 1. 아티팩트/로그 보존 기간 — 변경 불요(권고만)
- 현재: 명시 아티팩트는 1일, 자동 dockerbuild는 0MB. public이라 스토리지 무료.
- 굳이 리포 기본 보존을 줄이려면:
  - UI: **Settings → Actions → General → "Artifact and log retention"** (기본 90일, 1~90 조정)
  - 영향: 로그 보존도 함께 줄어 디버깅 이력이 짧아짐. **이득 0(무료)인데 디버깅만 손해 → 비권장.**

### 2. 허용 액션 화이트리스트 — 이미 `selected`(유지)
- UI: **Settings → Actions → General → "Allow {owner}, and select non-{owner}, actions and reusable workflows"**
- 현재 selected + SHA 핀 강제. 새 액션 추가 시 화이트리스트 갱신 필요(이미 올바른 상태).
- 조회: `gh api repos/TylorSTYLE/ChunChuGwan/actions/permissions/selected-actions`

### 3. fork PR에서 Actions 승인 — 이미 `all_external_contributors`(유지)
- UI: **Settings → Actions → General → "Fork pull request workflows from outside collaborators"**
- 현재 외부 기여자 전원 승인 필요 — 가장 안전한 값. 변경 불요.

### 4. 봇 PR 생성/승인 권한 — ⚠️ 끄지 말 것 (릴리스 자동화 의존)
- `can_approve_pull_request_reviews: true` 및 **Settings → Actions → General → "Allow GitHub Actions
  to create and approve pull requests"** 는 `docker.yml` promote 잡이 develop→main 릴리스 PR을
  자동 생성하는 데 쓰인다. `RELEASE_TOKEN`(PAT) 없이 이 설정을 끄면 **릴리스 자동화가 깨진다.**
  (근거: `.claude/rules/release-docker.md` 릴리스 토큰 의존성 절)
- 조이려면: 먼저 `repo`+`workflow` 스코프 PAT을 `RELEASE_TOKEN` 시크릿으로 등록한 뒤 끈다.

### 5. spending limit·예산 — 리포 범위 밖 (메모만)
- 스펜딩 리밋/예산은 **계정/조직 레벨**(Settings → Billing → Budgets and alerts)이라
  리포 단위 작업 범위 밖. public 리포는 호스티드 분이 무료라 이 리포만으로는 과금 0.
- 변경 시 다른 리포의 빌드까지 영향 → 이 태스크에서 건드리지 않음.

## Stop Conditions 준수
- Actions 권한/보안/접근 제어 설정 변경 → **자동 적용 안 함**(권고만)
- Actions 활성/비활성 토글 → 안 함
- 재정 설정(spending limit) → 범위 밖, 메모만
- 파일 삭제·의존성/액션 버전 변경 → 없음
