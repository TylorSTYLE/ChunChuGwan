# GitHub Actions 비용 감사 — TylorSTYLE/ChunChuGwan

- 측정일: 2026-06-26
- 윈도: 최근 30일 (2026-05-27 ~ 2026-06-26)
- 대상 리포: **TylorSTYLE/ChunChuGwan** (현재 작업 디렉터리 리포 1개 한정)

## 결론 (먼저)

**측정된 청구 분(billable minutes) = 0.** 이 리포는 **PUBLIC**이며, 모든 잡이
GitHub-hosted 표준 러너(`ubuntu-latest`, `ubuntu-24.04-arm`)에서만 돈다.
공개 리포의 표준 호스티드 러너는 **무과금**이다.

이건 추정이 아니라 **GitHub timing API 의 측정값**이다 — 표본 실행 10건(CI 5 + Docker 5)
모두에서 `billable.UBUNTU.total_ms = 0`, 각 잡 `duration_ms = 0` 으로 반환됐다.

→ **측정으로 정당화되는 워크플로 절감 변경이 0건이다.** 청구 분 기준으로 손댈 대상이 없다.

## 측정 근거

### 1) 리포 성격
| 항목 | 값 |
|---|---|
| visibility | **PUBLIC** |
| fork | false |
| 기본 브랜치 | develop |
| 워크플로 파일 | `ci.yml`, `docker.yml`, `release.yml` (+ Dependabot) |
| `runs-on` | 전부 `ubuntu-latest` / `ubuntu-24.04-arm` (GitHub-hosted) — **self-hosted/커스텀 라벨 잡 없음** |

> 태스크 프롬프트의 전제("자가 호스팅 러너 운영 중", "Rust(cargo)/Kotlin(Gradle)" 스택)는
> 이 리포에 **해당하지 않는다**. 실제 스택은 Python(FastAPI)+SvelteKit이고, 러너는 전부
> GitHub-hosted이며 self-hosted 잡은 존재하지 않는다. (템플릿 프롬프트의 일반 가정과 불일치)

### 2) billable 측정 (timing API)
표본 10건 전부 `billable.UBUNTU.total_ms = 0`:
```
CI    run 28192806618 → billable UBUNTU total_ms=0 (wall 179s)
CI    run 28191395396 → billable UBUNTU total_ms=0 (wall 196s)
Docker run 28192803411 → billable UBUNTU total_ms=0 (wall 98s, 5 jobs)
Docker run 28191392945 → billable UBUNTU total_ms=0 (wall 100s, 5 jobs)
...(10/10 동일: total_ms=0)
```
`run_duration_ms`(벽시계)는 0이 아니지만, **billable 환산 분은 0** — public repo이기 때문.

### 3) 30일 실행량 (참고 — 과금과 무관, 활동량 지표)
| 워크플로 | 30일 실행 수 | 잡/런 | 벽시계 평균(분/런)[근사] | 청구 분 | 절감 여지 |
|---|---:|---:|---:|---:|---|
| **CI** (`ci.yml`) | 379 | 2 (frontend, test) | ~3.7 | **0** | 낮음 (billing 무관) |
| **Docker** (`docker.yml`) | 180 | 5 (build×2, merge, smoke, promote) | ~3.4 | **0** | 낮음 (billing 무관) |
| **Release** (`release.yml`) | 16 | 1 | ~0.3 | **0** | 낮음 (billing 무관) |
| Dependabot Updates | ~2 | — | ~1 | **0** | 낮음 |
| **합계** | **577** | — | — | **0** | — |

> 벽시계 수치는 `startedAt~updatedAt` 합산 **[근사]**(300건 상한 도달로 표본은 최근 ~9일,
> 2026-06-16~25 구간). 상대 랭킹(CI > Docker ≫ Release)은 안정적. billable 자체는 timing
> API로 정확히 0 확정이라 벽시계 정밀도는 결론에 영향 없음.

## 이미 적용돼 있는 최적화 (추가 절감 여지 작음)

현 워크플로는 이미 비용/시간 위생이 잘 잡혀 있어, billing이 유의미했더라도 손댈 여지가 적다:
- **concurrency + cancel-in-progress**: `ci.yml`(ref별 취소), `docker.yml`(develop만 취소, main/태그 보존)
- **timeout-minutes**: 모든 잡에 상한 (frontend 10 / test 20 / build 30 / merge·smoke·promote·release 10)
- **paths-ignore**: `ci.yml` (docs/·*.md·.gitignore·LICENSE 제외)
- **draft PR 스킵**: `ci.yml` (`pull_request.draft == false`)
- **캐싱**: uv(`enable-cache`), npm(`setup-node cache`), Playwright(`actions/cache` + restore-keys), Docker buildx(`type=gha, mode=max`, 플랫폼별 scope)
- **러너 라우팅**: arm64를 QEMU 에뮬 대신 네이티브 `ubuntu-24.04-arm` 으로 (public이라 arm 호스티드도 무료)
- **트리거 분리**: CI의 push는 `main`만, PR은 develop 대상 → 동일 커밋 이중 실행 없음
- **중복 빌드 차단**: `docker.yml` build 잡이 릴리스 범프 커밋(`춘추관 v…`) push 빌드를 skip

## 권고 (측정 정당화 아님 — 적용 보류)

아래는 **청구 분 절감 근거가 없으므로 이번 작업에서 변경하지 않는다.** 향후 **리포가 PRIVATE로
전환되면** 그때 측정 후 검토할 후보로만 남긴다:
- Docker 잡의 build 매트릭스(amd64+arm64)는 private 전환 시 arm64가 비용 요인이 될 수 있음 → 그때 재측정
- CI `test` 잡의 Playwright/chromium 설치 시간 → private 전환 시 캐시 적중률 점검

## 효율(벽시계) 분석 — 사용자 옵션 3 (billing 무관, 측정 기반)

> 청구 분은 0이라 절감 대상이 아니지만, 벽시계/기여자 대기시간 관점에서 손댈 가치가
> 있는지 잡·스텝 단위로 측정했다. `run_duration_ms` 및 jobs API 의 실제 소요 기준.

### 잡별 벽시계 (대표 run 측정)
| 워크플로 | 잡 | 벽시계 | 임계경로 | 지배 요인 |
|---|---|---:|---|---|
| CI | frontend | 0.5분 | 아니오(병렬) | svelte-check + SPA 빌드 |
| CI | **test** | **2.9분** | **예** | **pytest 133s(76%)** + SPA 빌드 19s + Chromium apt-deps 10s |
| Docker | build×2 | 0.3·0.5분 | — | buildx gha 캐시 적중(거의 캐시) |
| Docker | merge·smoke·promote | 0.1~0.4분 | — | 경량 |

### 후보 검토 결과 — **고칠 가치 있는 변경 없음**
1. **pytest 133s** — CI 임계경로의 76%. 순수 테스트 실행 시간이며 테스트 코드 변경은
   범위 밖("기능 추가·리팩터링 금지", "통과 검사 깨지 말 것"). → **불가/범위 밖**.
2. **SPA 빌드 19s가 frontend·test 두 잡에 중복** — 그러나 둘은 **병렬**이라 벽시계 중립.
   artifact 재사용으로 dedup하면 test가 frontend 완료(~30s)를 기다려야 해
   빌드 가용 시점이 T+27s → T+33s 로 **오히려 느려진다**(측정 기반). → **net-negative, 보류**.
3. **Chromium apt 시스템 의존성 10s(캐시 히트 시)** — `playwright install-deps` 의 apt 설치는
   브라우저 캐시에 담기지 않는 OS 레벨 의존성이라 캐시 불가. ubuntu+Playwright의 고정 비용.
   → **불가피**.
4. **Docker 빌드** — 이미 gha 캐시로 0.3~0.5분. 추가 캐싱 여지 없음. → **이미 최적**.
5. concurrency·timeout·paths-ignore·draft 스킵·fetch-depth(checkout v7 기본 shallow=1) 등
   교과서적 절감 항목은 **전부 이미 적용**돼 있어 추가하면 중복 churn. → **불필요**.

### 결론 (옵션 3)
측정상 **저위험·유의미한 벽시계 개선이 없다.** 임계경로는 실제 테스트 실행 시간(범위 밖)이고,
나머지 오버헤드는 기존 캐싱·병렬화로 이미 최소화돼 있다. **추측 기반 변경을 피하기 위해
Phase 2 최적화 PR을 생성하지 않을 것을 (옵션 3에서도) 권고한다.**

## Acceptance 판정
- [x] `gha-cost-audit.md` 생성 (워크플로별 분 랭킹 포함)
- [ ] 최적화 PR — **측정상 정당화 0건이라 생성하지 않음을 권고** (Phase 2 게이트에서 결정)
- [ ] `gha-settings-checklist.md` — Phase 3 산출 예정
- [x] 추측 변경 0건 (billable=0 측정 확정)
