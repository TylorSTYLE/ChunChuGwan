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
                            #   documents 목록 (문서 CAS 참조 — 출처 URL·해시)
```

문서 파일은 페이지당 최대 20개·개당 50MB 까지 저장하며, 링크 확장자가
문서 화이트리스트(pdf, doc(x), ppt(x), xls(x), hwp(x), odt/odp/ods, rtf,
pages, key, numbers, epub, zip)에 있을 때만 받는다. 응답이 HTML(로그인·오류
페이지)이면 건너뛰고, 다운로드 실패는 페이지 아카이빙을 막지 않는다.
같은 내용(sha256)의 문서는 documents/ CAS 에 한 번만 저장되고 스냅샷은
참조만 가진다 — 참조하는 스냅샷이 모두 삭제되면 파일도 함께 삭제된다.
compact 이전의 구형 스냅샷은 문서를 스냅샷 안 files/ 디렉토리에 직접 담고
있으며, `wccg compact` 가 CAS 로 이전한다.

대시보드 **시스템** 메뉴(`/system`)의 "캡처 설정"에서 모바일 해상도
스크린샷을 켜면, 데스크탑 스크린샷 외에 같은 페이지를 모바일 뷰포트
너비(390×844)로 재배치한 `screenshot-mobile.webp` 를 한 장 더 저장한다
(반응형 레이아웃 확인용). 기본은 꺼짐이며, 설정을 켠 뒤 새로 만들어지는
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
uv run wccg backup [dest]                  # 전체 백업 tar.gz (DB·인증 데이터·스냅샷 파일·rules.json)
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
- `dest` 를 생략하면 현재 디렉토리에 `chunchugwan-{backup|export}-{시각}.tar.gz`
  로 생성된다.
- 대시보드의 **시스템** 메뉴(`/system`)에서도 같은 기능을 쓸 수 있다 — 백업·
  내보내기는 파일 다운로드, 복원·가져오기는 파일 업로드. 백업에 인증 데이터가
  포함되므로 인증이 켜진 환경에서는 관리자만 접근할 수 있다.
