---
description: 저장 구조 — 아카이브 파일 트리·CAS·문서 스냅샷·compact. 저장/자원/문서/삭제/최적화 모듈을 만질 때.
paths:
  - "chunchugwan/storage.py"
  - "chunchugwan/resources.py"
  - "chunchugwan/documents.py"
  - "chunchugwan/deletion.py"
  - "chunchugwan/optimize.py"
  - "chunchugwan/backup.py"
  - "docs/STORAGE.md"
---

# 저장 구조

> 관련 아키텍처 원칙: **원칙 2 — 스냅샷은 불변(immutable).** 한번 저장된 스냅샷 디렉토리는
> 수정하지 않는다. 변경 = 새 스냅샷. 유일한 예외는 `wccg compact` — 저장 형태만 바꾸는
> 내용 보존 변환(자원 CAS 추출·gzip·WebP)으로, 스냅샷이 담는 정보는 그대로다.

```
archive/
├── index.db
├── resources/                       # 스냅샷 간 공유 자원 CAS (resources.py)
│   └── {sha256 앞 2자}/{sha256}{확장자}   # 이미지·폰트·CSS, 콘텐츠 주소라 불변.
│                                    #   참조(snapshot_resources)가 0 이 되면 삭제(GC)
├── documents/                       # 문서 파일 CAS (documents.py — 인증 라우트 전용)
│   └── {sha256 앞 2자}/{sha256}{확장자}   # PDF·워드·한글 등, 같은 내용은 한 번만.
│                                    #   참조(snapshot_documents)가 0 이 되면 삭제(GC)
└── sites/
    └── {domain}/
        └── {slug}-{url_hash8}/
            └── {timestamp ISO, 콜론은 - 로}/
                ├── page.html.gz    # 단일 HTML (gzip). 큰 자원은 /resource/ 참조,
                │                   #   작은 자원(<4KB)은 data URI 인라인 유지.
                │                   #   큰 인라인 <style>(사이트 공통 CSS)도
                │                   #   /resource/*.css 로 추출해 스냅샷 간 공유
                ├── raw.html.gz     # 렌더링 후 DOM 소스 (gzip)
                ├── content.md      # 추출+정규화 텍스트
                ├── screenshot.webp # 전체 페이지 데스크탑 스크린샷 (WebP 한도
                │                   #   초과·역효과면 screenshot.png 유지 + .keep
                │                   #   마커 — 카운트 제외)
                ├── screenshot-mobile.webp # (선택) 모바일 해상도(390×844) 스크린샷 —
                │                   #   시스템 '캡처 설정'(mobile_screenshot_enabled)이
                │                   #   켜졌을 때 찍힌 스냅샷에만 있다
                ├── files/          # (구형 스냅샷만) 문서 파일 — wccg compact 가
                │                   #   문서 CAS 로 이전한다. 신규 스냅샷은 없음
                └── meta.json       # url, final_url, 시각, 해시, http 정보,
                                    #   documents 목록(문서 서빙 화이트리스트),
                                    #   origin(server|extension)·incomplete·
                                    #   capture_env(확장 캡처의 viewport·dpr·ua)
```

`wccg compact` 이전의 구형 스냅샷(page.html / raw.html / screenshot.png)도
그대로 읽힌다 — 대시보드 파일 라우트가 신/구 이름을 모두 해석한다.

URL 자체가 파일 다운로드(download.php?file=...pdf 등)면 페이지 캡처 대신
**문서 스냅샷**으로 저장된다 (capture 가 goto 의 "Download is starting" 으로
감지 → `CaptureDownloadError` → pipeline `_archive_document_url`). 파일 본체는
문서 CAS 에, 스냅샷 디렉토리에는 생성된 안내 page.html.gz + 문서 메타데이터
content.md(파일 sha256 포함 — 같은 파일이면 unchanged) + meta.json 만 남고
raw.html·스크린샷은 없다 (뷰어는 스크린샷 탭을 숨긴다). 파일명은
Content-Disposition(EUC-KR 모지바케 복구 포함) → URL 경로 → 쿼리 값 →
content-type 순으로 결정하며, 문서 화이트리스트 확장자를 못 정하면 실패.

> **문서 다운로드는 브라우저 네트워크 스택 우선, httpx 폴백.** 일부 사이트(WAF)
> 가 봇 차단으로 httpx 의 TLS ClientHello 를 핑거프린팅해 `start_tls` 단계에서
> 연결을 끊으므로(`[Errno 104] Connection reset by peer`), 문서는 Chromium 의
> 네트워크 스택으로 받아 WAF 를 통과시킨다. (1) 직접 다운로드 — 브라우저가 goto
> 중 이미 받은 파일을 `CaptureDownloadError.download_path` 로 운반해
> `documents.entry_from_local_file` 로 재요청 없이 그대로 쓴다. (2) 링크 문서 —
> `capture.fetch_documents_via_browser`(`context.request`)로 받고, 브라우저로 못
> 받은 것만 `documents.download_documents`(httpx)로 폴백한다(jwt 토큰은
> context.request 에 안 붙으므로 그런 인증 문서는 폴백이 싣는다). 두 경로 모두
> 브라우저가 불가하면 httpx 직접 다운로드(`documents.download_direct`)로 떨어진다.

## 관련 DB 테이블

- `snapshot_resources` — 스냅샷이 /resource/ CAS 로 참조하는 공유 자원
  인덱스 (CAS 이름 = sha256+확장자, 원본 url — 모를 수 있음). 캡처가
  기록하고(인라인 자원의 sha256 은 crypto.subtle, http 등 비보안 컨텍스트는
  expose_function 으로 노출된 Python hashlib 바인딩으로 폴백), 삭제 시
  참조가 0 이 된 CAS 파일은 deletion.py 가 GC 한다. 자원 인라인 실패 시
  같은 url 의 과거 캡처본을 재사용하는 폴백(pipeline._resource_fallback)의
  조회 인덱스이기도 하다. 참조가 기록되지 않은 구형 스냅샷은 저장공간
  최적화(compact)의 백필이 채우고, 인라인 <style> 이 추출되지 않은 구형
  스냅샷(snapshots.css_externalized=0)은 같은 최적화가 공통 CSS 를
  /resource/*.css 로 추출한다
- `snapshot_documents` — 스냅샷의 문서 파일 참조 (url·정제 파일명·bytes·
  sha256·content_type). 파일 본체는 문서 CAS — 같은 sha256 은 한 번만
  저장되고, 삭제 시 참조가 0 이 된 CAS 파일은 deletion.py 가 GC 한다.
  대시보드 `/documents` 통합 목록의 데이터 소스

## S3 객체 저장소 백엔드 (blobstore.py)

blob 입출력은 **StorageBackend 인터페이스**(blobstore.py)를 경유한다 — 코어
(storage·resources·documents)·서빙·export/import 가 직접 파일 I/O 대신
`config.blob_store()` 를 쓴다. 구현은 `LocalBlobStore`(기본)와 `S3BlobStore`(boto3,
endpoint_url + path-style). 인터페이스는 함부로 바꾸지 말 것.

- **S3 대상은 blob 만**(`sites/`·`resources/`·`documents/`). `index.db`(SQLite WAL)·
  `cache/`·read-through 캐시(`blobcache/`)는 **항상 로컬**. DB 는 S3 에 두지 않는다.
- **활성 백엔드 = DB 설정 `storage_backend`**(`local`|`s3`, 기본 local). env
  `WCCG_S3_*` 는 **가용성/자격증명**일 뿐 활성 백엔드를 바꾸지 않는다(데이터 접근
  불가 상태로의 무단 전환 방지). 전환은 **마이그레이션 0실패 완료** 또는 **첫 구동
  setup** 으로만. `storage_backend=s3` 인데 env 불완전 시 부팅 실패. 비밀값(키)은
  env 전용 — DB·로그·예외·응답에 출력 금지. `config.active_backend()` 로 모드 판정.
- **서빙은 원칙 5 유지** — presigned 금지. 존재 확인은 HEAD(비다운로드), 서빙
  시점에만 `local_path()` 로 blob 을 read-through 캐시에 materialize 해 그 로컬
  경로로 기존과 동일한 FileResponse(헤더·gzip·attachment·Range·CSP sandbox).
  blob 은 불변이라 캐시 무효화 없음(LRU 용량 제거만).
- **양방향 마이그레이션**(storage_migration.py): 매니페스트 → 파일 단위 copy
  (업로드 sha256 체크섬) → 존재+크기 검증 → 파일당 3회 재시도 → **0실패에서만
  완료·전환**. 진행 중 캡처·스케줄·크롤 일시중지(`db.writes_paused`), 읽기 유지.
  원본 자동삭제 금지(수동 정리). `put_verified`(로컬 원자적, S3 ChecksumSHA256).
- **S3 DB 백업**(db_backup.py): `index.db`+`rules.json` 일관복사 → tar.gz 단일
  객체로 `<prefix>/db-backups/<UTC>.tar.gz`(체크섬). 정기(스케줄러)+즉시, 보존 개수
  rotation(최신 보존). S3 모드 전체 백업의 대체 내구성.
- **첫 구동 분기·복구**(recovery.py): 사용자0+blob 시 — db-backup 있으면 복원(완전),
  없으면 복구모드(blob→인덱스 재구축, 부분). ⚠ **복구 스냅샷은 meta 에 없는
  authenticated 를 보수적으로 1(관리자 전용)로 전수 설정** — DEFAULT 0 에 기대면
  비공개 스냅샷이 노출되는 사고. 공개 전환은 관리자 명시 선택(전체/개별)으로만.
- **온디맨드 사용량**(storage_usage.py): 요청 경로·화면 진입은 **S3 자동 호출
  금지** — 캐시(DB 설정)만 읽는다. ListObjectsV2 스캔은 [업데이트]/`--scan` 명시
  트리거에서만(카테고리별 합산·시각 캐시). 로컬 사용량(db+cache+blobcache)은
  `storage.local_usage()` 가 S3 없이 계산, S3 모드 `archive_disk_usage` 도 로컬 분해만.
- **full backup 차단 / export 유지**: S3 모드에서 `create_backup`/`restore_backup`
  은 `_require_local_mode` 로 비활성(blob 로컬 부재로 일관 백업 불가). `export`/
  `import` 는 S3 모드에서도 동작 — export 가 blob 을 백엔드 read 로 스트리밍(로컬과
  동일 tar 구조), import 는 백엔드 쓰기 경로로 기록(S3 면 S3). cross-mode 호환.

## 삭제·휴지통 (deletion.py)

페이지·사이트 삭제는 기본적으로 **휴지통(소프트 삭제)**으로 간다 — `deletion.delete_page`/
`delete_site` 가 `trash_enabled`(시스템 설정, 기본 on)이고 `hard=False` 면 즉시 지우지 않고
`trash_entries` 항목을 만들고 연결 행의 `trash_id` 를 세팅한다(파일·CAS·CAS 참조행 모두
보존). `trash_enabled` off 또는 `hard=True`(CLI `--hard`)면 종전처럼 즉시 영구삭제. 단일
스냅샷 삭제(`delete_snapshot`·CLI `--snapshot`)는 휴지통을 거치지 않고 항상 즉시 삭제(범위
밖). 복원(`restore`)은 `trash_id` 만 되돌리고, 영구삭제(`purge`/`purge_expired`)·즉시삭제는
**DB 확정(커밋) → 파일 삭제** 순서로 기존 하드삭제 기구(고아 문서/자원 CAS GC·FTS·diff
캐시·`prune_site_if_empty`)를 그대로 쓴다. 휴지통에 머무는 동안 CAS 참조행이 남아 공유
CAS 가 GC 되지 않으므로(저장공간 최적화의 고아 정리도 안전), 영구삭제 때 비로소 GC 된다.
테이블·숨김 표면·자동 purge 상세는 `.claude/rules/database.md` 의 `trash_entries` 참조.

> /resource/ 공유 자원 CAS 와 문서 CAS 의 **서빙·보안**(인증 예외 경로·미디어
> 타입 화이트리스트·인증 라우트) 규칙은 원칙 5 — `.claude/rules/dashboard.md` 참조.
