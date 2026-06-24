# 클러스터 (federation)

여러 춘추관 인스턴스를 연결해 아카이브를 **선택적으로** 주고받는 기능이다. 각
인스턴스는 독립적으로 동작하며, 운영자가 명시적으로 연결·방향·보호를 설정한 범위
안에서만 스냅샷이 오간다. 기본값은 **보호(전송 안 함)** 라서, 아무것도 설정하지
않으면 어떤 데이터도 밖으로 나가지 않는다.

> 관련 아키텍처 원칙(CLAUDE.md): 쓰기는 코어 경유(원칙 1), 스냅샷 불변(원칙 2 —
> 수신분은 새 스냅샷), content_hash 중복 제거(원칙 3), iframe sandbox 렌더(원칙 5 —
> 수신분도 동일 경로), 인증 데이터 단방향·자격증명만 대칭 암호화(원칙 6).

## 모델 — 누가 무엇을 하나

- **노드 식별**: 설치 시 영속 UUID 1개를 `settings`(`cluster_node_id`)에 만들어
  재시작·백업/복원 후에도 동일하게 쓴다. 피어 매칭은 **항상 이 UUID**로 한다.
  디스플레이 이름(`cluster_display_name`)은 표시용일 뿐 신뢰·식별 근거가 아니다.
- **연결은 항상 B(연결을 등록한 쪽)가 개시**한다. A 는 B 로 능동 연결하지 않는다.
  - 보내기(push): B 가 자기 아카이브를 A 로 올린다.
  - 받기(pull): B 가 A 의 아카이브를 내려받는다.
- **권한은 A 가 발급한 시스템 키**로 표현한다(개인 API Key 와 별개). 키 소유자(B)
  기준으로 방향을 읽는다:
  - `can_cluster_send` — 이 키로 B 가 A 에 **보낼(push)** 수 있다.
  - `can_cluster_receive` — 이 키로 B 가 A 에서 **받을(pull)** 수 있다.
- **실제 수행 = (A 키 권한) AND (B 연결 설정)** 이 모두 켜진 방향만. 둘 중 하나라도
  꺼지면 그 방향은 동작하지 않는다.

## 설정 절차

A(데이터를 내주거나 받아 줄 쪽), B(연결을 거는 쪽) 두 인스턴스가 있다고 하자.

1. **A**: 시스템 → API 키(`/system/api-keys`)에서 키를 발급하며 **클러스터 보내기/
   받기** 권한을 선택한다. 원문은 발급 직후 1회만 표시된다(`manage_users` 권한 필요).
   - B 가 A 로 **보내게** 하려면 → `클러스터 보내기` 체크.
   - B 가 A 에서 **받게** 하려면 → `클러스터 받기` 체크.
2. **B**: 시스템 → 클러스터(`/system/cluster`)에서 **피어 연결 추가**에 A 의 주소와
   위 키, 방향(보내기/받기)을 입력한다. 연결 시 핸드셰이크로 A 의 UUID·프로토콜
   버전을 받아 저장한다(`manage_system` 권한 필요).
3. 이후 B 의 조정 루프가 주기적으로 A 의 상태를 확인하고, 허용된 방향으로 델타만
   주고받는다. 추가 조작은 필요 없다.

> **전제: 같은 `WCCG_SECRET_KEY`.** B 는 A 발급 키를 평문이 아니라 대칭 암호화해
> 저장한다(원칙 6 예외, `export` 제외). 키가 없으면 피어 등록이 거부된다.

## 아카이브 보호 (전송 차단, 페이지 단위)

보호 ON = 다른 클러스터로 **전송하지 않음**. 출처측(데이터를 가진 노드)이 강제한다.

- **해소 순서**: 페이지 명시값 > 사이트 기본값 > 시스템 기본값.
  - `pages.cluster_protect` (NULL=사이트 기본 상속)
  - `sites.cluster_protect_default` (새 사이트 기본 1=보호)
  - `settings.cluster_protect_default` (시스템 기본, 기본 on=보호)
- **선택 지점**:
  - 새 아카이빙(`/archive/new`)의 `다른 클러스터로 공유 허용` 토글(기본 OFF=보호).
    단일 페이지는 페이지 값으로, 새 사이트면 사이트 기본값으로도 적용된다.
  - REST `POST /api/v1/archive`·`POST /api/v1/ingest` 의 `protect` 필드.
  - 크롬 확장 archive 탭의 `다른 클러스터로 공유 허용` 체크박스.
- 비동기 서버 캡처는 작업 큐(`archive_jobs.protect`)에 실어, 워커가 캡처 후
  메타로 적용한다(캡처 로직 자체는 바뀌지 않는다). 확장 적재(ingest)는 동기 적용.

## 전송 — 스냅샷 1건이 원자 단위

대량 봉투(export)를 그대로 쓰지 않고 **스냅샷 입자**로 직렬화·적재한다.

- **단위**: 스냅샷 1건 = 메타 + 인라인 스냅샷 파일(page.html.gz·raw.html.gz·
  content.md·screenshot 등) + 공유 CAS 블롭(자원·문서) 참조.
- **CAS 협상**: 큰 공유 블롭은 인라인하지 않고 sha256 참조만 보낸다. 받는 쪽이
  자기 CAS 와 대조해 **없는 것만** 별도로 가져간다(pull) / 보내는 쪽이 협상으로
  결손만 업로드(push). 블롭은 sha256 으로 무결성 검증한다(불일치 거부).
- **커서**: 출처 노드의 단조 시퀀스(`snapshots.id`). 소비자는 피어별·방향별 마지막
  커서(`cluster_peers.send_cursor`/`receive_cursor`)를 기록해 전수 재스캔 없이
  "X 이후 신규"만 조회한다. 성공한 건까지만 커서가 전진하므로, 중단되면 진행 중
  1건만 손실되고 다음 사이클에 이어진다(부분 적재 없음).
- **중복 수신**: 동일 출처(노드 UUID + 원본 snapshots.id)면 처리·저장·로깅을 모두
  생략한다. 신규만 적재·기록한다.
- **루프 방지**: 수신분(`snapshots.origin_node_id` 기록)은 다시 내보내지 않는다
  (전송 선택은 **로컬 생성분만**, 보호 기본값도 ON). 1:N 을 가정하며, 재연합(N:N)은
  명시 비범위다.

## 조정 루프 (reconciliation)

`cluster_sync` 가 피어별로 한 사이클에서 순서대로 처리한다(scheduler 옆 백그라운드
스레드, 주기는 `cluster_sync_interval_seconds` 설정).

1. **상태/권한 갱신**: A 의 상태 엔드포인트를 키로 조회 → 키 활성·방향 권한 확인.
   - 키 폐기(401/403) → 이 피어 폴링 **영구 중단**(revoked).
   - 5xx·타임아웃 등 일시 오류 → 종료가 아니라 **지수 백오프 재시도**(degraded).
   - 프로토콜 불호환 → error(운영자 개입 전까지 제외).
2. **받기**(허용 시): 커서 교환으로 신규만 pull.
3. **보내기**(허용 시): 커서 이후 공유 가능(보호 OFF)분만 push.

**정중한 페이싱**: 사이클당 배치 상한(`CLUSTER_SYNC_BATCH_MAX`), 건당 최소 간격
(`CLUSTER_SEND_MIN_INTERVAL_SECONDS`), 받는 쪽 백프레셔(바쁘면 429 Retry-After —
대기·진행 아카이빙 작업이 임계 이상이거나 이전·스토리지 마이그레이션 중) → 보내는
쪽 백오프. "서로 여유 있을 때"만 오간다.

## 엔드포인트 (`/api/cluster/*` — 시스템 키 게이트)

`/api/v1`(개인 키)·`/api/web`(세션)과 분리된 머신-투-머신 채널이다. 매 요청 서버측에서
시스템 키(owner=NULL)+방향 권한을 재검증하고, 인증 실패는 IP 별 인증 보호(`cluster_ip`
버킷)로 막는다. 통신은 항상 B 가 개시한다(A 는 응답만).

| 메서드·경로 | 권한 | 용도 |
|---|---|---|
| `GET /api/cluster/status` | 클러스터 키 | 핸드셰이크·권한 갱신(노드 UUID·버전·키 권한) |
| `GET /api/cluster/snapshots?after=&limit=` | 받기 | 커서 이후 공유 가능 스냅샷 목록 |
| `GET /api/cluster/snapshots/{id}` | 받기 | 스냅샷 envelope(보호분은 404) |
| `GET /api/cluster/blobs/{kind}/{name}` | 받기 | 공유 CAS 블롭 서빙 |
| `POST /api/cluster/negotiate` | 보내기 | 수신측에 없는 블롭 목록 협상 |
| `POST /api/cluster/blobs/{kind}/{name}` | 보내기 | CAS 블롭 업로드(sha256 검증) |
| `POST /api/cluster/snapshots` | 보내기 | 스냅샷 envelope 적재(중복 스킵·자기출처 거부) |

> 전송 계층 보호는 리버스 프록시 HTTPS(구간 암호화)에 의존한다. 키는 Authorization
> 헤더로만 보내며 URL·쿼리스트링에 키·민감정보를 싣지 않는다.

## 로깅

- **수신**: 신규 적재만 `archive_logs`(`source='cluster'` + 출처 피어 `cluster_peer_id`)에
  기록한다. 중복 수신은 무기록.
- **전송**: 신규 전송을 `audit_logs`(`action='cluster_send'`, 대상 피어)에 기록한다.
- 두 로그 모두 append-only. 수신분도 일반 아카이브와 같이 `<iframe sandbox>` 안에서만
  렌더된다(원칙 5 — 비신뢰 콘텐츠 동일 경로).

## 설정 키 (settings)

| 키 | 기본 | 의미 |
|---|---|---|
| `cluster_node_id` | (자동) | 영속 노드 UUID — 직접 바꾸지 않는다 |
| `cluster_display_name` | (빈값) | 표시용 이름 |
| `cluster_protect_default` | `on` | 시스템 보호 기본값(on=보호) |
| `cluster_sync_interval_seconds` | 300 | 조정 사이클 간격(60~86400 클램핑) |

관련 환경변수·운영 상수는 `chunchugwan/config.py` 의 `CLUSTER_*` 참조. 시스템 키
권한 모델은 [AUTHENTICATION.md](AUTHENTICATION.md), REST 채널 일반은 [API.md](API.md)
참조.
