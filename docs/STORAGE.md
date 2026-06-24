# 저장 구조 · 정규화 룰 · 백업/복원

> 아카이브가 디스크에 저장되는 형태, 비교 노이즈를 제거하는 도메인별
> 정규화 룰, 데이터 백업/복원·내보내기/가져오기를 다룬다. 개요는
> [README](../README.md) 참조.

## 저장 구조

```
archive/
├── index.db                # SQLite 인덱스 (pages / snapshots / checks)
├── rules.json              # (선택) 도메인별 정규화 룰
├── cache/                  # 파생 산출물 (픽셀 diff 하이라이트 등, 재생성 가능)
├── resources/              # 스냅샷 간 공유 자원 CAS — 이미지·폰트·CSS,
│                           #   sha256 콘텐츠 주소라 같은 자원은 한 번만 저장
├── documents/              # 문서 파일 CAS — 페이지가 링크한 PDF·워드·한글 등,
│                           #   같은 내용은 스냅샷이 달라도 한 번만 저장
└── sites/{domain}/{slug}-{url_hash8}/{timestamp}/
    ├── page.html.gz        # 단일 HTML (gzip). 큰 자원은 /resource/ 참조,
    │                       #   작은 자원(<4KB)은 data URI 인라인 유지
    ├── raw.html.gz         # 렌더링 후 DOM 소스 (gzip)
    ├── content.md          # 추출+정규화 텍스트 (해시/diff 기준)
    ├── screenshot.webp     # 전체 페이지 데스크탑 스크린샷 (WebP 한도 초과·용량
    │                       #   역효과면 screenshot.png 유지 + screenshot.png.keep 마커)
    ├── screenshot-mobile.webp  # (선택) 모바일 해상도(390×844) 스크린샷 — 시스템
    │                       #   설정 '캡처 설정'이 켜졌을 때 찍힌 스냅샷에만 있다
    └── meta.json           # url, final_url, 시각, 해시, http 정보,
                            #   documents 목록 (문서 CAS 참조 — 출처 URL·해시),
                            #   origin(server|extension)·incomplete·capture_env
                            #   (확장 클라이언트 캡처의 viewport·dpr·ua)
```

문서 파일은 페이지당 최대 20개·개당 50MB 까지 저장하며(기본값 — 대시보드
**시스템** 메뉴의 "문서 아카이브 설정"에서 문서 수·크기·다운로드 타임아웃을
바꿀 수 있다), 링크 확장자가 문서 화이트리스트(pdf, doc(x), ppt(x), xls(x),
hwp(x), odt/odp/ods, rtf, pages, key, numbers, epub, zip)에 있을 때만 받는다.
응답이 HTML(로그인·오류 페이지)이면 건너뛰고, 다운로드 실패는 페이지
아카이빙을 막지 않는다. 문서는 **캡처에 쓰인 브라우저의 네트워크 스택으로
먼저 받는다** — 일부 사이트(WAF)가 봇 차단으로 일반 HTTP 클라이언트의
연결을 TLS 단계에서 끊기 때문이며, 브라우저로 못 받은 것만 일반 HTTP 로
폴백한다. URL 자체가 파일 다운로드(download.php?file=…pdf 등)인 경우도 같은
원리로, 브라우저가 탐색 중 받은 파일을 그대로 문서 스냅샷으로 저장한다.
같은 내용(sha256)의 문서는 documents/ CAS 에 한 번만 저장되고 스냅샷은
참조만 가진다 — 참조하는 스냅샷이 모두 삭제되면 파일도 함께 삭제된다.
compact 이전의 구형 스냅샷은 문서를 스냅샷 안 files/ 디렉토리에 직접 담고
있으며, `wccg compact` 가 CAS 로 이전한다.

대시보드 **시스템** 메뉴(`/system`)의 "캡처 설정"에서 모바일 해상도
스크린샷을 켜면, 데스크탑 스크린샷 외에 같은 URL 을 **안드로이드 크롬
모바일 브라우저**(UA·뷰포트 390×844·터치)로 한 번 더 열어 찍은
`screenshot-mobile.webp` 를 저장한다 (모바일 레이아웃 확인용). User-Agent 가
브라우저 컨텍스트 옵션이라 데스크탑 캡처를 재사용하지 못하고 같은 URL 을
모바일로 재캡처한다. 기본은 꺼짐이며, 설정을 켠 뒤 새로 만들어지는
스냅샷에만 적용된다. 스냅샷 뷰어는 모바일 스크린샷이 있을 때만 "모바일
스크린샷" 탭을 노출한다.

스냅샷 디렉토리는 불변이다. 변경 = 새 스냅샷. 아카이브 위치는 환경변수
`WCCG_ROOT`로 변경할 수 있다 (기본 `./archive`).

압축 저장 형태 도입 이전에 만든 스냅샷(page.html / raw.html / screenshot.png)도
그대로 읽힌다. 기존 스냅샷을 압축 형태로 변환해 저장 공간을 줄이려면:

```bash
uv run wccg compact          # 1회성 마이그레이션 (내용 보존 변환, --yes 로 확인 생략)
```

대시보드 **시스템** 메뉴(`/system`)의 "저장 공간 압축" 버튼으로도 같은 변환을
실행할 수 있다 (인증이 켜진 환경에서는 관리자 전용).

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

## 백업/복원

```bash
uv run wccg backup [dest]                  # 전체 백업 .ccg.backup (DB·인증 데이터·스냅샷 파일·rules.json)
uv run wccg restore <file> [--yes]         # 전체 복원 — 현재 데이터를 백업 시점 상태로 교체
uv run wccg export [dest]                  # 아카이브 데이터만 내보내기 (페이지·스냅샷·확인 기록 + 파일)
uv run wccg import <file>                  # 가져오기 (기본 merge — 기존 유지, 중복 스냅샷 스킵)
uv run wccg import <file> --mode overwrite # 기존 아카이브 데이터를 지우고 가져오기 (--yes 로 확인 생략)
```

- `backup`/`restore` 는 인증 데이터(사용자·세션·패스키)까지 포함한 전체 복구용.
  복원은 아카이브 루트 전체를 교체하므로 확인 프롬프트를 거친다.
- `export`/`import` 는 아카이브 데이터(pages·snapshots·checks + 스냅샷 파일)만
  다룬다 — 인증 테이블과 실행 로그(archive_logs)는 건드리지 않으므로 다른
  인스턴스로 데이터를 옮기거나 합칠 때 쓴다. `merge` 는 같은 페이지의 같은
  스냅샷 디렉토리를 스킵해 여러 번 실행해도 안전하다(멱등).
- `dest` 를 생략하면 현재 디렉토리에 백업은 `chunchugwan-backup-{시각}.ccg.backup`,
  내보내기는 `chunchugwan-export-{시각}.ccg.export` 로 생성된다 (둘 다 내용은 tar.gz).
- `restore` 는 `.ccg.backup`, `import` 은 `.ccg.export` 확장자 파일만 받는다
  (대시보드 업로드도 동일) — 다른 확장자는 거부한다.
- 대시보드의 **시스템** 메뉴(`/system`)에서도 같은 기능을 쓸 수 있다 — 백업·
  내보내기는 파일 다운로드, 복원·가져오기는 파일 업로드. 백업에 인증 데이터가
  포함되므로 인증이 켜진 환경에서는 관리자만 접근할 수 있다.
- **휴지통과의 관계** — 전체 백업(`backup`)은 휴지통 항목을 **그대로 보존**하지만,
  데이터 내보내기(`export`)는 휴지통 항목을 **제외**한다 (삭제 보류 상태의
  데이터는 다른 인스턴스로 옮기지 않는다).
- **S3 모드에서는 전체 백업/복원이 비활성화**된다 (blob 이 로컬에 없어 일관
  백업이 불가). 내구성은 아래 **S3 DB 백업**으로 확보하고, 데이터 이동은
  `export`/`import` 로 한다 (S3 모드에서도 동작 — blob 을 S3 에서 스트리밍).

## S3 객체 저장소 백엔드 (선택)

기본은 로컬 파일시스템에 blob 을 저장한다. 대용량·원격 운영을 위해 **blob
(`sites/`·`resources/`·`documents/`)만 S3 호환 객체 저장소**에 둘 수 있다. 자체
호스팅이면 **MinIO 설치를 권장**한다. `index.db`(SQLite)·`cache/`·read-through
캐시는 항상 로컬에 남는다 (DB 는 S3 에 두지 않는다).

### 자격증명·활성화

연결 정보는 환경변수 `WCCG_S3_*` 로 준다 (로컬 실행은 `.env`, 도커는
`env_file`/override). **부팅 시 가용성**만 판정하고, 실제 **활성 백엔드는 DB
설정 `storage_backend`(`local`|`s3`, 기본 `local`)** 로 결정한다 — env 만 설정해도
활성 백엔드는 바뀌지 않는다 (데이터에 접근 못 하는 상태로의 무단 전환 방지).
전환은 **마이그레이션 0실패 완료** 또는 **첫 구동 setup** 으로만 일어난다.
`storage_backend=s3` 인데 필수 env 가 불완전하면 부팅이 명확히 실패한다.

| 환경변수 | 설명 |
|---|---|
| `WCCG_S3_ENDPOINT_URL` | MinIO 등 커스텀 엔드포인트 (AWS 면 비우고 region 사용) |
| `WCCG_S3_BUCKET` | 버킷 이름 (필수) |
| `WCCG_S3_ACCESS_KEY_ID` / `WCCG_S3_SECRET_ACCESS_KEY` | 자격증명 (필수, **env 전용 — DB·로그 미저장**) |
| `WCCG_S3_REGION` | 리전 (기본 `us-east-1`) |
| `WCCG_S3_FORCE_PATH_STYLE` | path-style 주소 (MinIO 위해 기본 `on`) |
| `WCCG_S3_PREFIX` | 버킷 내 키 프리픽스 (선택) |
| `WCCG_BLOB_CACHE_MAX_MB` | read-through 캐시 용량 상한 (기본 2048) |

### 서빙 (stream-through + read-through 캐시)

아카이브 열람은 **presigned URL 을 쓰지 않는다**(원칙 5 — CSP sandbox·인증
게이트·미디어 화이트리스트 유지). 대신 존재 확인은 HEAD(객체 미다운로드),
서빙 시점에만 blob 을 **로컬 read-through 캐시**(`blobcache/`)로 받아 그 로컬
경로로 기존과 동일한 응답(헤더·gzip·attachment·Range)을 돌려준다. blob 은
콘텐츠 주소·불변이라 캐시 무효화가 없고, 용량 상한 초과 시 LRU 로만 제거한다.

### 양방향 마이그레이션 (로컬 ↔ S3)

시스템 → 스토리지에서 활성 백엔드의 전 blob 을 반대 백엔드로 **copy** 한다 —
매니페스트 작성 → 파일 단위 복사(업로드는 sha256 체크섬으로 종단 무결성) →
존재+크기 검증 → 파일당 최대 3회 재시도 → **0건 실패에서만 완료**로 보고
활성 백엔드를 전환한다. 진행 중에는 캡처·스케줄·크롤이 일시중지되고 읽기는
유지된다. 원본은 **자동 삭제하지 않으며**, 완료 후 "원본 정리 대기" 안내가 뜨고
관리자가 수동으로 삭제한 뒤 확인한다 (멱등 재실행 안전).

### S3 DB 백업

S3 모드의 전체 백업 대체 수단. `index.db` + `rules.json` 을 일관 복사(sqlite
backup API)해 tar.gz 한 객체로 `<prefix>/db-backups/<UTC 시각>.tar.gz` 에 업로드
한다 (종단 sha256 체크섬). **정기**(스케줄러, 주기 설정) + **즉시**(웹/CLI) 실행,
보존 개수 초과분만 삭제(rotation, 최신 보존). `wccg db-backup` / `wccg db-backup
status`, 시스템 화면의 DB 백업 카드.

### 첫 구동 분기 · 복구

사용자 0명 + blob 존재 시: **S3 DB 백업이 있으면 복원**(인증 데이터 포함 완전
복구 → 로그인), 없으면 **복구모드**로 blob 에서 인덱스를 재구축한다(부분 —
사용자 미복구라 복구 후 관리자 생성 필요). ⚠ 복구모드는 meta.json 에 없는
`authenticated` 플래그를 알 수 없어 **복구된 모든 스냅샷을 보수적으로 관리자
전용(authenticated=1)으로 제한**한다. 복구 후 관리자가 시스템 화면에서 **[전체
공개]**(일괄) 또는 스냅샷별 토글로 **개별 해제**해 공개 정책을 정한다.

### 온디맨드 카테고리별 사용량

S3 모드에서 화면·요청 경로는 **S3 를 자동 호출하지 않고** 캐시된 값만 보여준다.
시스템 화면의 Object Storage 사용량 **[업데이트]**(부하 확인 후) 또는 `wccg
storage status --scan` 으로 ListObjectsV2 를 페이지네이션 순회해 카테고리별
(sites·resources·documents·db-backups) 사용량을 산출·캐시한다. 로컬 사용량
(index.db + cache + read-through 캐시)은 S3 호출 없이 분리 표시한다.

## 삭제 · 휴지통 (소프트 삭제)

페이지·사이트 삭제는 기본적으로 즉시 영구삭제가 아니라 **휴지통(소프트 삭제)**으로
간다 (`deletion.py`). 삭제된 항목은 모든 목록·검색·뷰어·문서·서빙에서 숨겨지지만
스냅샷 파일과 공유 자원/문서 CAS 는 그대로 보존된다. 보관 기간이 지나면 스케줄러가
영구삭제하고, 그때 비로소 CAS 정리(GC)가 일어난다.

```bash
uv run wccg delete <url>                  # 휴지통으로 이동 (기본 — 파일 보존, 목록에서 숨김)
uv run wccg delete <url> --hard           # 휴지통을 거치지 않고 즉시 영구삭제
uv run wccg delete <url> --snapshot N     # 단일 스냅샷 삭제 — 항상 즉시 삭제 (휴지통 대상 아님)
uv run wccg trash list                    # 휴지통 항목 목록
uv run wccg trash restore <id|URL>        # 휴지통에서 복원 (다시 목록·검색·뷰어에 노출)
uv run wccg trash purge <id|URL>          # 영구삭제 (특정 항목)
uv run wccg trash purge --expired         # 보관 기한이 지난 항목만 영구삭제
uv run wccg trash purge --all [--yes]     # 휴지통 전체 영구삭제
```

- **보관 기간** — 시스템 설정의 `trash_retention_days`(기본 30일, `0` = 자동삭제
  끔)가 지난 항목은 스케줄러가 자동으로 영구삭제한다.
- **휴지통 끄기** — `trash_enabled`(기본 on)를 끄면 삭제가 즉시 영구삭제로 동작한다
  (종전 동작). 끈 상태여도 이미 휴지통에 있는 항목은 계속 관리·자동삭제된다.
- **CAS GC 시점** — 휴지통에 머무는 동안에는 공유 자원/문서 CAS 의 참조가 남아 있어
  GC 되지 않는다. **영구삭제(또는 `--hard`) 때** 비로소 참조가 0 이 된 CAS 파일이
  삭제되고, FTS 색인·diff 캐시 정리, 빈 사이트 행 정리가 함께 일어난다.
- **단일 스냅샷 삭제는 범위 밖** — `wccg delete <url> --snapshot N` 과 내부
  `delete_snapshot` 은 휴지통을 거치지 않고 항상 즉시 삭제한다.
- **재아카이빙 안전장치** — 휴지통에 있는 URL 을 다시 아카이빙하면 자동으로
  복원된다 (숨겨진 페이지에 스냅샷이 쌓이지 않게).
- 대시보드의 휴지통 화면(`/archive/trash`, `manage_trash` 권한)에서도 열람·복원·
  영구삭제할 수 있다 (docs/DASHBOARD.md 참조).
