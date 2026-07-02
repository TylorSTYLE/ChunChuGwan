# 대시보드 화면 구성 (참조)

화면 24개의 라우트·권한·세부 동작 레퍼런스. UI 스타일·렌더링 보안·i18n 등
작업 시 지켜야 하는 설계 원칙은 아래 [설계 원칙 (§11)](#설계-원칙-11) 절과
정본인 [`.claude/rules/dashboard.md`](../.claude/rules/dashboard.md) 를 따른다.

> **C2 컷오버 이후**: 대시보드는 **SvelteKit SPA**(`frontend/`)다. 아래 화면 라우트
> 경로는 SvelteKit 클라이언트 라우트다. 로그·아카이브 화면은 `/log/*`·`/archive/*`
> 아래로 묶이고, 페이지·스냅샷은 사이트 계층(`/archive/sites/{id}/page/{pageId}/snapshot/{snapId}`)
> 으로 중첩한다. 백엔드 자산·API 경로(`/api/web/*`·`/snapshot/{id}/file`·인증서 pem 등)는
> 그대로다. 이제 클라이언트가
> 렌더하며, 데이터는 `/api/web/*` JSON API 가 내려준다. SPA 화면 컴포넌트는
> `frontend/src/routes/`, 백엔드 데이터 엔드포인트는 `web_api_routes`·`web_auth_routes`.
> 아키텍처 상세는 `.claude/rules/dashboard.md` 참조.

## 설계 원칙 (§11)

대시보드 UI 의 정본 설계 규칙은 [`.claude/rules/dashboard.md`](../.claude/rules/dashboard.md)
에 있다. 아래는 `web/**`·`frontend/**`·`differ.py` 를 만질 때 지켜야 하는 요약이다.

- **렌더링·서빙 보안 (아키텍처 원칙 5).** 대시보드는 기본 loopback(127.0.0.1)
  바인딩이고 외부 노출 시 인증이 필수다. 아카이빙된 HTML 은 항상 `<iframe sandbox>`
  안에서만 렌더하며 스크립트를 실행하지 않는다 — `allow-scripts`·`allow-same-origin`
  은 절대 넣지 않고, 허용하는 유일한 토큰은 사용자 클릭 네비게이션용
  `allow-top-navigation-by-user-activation` 이다. `/resource/`(공유 자원 CAS)만
  유일한 인증 예외로, sha256 콘텐츠 주소 이름 + 미디어 타입 화이트리스트 + CSP
  sandbox 로만 서빙한다. 문서 파일은 인증 걸린 라우트에서 첨부 다운로드로만 내려준다.
- **색은 시맨틱 토큰만, 컴포넌트는 재사용.** Tailwind v4 + shadcn-svelte 기반으로,
  `app.css` 의 시맨틱 토큰만 쓰고 직접 hex 는 금지한다(다크모드는 mode-watcher 가
  토큰을 치환). UI 는 `frontend/src/lib/components/ui` 프리미티브와 공통 래퍼를 우선
  재사용하고 일회성 스타일만 Tailwind 유틸로 쓴다.
- **다국어는 카탈로그 정본.** 문자열은 `web/i18n.py` 카탈로그(한국어 원문=키)를
  정본으로 하고, 새 SPA 문자열·라우트 오류 메시지를 추가하면 en 카탈로그도 채운다
  (`tests/test_i18n.py` 가 누락을 검사). CLI 는 한국어를 유지한다.
- **diff 뷰.** 텍스트 side-by-side + 스크린샷 비교를 제공하되, 비교 대상 중 하나라도
  확장(브라우저) 캡처면 스크린샷 비교를 숨기고 렌더 환경 차이를 경고한다.

**헤더(`frontend/src/routes/+layout.svelte`)** 는 모든 화면 상단에 공통으로 뜬다 —
로고(현황 링크) 옆에 권한별 메뉴 드롭다운 3개(**"아카이브"**: 새 아카이빙·아카이브 사이트
목록·전체 문서(파일)·스케줄(+사람 확인), **"로그"**: 아카이브 로그·시스템 로그,
**"설정"**: 사용자 관리·권한 그룹·API Key 관리·시스템 설정 — 하위 항목이 하나라도
보일 때만 그룹이 뜬다), 가운데에 **구글 스타일 전역 검색 박스**(둥근 입력창,
`can_search`= viewer 이상에게만 노출), 우측에 크롬 확장 안내 팝오버·테마 토글·개인설정
드롭다운이 놓인다. 검색 박스는 제출 시 **별도 검색 결과 화면**(`/search?q=…`)으로 이동한다
(도메인·최신 필터는 그 화면에서 더한다). 좁은 화면(<1024px)에서는 메뉴 그룹이 ☰ 토글
안의 세로 아코디언으로, 검색 박스는 헤더 다음 줄 풀폭으로 내려간다. 헤더 드롭다운은
한 번에 하나만 열린다(네이티브 `<details name>` 배타 + 바깥 클릭 시 닫힘).
헤더 우측의 이메일/표시이름을 누르면 **개인설정 드롭다운**이 열려
계정·개인 API Key·내 아카이브·로그아웃으로 이동한다.

**업데이트 안내 모달(`frontend/src/lib/components/UpdateNoticeModal.svelte`)** 은
로그인(승인 대기 제외) 후 첫 진입 시 현재 실행 중인 앱 버전의 릴리스 노트를 화면
가운데 모달로 1회 띄운다(닫기 버튼·× ·Esc·배경 클릭으로 닫음). 표시 내용은 **GitHub
Release 기준**이다 — 릴리스 시 CI(`release.yml`)가 그 버전의 릴리즈 노트를 받아
수정자(`@user`)·원본 링크를 빼고 **PR 번호만** 남기도록 변환해
`chunchugwan/web/release_notes.json`(버전 → `items[{text, pr, url}]`)으로 이미지에
동봉한다(`release_notes.parse_github_notes` + `scripts/gen_release_notes.py`). `/api/web/me`
가 현재 버전(`__version__`)의 항목을 그대로 내려주고(제목 `"{버전} 새 소식"`만 로케일
처리 — 항목 본문은 PR 제목 원문) 모달이 각 항목 끝에 `#번호` PR 링크를 건다. 해당 버전
항목이 없으면 노트를 표시하지 않는다(**런타임 외부 호출 없음** — JSON 만 읽음). "봤음"은
브라우저 `localStorage`(`wccg-seen-update` = 마지막으로 본 버전)로만 추적해 같은 버전은
다시 뜨지 않고 새 버전이면 다시 뜬다(닫을 때 현재 버전을 기록). 콘텐츠 자동화 상세는
`.claude/rules/release-docker.md` 참조.

- **현황** (dashboard) — 첫 화면 `/`(= `/dashboard`). 본문은 사이트·
  페이지·스냅샷 수, 총 용량(실제 저장공간 — DB·스냅샷·자원/문서 CAS 합,
  시스템 화면의 저장 용량 분해와 같은 기준 `storage.archive_disk_usage`),
  기간별 용량 트렌드(오늘/이번 주/이번 달/올해 — 스냅샷 디렉토리 합산),
  최근 스냅샷·최근 로그. 사설 대역 페이지의 행에는 URL 앞에 로컬 네트워크
  태그 뱃지가 붙는다.
- **목록** (index) — `/archive/list`, 헤더 "아카이브" 드롭다운의 "아카이브 사이트 목록". 사이트(서브도메인) 단위
  목록 — 단일 페이지 아카이브든 크롤이든 같은 서브도메인이면 한 행이다
  (www 와 apex 동일 취급). 행마다 사이트 키(상세 링크)와 사이트 타이틀
  (최신 스냅샷 meta.json 의 title, 없으면 직전 스냅샷으로 폴백 — 최대
  5개)·페이지/스냅샷/크롤 회차 수·저장 용량(스냅샷 디렉토리 합산)·마지막
  활동·자동(스케줄 수)·진행 중 뱃지. 사이트 키(도메인) 부분 일치 텍스트
  필터(`?q=`)는 서버 측에서 전체 사이트를 거른다(현재 페이지만 거르는 게 아님) —
  표시 개수 셀렉터(`?limit=` 10/25/50/100, 기본 25)·페이징(`?page=`)과 함께.
  소속 페이지·크롤이 로컬 네트워크 태그를 참조하면 사이트 키
  옆에 태그 뱃지(`_network_tag.html` 매크로, 툴팁에 설명·ID)를 보여준다 —
  같은 IP 대역의 다른 사설 네트워크 구분용 (`db.list_site_network_tags`).
  진행 중 사이트(아카이빙 중 페이지나 진행 중 크롤 보유) 상단 고정, 아직
  pages 행이 없는 신규 URL 진행 건은 임시 사이트 행으로 보인다. 진행 중
  항목이 있으면 `/archive/active` 와 `/crawls/{id}/status` 를 폴링해 자동
  갱신. 구 `/crawls` 목록은 `/archive/list` 로 301 리다이렉트.
- **사이트 상세** (site) — `/archive/sites/{id}`. 사이트에 속한 페이지 목록(스냅샷
  수·저장 용량·마지막 활동·자동 주기·재아카이빙/삭제 버튼 — 목록 화면에
  있던 페이지 행 동작이 여기로 왔다, 페이징 `?page=`. 표시 개수는 페이저의
  셀렉터 `?per_page=` 로 10/25/50/100 중 선택, 기본 10 — 허용 밖 값은
  기본으로 보정되고 이전/다음 링크가 선택을 유지한다)과 크롤
  회차 목록(`/crawls/{id}` 링크, 상태·실패 뱃지, 회차마다 "다시 아카이빙"
  `POST /sites/{id}/crawls/{cid}/rerun` — admin/archiver 전용, 같은 시작
  URL·범위·옵션으로 새 크롤을 만들어 진행 화면으로 보낸다, 같은 시작 URL 의
  크롤이 진행 중이면 자동 병합. 실패 페이지가 있는 회차에는 "실패 일괄
  재시도" `POST /crawls/{id}/retry` 도 함께 — 회차 상세와 같은 라우트로,
  실패 페이지만 큐로 되돌려 다시 시도하고(성공 페이지는 보존) 회차 상세로
  보낸다), 크롤 스케줄 요약. 페이지·회차·실패한 작업 표는 각자 독립
  페이징하며(표시 개수 셀렉터 10/25/50/100, 기본 10) 좁은 화면에서 카드형으로 전환된다.
  헤더에
  사이트 타이틀(목록 화면과 같은 기준)과 페이지·스냅샷 총수, 사이트 저장
  용량(스냅샷 디렉토리 합산), 소속 페이지·크롤이 참조하는 로컬 네트워크
  태그 뱃지를 보여준다 — 사설 대역 페이지/크롤 행에는 각자의 태그 뱃지도
  URL 옆에 붙는다. 실패한 작업 섹션 — URL 별 최신
  아카이브 로그가 실패(error)인 페이지 목록(시각·출처·오류)과 재시도 버튼
  (`POST /sites/{id}/failed/{log_id}/retry`, admin/archiver 전용 — 백그라운드
  재아카이빙). 재시도가 성공하면 최신 로그가 성공으로 바뀌어 목록에서
  자연히 사라진다. 같은 표에 크롤 실패 페이지도 보인다 — 사이트 소속
  크롤에서 URL 별 최신 crawl_pages 행이 failed 인 것(페이지 행이 생기기
  전에 실패한 신규 URL 포함, 아카이브 로그 실패 목록과 겹치는 URL ·이후
  직접 아카이빙이 성공한 URL 은 제외). 출처는 크롤 회차 링크, 재시도는
  `POST /sites/{id}/crawl-failed/{cp_id}/retry` (admin/archiver 전용) — 행을
  pending 으로 되돌려 크롤러가 다시 집어가고, 끝난 크롤이면 다시 연다.
  직접 실패·크롤 실패를 시각 내림차순으로 합쳐 페이지 목록처럼 독립
  페이징하고(`?fpage=`/`?fper=`), 도구막대의 "모두 재시도"
  (`POST /sites/{id}/failed/retry-all`, admin/archiver 전용)는 보이는 실패를
  한 번에 — 직접 실패는 아카이빙 큐에, 크롤 실패는 큐로 되돌려 재처리한다.
  사이트 내보내기(`POST /sites/{id}/export`, admin/archiver 전용)는 소속
  페이지·스냅샷·확인 기록·크롤 회차·사이트 인증서·아카이브 로그와 참조
  중인 공유 자원·문서 CAS 파일만 담은 `.ccg.export` 파일을 다운로드한다 — 형식은
  전체 내보내기(`/system/export`)와 같아 가져오기(웹 화면·`wccg import`)로
  복원할 수 있고, 파일명에 사이트 키가 붙는다. 페이지 행이 생기기 전에
  실패한 로그(page_id NULL)와 로컬 네트워크 태그는 옮겨지지 않는다. 사이트 삭제(`POST /sites/{id}/delete`,
  admin/archiver 전용)는 소속 페이지·크롤 회차·크롤 스케줄을 일괄 삭제 —
  소속 페이지가 아카이빙 중이거나 크롤이 진행 중이면 거부. 진행 중 작업
  폴링은 목록 화면과 동일. 인증서 섹션 — https 아카이빙 때 받은 서버
  인증서의 버전 이력을 호스트별 카드로 펼쳐 보여준다(주체·발급자·대체
  이름(SAN)·유효 기간 `not_before ~ not_after`·확인 기간 `first ~ last_seen`·
  일련번호·서명 알고리즘·지문 sha256). 현재/이전 버전·검증 안 됨 뱃지에
  더해 만료 시각 기준 만료됨/곧 만료(30일 이내) 뱃지를 단다(임박 판정은
  클라이언트 현재 시각). PEM 다운로드는
  `GET /sites/{id}/certificates/{cert_id}.pem` — 소속 행만, 항상 첨부파일.
  문서 섹션 — 이 사이트의 페이지가 링크한 문서 파일을 sha256 그룹으로
  보여준다(문서명·형식·용량·출처 페이지·참조 스냅샷·마지막 저장). 전체
  문서 화면(`/archive/documents`)의 사이트 스코프 버전(`db.list_site_document_groups`)
  으로, 다운로드 링크는 같은 `GET /document/{sha256}/{file}` 를 쓴다. 문서가
  없으면 섹션을 숨기고, 페이지 목록처럼 독립 페이징한다(`?dpage=`/`?dper=`).
- **사이트 로그인 자격증명** (site_credentials) — `/archive/sites/{id}/credentials`,
  관리자 전용 (헤더 메뉴 없이 사이트 상세 도구막대의 "로그인 자격증명"
  링크로 진입). 아카이빙 대상 사이트에 로그인이 필요할 때 쓸 자격증명을
  등록·삭제한다. 종류(`kind` 셀렉터)는 HTTP 기본 인증(아이디·비밀번호),
  세션 쿠키(storage_state JSON), JWT(Bearer 토큰) 셋. 세션 쿠키는 JSON 을
  직접 붙여넣는 대신 로그인 상태로 기록한 **HAR 파일**(브라우저 개발자도구
  네트워크 탭 → 내보내기)을 올려 쿠키를 자동 추출할 수도 있다
  (`credentials.storage_state_from_har` — 대상 사이트의 등록 도메인 쿠키만
  남기고 무관한 서드파티 쿠키는 버린다, localStorage 는 포함 안 됨, HAR 을
  올리면 JSON 입력은 무시). 비밀은
  `WCCG_SECRET_KEY` 로 대칭 암호화해
  저장하고(`crypto.py`·`credentials.py`·`site_credentials` 테이블) 화면에는
  라벨·종류·만든 사람·등록 시각만 보인다 — 비밀 평문은 다시 표시하지 않는다.
  `WCCG_SECRET_KEY` 미설정 시 등록 폼이 비활성화되고 경고를 띄운다. 목록 +
  등록(`POST /sites/{id}/credentials`)·삭제
  (`POST /sites/{id}/credentials/{cid}/delete`), 라벨은 사이트 안에서 UNIQUE.
  사이트 prune·삭제 시 함께 정리된다. 캡처 연동(아카이빙 시 실제 로그인
  사용)은 이후 단계.
- **문서** (documents) — `/archive/documents`, 헤더 "아카이브" 드롭다운의 "전체 문서(파일)". 아카이브된 페이지들이
  링크한 문서 파일의 통합 목록. 같은 내용(sha256)은 한 행으로 묶어 표시 —
  대표 파일명(가장 최근 참조)·형식·용량·사이트(상세 링크)·출처 페이지(+외
  N곳)·참조 스냅샷 수·마지막 저장 시각, 100개 단위 페이징. 다운로드는
  `GET /document/{sha256}/{file}` — snapshot_documents 에 기록된 조합만
  문서 CAS 에서 항상 첨부파일로 서빙. compact 전 스냅샷(files/)의 문서가
  남아 있으면 압축 실행 안내를 띄운다 (압축하면 목록에 포함 + 중복 제거).
- **휴지통** (trash) — `/archive/trash`, 헤더 "아카이브" 드롭다운의 "휴지통",
  휴지통 관리(`manage_trash`) 권한 전용 (기본 admin — 권한이 없으면 메뉴에
  보이지 않고 라우트도 403). 소프트 삭제된 페이지·사이트의 목록 — 종류(페이지/
  사이트)·대상 URL·스냅샷 수·용량·삭제 시각·삭제자·보관 기한을 표로 보여준다
  (표시 개수 셀렉터 10/25/50/100, 기본 25·페이징).
  행마다 **복원**(다시 모든 목록·검색·뷰어·문서·서빙에 노출)과 **영구삭제**
  (스냅샷 파일 + 참조 0 인 공유 자원/문서 CAS 정리) 버튼을 둔다. 삭제(= 휴지통으로
  보내기)는 종전 `delete` 권한이고, 이 화면의 열람·복원·영구삭제만 `manage_trash`
  가 게이트한다. 보관 기간(`trash_retention_days`)이 지난 항목은 스케줄러가
  자동 영구삭제하며, 설정은 시스템 설정 화면의 "휴지통" 섹션에서 바꾼다.
- **검색** (search) — `/search`, 헤더의 전역 검색 박스에서 진입
  (헤더 메뉴에는 따로 두지 않는다 — 검색 박스가 단일 진입점), viewer 이상
  (`permissions.can_search` — 전문 검색은 모든 아카이브 본문 열람이라 로그와
  같은 하한). 검색어(`q`)·도메인 필터(`domain`)·"URL당 최신만"(`latest`)
  GET 파라미터. 색인 본문은 스냅샷 content.md + 첨부 문서 본문(searchindex.py),
  결과마다 매치 강조 스니펫(`highlight` 필터)·스냅샷 뷰어·타임라인 링크,
  20개 단위 페이징. 한국어는 trigram 부분문자열(3글자+), 1~2글자는 LIKE
  폴백(안내 배지). FTS5 없는 SQLite 빌드면 비활성 안내만 표시(아카이빙 영향
  없음). 색인 동기화·CLI(`wccg search`)는 docs/SEARCH.md 참조.
- **새 아카이빙** (archive_new) — `/archive/new`, 헤더 "아카이브" 드롭다운의 "새 아카이빙",
  admin/archiver 전용. URL 등록 + 자동 재아카이빙 주기 선택. 신규 URL 은 pages 행이 아카이빙
  후에 생기므로 주기 등록은 백그라운드 작업 말미에 수행.
  "사이트 전체 아카이브" 체크 시 크롤 옵션(최대 페이지·깊이·간격, 초깃값은
  시스템 설정의 기본값, 입력 상한은 "사이트 아카이브 최대값")이 펴진다 —
  `POST /archive` 가 크롤을 등록하고 진행
  화면으로 보낸다. 같은 시작 URL 의 크롤이 진행 중이면 새로 만들지 않고
  그 크롤로 자동 병합 — 기존 진행 화면으로 `?merged=1` 리다이렉트해 병합
  알림을 띄운다 (이번 옵션은 버리고 진행 중 크롤의 옵션 유지). 주기를 함께
  선택하면 같은 옵션의 크롤 스케줄(주기적 재크롤)도 등록된다 (다음 실행 =
  지금 + 주기, 첫 실행은 방금 등록한 크롤).
  로컬 네트워크 태그 선택(`network_tag`) — URL 호스트가 사설 IP 대역(RFC1918 등)
  으로 감지되면 태그 선택이 자동으로 나타나고(루프백이면 거부 안내), 시스템
  화면에서 만든 태그를 골라야 등록된다. 루프백 주소는 항상 거부된다
  (`netcheck.py` 판정, 동기 검증 후 pipeline/crawler 가 재강제. 공개 주소면
  태그 무시). 태그는 페이지/크롤/크롤 스케줄에 저장돼 스케줄 재실행에도
  이어진다. 명시적 `http://` URL 은 신규 등록 시 https 지원을 확인해
  https 로 승격해 저장한다 (미지원·자체 서명 인증서면 http 유지 —
  pipeline.upgrade_http_to_https).
  관리자에게는 **"로그인 자격증명"(선택)** 드롭다운이 보인다 — URL 을 입력하면
  그 도메인(사이트)에 등록된 자격증명을 `GET /archive/credentials?url=` 로
  비동기 조회해 옵션으로 채운다(관리자 전용 JSON, 비밀은 안 내려보냄).
  기존 자격증명을 고르면 그것을 이 페이지에 연결하고(`pages.credential_id` —
  아카이빙 시 로그인에 사용, 재아카이빙·스케줄에도 이어짐), "새 자격증명
  추가…" 를 고르면 종류(HTTP 기본 인증/세션 쿠키/JWT) 폼이 펴져 즉석에서
  등록한 뒤 연결한다 (세션 쿠키는 storage_state JSON 직접 입력만 — HAR 파일
  업로드는 사이트 상세 자격증명 화면에서 지원). 새로 만들 때 라벨을 입력해야
  하고 같은 이름이 있으면 오류로 막는다. 선택한 자격증명이 그 도메인 소속이 아니면 거부하며,
  입력·선택 오류 시 아카이빙도 진행하지 않고 비밀번호·토큰은 리다이렉트 URL
  에 싣지 않는다. 크롤(사이트 전체)은 자격증명을 `crawls.credential_id` 에
  실어 크롤러가 전 페이지에 적용한다(주기 크롤은 `crawl_schedules.credential_id`
  로 이어짐). `WCCG_SECRET_KEY` 미설정 시 경고 + 저장 거부. 비관리자(archiver)
  에겐 이 드롭다운이 보이지 않는다 (자격증명은 관리자 전용).
- **사이트 아카이브 진행** (crawl) — `/crawls/{id}`. 상태 카드(실패 재시도
  대기 — 시스템 설정값 표시, 사설 대역 크롤이면 로컬 네트워크 태그 뱃지
  행 포함) + 페이지 목록(상태 뱃지, 시도 횟수, 오류,
  스냅샷/타임라인 링크). 목록은 `?status=` (pending/in_progress/done/failed,
  잘못된 값은 전체)로 상태별 필터링 — 테이블 위 필터 링크에 상태별 개수
  표기. 진행 중이면
  `/crawls/{id}/status` JSON 을 5초 폴링해 자동 갱신. 취소(`POST
  /crawls/{id}/cancel`)·실패 일괄 재시도(`POST /crawls/{id}/retry`)·실패
  페이지 단건 재시도(`POST /crawls/{id}/pages/{crawl_page_id}/retry` —
  실패 행의 버튼, 끝난 크롤이면 다시 열고 ?status 필터를 유지한 채
  복귀)는 admin/archiver 전용. 끝난(완료·취소) 크롤이면 도구막대에 "다시
  아카이빙" — 사이트 상세 회차 목록과 같은 `POST /sites/{id}/crawls/{cid}/rerun`
  로 같은 옵션의 새 크롤을 만든다(진행 중에는 안 보인다). 링크 리졸버 `GET /crawl/{id}/goto?url=` 은 크롤
  세트의 스냅샷 → 그 URL 의 최신 스냅샷 → 미아카이브 안내(404) 순으로
  처리한다 — 크롤로 저장된 page.html 의 재작성 링크(`target="_top"`)가
  여기로 온다.
- **스케줄** (schedules) — `/archive/schedules`, 헤더 "아카이브" 드롭다운의 "스케줄". 페이지 스케줄과
  사이트 아카이브 스케줄(크롤 옵션 표시)을 섹션으로 나눠 보여주고 주기
  변경·다음 실행 변경·해제를 제공 (크롤 스케줄은 `POST
  /crawl-schedules/{id}`·`/next-run`·`/delete`). 변경/해제는 admin/archiver
  만, viewer 는 읽기 전용. 주기 입력은 프리셋 + 직접 입력(분/시간/일) +
  1일 단위 주기의 실행 시각 — `_schedule_field.html` 매크로를
  타임라인·새 아카이빙과 공용. 사설 대역 페이지/크롤 스케줄 행에는 URL 옆에
  로컬 네트워크 태그 뱃지가 붙고, URL 필터가 태그 이름도 검색한다.
- **타임라인** (timeline) — `/archive/sites/{id}/page/{pageId}`. 헤더에 소속 사이트 링크
  (`/archive/sites/{id}`)와 로컬 네트워크 태그 뱃지를 보여준다. 스냅샷 이력은
  **최신 순**으로 정렬되고 `?page=`·`?limit=`(10·25(기본)·50·100, 목록 화면 공통
  페이저) 페이지네이션이 붙는다 — `#`(idx)는 최초 아카이빙이 1번(신규 뱃지)인
  전체 기준 history 번호라 페이지가 바뀌어도 유지된다(`diff --from/--to` 번호와 일치).
  스냅샷 행마다
  저장 용량과 상세 펼침 버튼 — 로그 화면과 같은 파일 목록(용량·보기 링크)과
  실행 로그의 단계별 소요를 보여준다 (`_snapshot_detail.html` 매크로 공용).
  **페이지 메모**가 있으면 상단(스냅샷 목록 위)에 시간순으로 표시되고,
  메모 권한별로 등록/개별 삭제 UI 가 붙는다(`memo_view`=보기, `memo_create`=등록,
  `memo_delete`=삭제 — authentication.md). 사이트 메모는 사이트 상세 상단에 동일하게
  표시된다.
- **스냅샷 뷰어** (snapshot) / **diff 뷰어** (diff) — 스냅샷 뷰어의
  메타데이터 표에는 사설 대역 페이지면 로컬 네트워크 태그 뱃지 행이
  붙는다. 메타데이터 표 아래에 **재아카이빙 버튼**(+강제 옵션, `archive`
  권한)이 있어 보고 있는 URL 을 바로 다시 아카이빙할 수 있다(타임라인과 같은
  `POST /pages/{id}/rearchive`). 탭은 렌더링·데스크탑 스크린샷·텍스트가 기본이며, 시스템 "캡처
  설정"을 켜고 찍은 스냅샷처럼 모바일 해상도 스크린샷
  (`screenshot-mobile.*`)이 있으면 "모바일 스크린샷" 탭이 추가로 노출된다
  (안드로이드 크롬 모바일 브라우저로 같은 URL 을 다시 열어 390×844 로 찍은
  전체 페이지). 문서 스냅샷
  (URL 자체가 파일 다운로드인 페이지, pipeline `_archive_document_url`)은
  스크린샷이 없으므로 스크린샷 탭을 숨긴다. 렌더링 탭에는 생성된 안내
  page.html, 텍스트 탭에는 문서 메타데이터 content.md 가 보이고 파일
  본체는 첨부 문서 목록에서 내려받는다.
  확장(브라우저) 클라이언트 캡처 스냅샷(`origin=extension`)에는 "브라우저
  캡처" 뱃지(로그인 상태로 캡처되어 민감 정보가 모든 사용자에게 보일 수
  있다는 안내)와, 일부 수집이 실패했으면 "불완전" 뱃지가 붙는다. diff 뷰어는
  비교 대상 중 하나라도 확장 캡처면 **스크린샷 비교를 숨기고**(로컬 해상도·
  dpr 의존이라 무의미) 본문 diff 에 렌더 환경 차이 경고 배너를 단다. 적재된
  페이지(`pages.client_captured=1`)는 서버가 다시 가져오지 않으므로 서버
  재아카이빙·스케줄 대상에서 제외된다(갱신은 확장 재캡처).
- **아카이브 로그** (logs) — `/log/archive` (`view_archive_logs` 권한, 기본 admin), 헤더 "로그" 드롭다운의 항목. 아카이브
  실행 기록, 도메인·페이지·
  스냅샷·상태 필터 + 표시 개수 셀렉터(10/25/50/100, 기본 25)·페이징 + 단계별 상세
  펼침. viewer 이상(admin/archiver/viewer)만
  열람 — 권한이 없으면(pending) 메뉴에 보이지 않고 라우트도 403.
  사설 대역 페이지의 로그 행에는 URL 앞에 로컬 네트워크 태그 뱃지가
  붙는다 (페이지 생성 전 실패 등 page_id 없는 로그는 제외).
  실패(error) 행의 스냅샷 컬럼에는 재시도 버튼(`POST /logs/{id}/retry`,
  admin/archiver 전용)이 보인다 — 로그의 url 을 백그라운드로 다시
  아카이빙하고 필터를 유지한 채 복귀(`retry=queued|active` 알림). 같은
  URL 이 이미 진행 중이면 중복 실행하지 않는다.
- **시스템 로그** (system_logs) — `/log/system` (`view_system_logs` 권한, 기본 admin) (헤더 "로그"
  드롭다운의 항목, 관리자에게만 표시). 앱(serve·worker·CLI)의 Python logging 레코드 —
  `system_log.py` 의 DB 핸들러가 `system_logs` 테이블에 적재한 경고/오류와
  INFO 기록. 레벨·출처(serve/worker/cli)·기간 필터 + 표시 개수 셀렉터(10/25/50/100, 기본 25)·페이징 + 트레이스백 펼침.
  보관 한도(`WCCG_SYSTEM_LOG_MAX_ROWS`, 기본 2만 행) 초과분은 적재 중
  자동 정리. 감사 로그(`chunchugwan.web.audit`)는 핸들러가 제외해 여기엔 안 남고
  전용 `audit_logs` 테이블로 분리된다(아래 감사 로그 화면).
- **감사 로그** (audit) — `/log/audit` (`view_audit_logs` 권한, 기본 admin), 헤더
  "로그" 드롭다운의 항목. 누가 무엇을 했는지의 사용자 액션 기록 — `web/audit.py` 가
  요청 주체(세션 사용자 이메일 또는 API 키 이름)·액션 종류(아카이빙·열람·문서
  다운로드·관리 작업)·대상·한국어 원문을 전용 `audit_logs` 테이블에 적재한다.
  종류·요청자·기간 필터 + 표시 개수 셀렉터(10/25/50/100, 기본 25)·페이징. 아카이빙(새/재아카이빙·API), 아카이브 열람
  (스냅샷 뷰), 문서 다운로드, 관리 작업(설정·권한·자격증명·네트워크 태그·API 키
  등)이 기록된다. 시스템 로그와 분리돼 별도 권한으로 게이트한다.
- **시스템 설정** (system) — `/system/general`, 헤더 "설정" 드롭다운의 "시스템 설정" 항목, 관리자 전용.
  화면은 그룹으로 묶인다 — 시스템 상태(버전·페이지·스냅샷·사용자 + 저장용량 미터
  차트)·유지관리(검색 인덱스·저장공간 최적화)·아카이브 설정(사이트 아카이브·캡처·
  확장 자격증명·문서 아카이브·휴지통·로컬 네트워크 태그)·사용자 설정(가입·이메일 본인 인증·
  인증 보호(rate limit — 로그인·2단계·이메일 코드 시도 한도/창·전체 토글))·
  서버 환경설정(메일 SMTP·API 키 링크)·위험 구역(데이터 관리: 전체 백업/복원·
  내보내기/가져오기, 다른 춘추관으로 이전). 각 기능엔 간단한 설명이 붙는다.
  현황(페이지·스냅샷·확인 기록·사용자 수 + 춘추관
  버전 `chunchugwan.__version__`)·백업/복원·내보내기/가져오기·저장공간 최적화
  (`POST /system/compact`, `wccg compact` 와 동일 — 압축 변환 + 인라인
  스타일 추출 + 자원 참조 백필 + 고아 공유 자원 정리. 대상이 없으면 버튼
  비활성화)·검색 인덱스(`searchindex.verify` 상태 — 색인됨·미색인·과소 색인·
  orphan 표시 + `POST /system/search/reindex` "전체 다시 색인" 버튼 =
  `searchindex.reindex_all`, `wccg search reindex --all` 과 동일. FTS5
  미지원이면 비활성)·아카이브 링크 교정(`POST /system/links/repair` "아카이브
  링크 교정" 버튼 = `linkrepair.backfill_all`, `wccg links repair` 와 동일 —
  구형 단일 페이지 스냅샷의 page.html 앵커를 `/goto` 리졸버로 재작성해 렌더링 시
  깨진 내부 링크를 바로잡는다. 미교정 수·진행률 폴링 `GET /system/links/repair/status`,
  내용 보존 변환)·가입 설정(회원
  가입 허용 여부 + 가입 초기 권한
  관리자를 뺀 권한 그룹 + 권한없음 중 선택, 기본 권한없음 — `settings` 테이블)·사이트
  아카이브 설정(`POST /system/crawl-settings` — 크롤 기본 옵션 3종(최대 페이지·
  깊이·간격) / `POST /system/crawl-limits` — 그 상한(최대값) 3종 + 실패 재시도
  대기 쉼표 목록, 대기 횟수 + 1 = 페이지당 최대 시도. 기본값은 상한 이내로
  클램프되고 새 크롤 등록도 상한을 넘으면 거부. `settings` 테이블, 재시도
  대기는 진행 중 크롤에도 즉시 적용)·캡처 설정
  (`POST /system/capture-settings` — 모바일 해상도(390×844) 스크린샷도 함께
  저장할지. 켜면 같은 URL 을 안드로이드 크롬 모바일 컨텍스트로 한 번 더 열어
  캡처한다. `settings` 테이블 `mobile_screenshot_enabled`, 기본 꺼짐, 켠 뒤
  새로 만들어지는 스냅샷에만 적용)·문서 아카이브 설정
  (`POST /system/document-settings` — 페이지가 링크한 문서를 받을 때의 한도:
  스냅샷당 문서 수·문서 1개 크기(MB)·다운로드 타임아웃(초). `settings` 테이블
  `document_max_count`/`document_max_mb`/`document_fetch_timeout_seconds`,
  값 해석·클램핑은 `documents.limits`, 이후 저장되는 스냅샷에 적용)·휴지통 설정
  (`POST /system/trash-settings` — 삭제 시 휴지통 사용 여부(`trash_enabled`,
  기본 on, 끄면 삭제가 즉시 영구삭제)와 보관 기간 일수(`trash_retention_days`,
  기본 30일, 0 = 자동삭제 끔). `settings` 테이블, 기한 지난 항목은 스케줄러가
  자동 영구삭제. 휴지통 목록·복원·영구삭제는 별도 `/archive/trash` 화면)·메일(SMTP)
  설정(`POST /system/smtp-settings` — 초대 메일 발송 SMTP 호스트·포트·TLS·로그인
  사용자/비밀번호·발신자. `settings` 테이블, `WCCG_SMTP_*` 환경변수보다 우선하고
  없는 항목만 환경변수로 폴백. 비밀번호는 `WCCG_SECRET_KEY` 로 암호화한 암호문만
  저장하고 화면엔 노출하지 않으며, 입력칸을 비우면 유지·'저장된 비밀번호 삭제'로
  제거. `POST /system/smtp-test` 는 저장된 설정으로 관리자 본인에게 테스트 메일을
  보낸다)·이메일 본인 인증 설정
  (`POST /system/email-verification-settings` — 사용자 설정 섹션. 패스워드
  계정이 메일로 받은 코드로 이메일을 검증하게 할지(`settings` 테이블
  `email_verification_enabled`, 기본 꺼짐)와 코드 만료 시간(분,
  `email_verification_ttl_minutes`). SMTP 미설정이면 켜도 동작하지 않고 SSO
  계정은 제외 — 인증 흐름은 `docs/AUTHENTICATION.md` 참조)·로컬 네트워크 태그
  (`POST /system/network-tags`, `POST /system/network-tags/{id}/delete`,
  `POST /system/network-tags/merge` — 사설 IP 대역 아카이빙을 허용하는 태그.
  id 는 GUID 자동 발급, 이름은 유일·60자, 설명 200자. 페이지·크롤·크롤
  스케줄이 참조 중이면 삭제 거부. 병합은 출처(source)·대상(target) 두 태그가
  같은 사설 IP·포트(= 같은 site_id) 집합을 가리킬 때만 허용 — 출처의 참조를
  대상으로 옮기고 출처를 삭제한다. 폼은 태그가 2개 이상일 때만 보인다)·
  다른 춘추관으로 이전(`POST /system/migration/enable`·`/regenerate`·`/disable` —
  이전 모드를 켜면 1회용 인증 토큰을 발급하고(원문은 1회만 표시) 그 동안 모든
  스크래핑·스케줄·크롤이 중단된다. 받는 쪽이 최초 설정 화면에서 이 서버 주소 +
  토큰으로 데이터를 가져간다. 토큰은 SHA-256 해시만 저장 — 모드를 끄면 무효화·
  스크래핑 재개. 상세는 `docs/AUTHENTICATION.md`).
  백업에 인증 데이터가 포함되므로 인증이 켜진 환경에서는 관리자 전용.
- **사용자** (users) — `/system/users`, 헤더 "설정" 드롭다운의 "사용자 관리" 항목,
  사용자 관리(`manage_users`) 권한 전용. **권한은 역할(프리셋) 단위로만 부여**한다
  (권한없음 가입자 승인 = 권한 부여) — 사용자별 세분 권한 편집 UI·라우트는 제거됐고
  화면엔 권한 열도 없다(권한 묶음 내용은 권한 그룹 화면에서 편집). 사용자 목록은
  표시 개수 셀렉터(10/25/50/100, 기본 25)로 페이징한다(초대 목록은 전체). 각 행에서
  **표시이름**을 직접 편집(`POST /system/users/{id}/name`)할 수 있다. `manage_users`
  마지막 보유자에게서는 그 권한을 떼거나 역할을 낮출 수 없다(관리 잠김 방지).
  차단 시 세션 즉시 무효화, 최초 관리자는 변경 불가. 계정별로 2FA·SSO와 함께
  **이메일 인증 여부**(인증됨/미인증, SSO 계정은 `-`)를 표시한다.
  계정 정보 삭제(`POST /system/users/{id}/delete`) — 대상 이메일을 입력해
  확인해야 하며, 최초 관리자·본인은 불가. 삭제하면 세션·OIDC 연결·패스키까지
  지워져 같은 이메일로 재가입/초대가 가능해진다. 탈퇴(withdrawn) 계정은
  본인이 계정 설정 위험영역(`POST /settings/account/withdraw`, 패스워드
  또는 SSO 는 이메일 확인)에서 만든 상태로 로그인이 거부되며, 권한 변경
  대상이 아니고 삭제만 가능.
  화면 하단 **초대** 섹션에서 이메일·역할로 초대(`POST /system/users/invite`)를
  발급한다 — SMTP 가 켜져 있으면 초대 메일을 보내고, 아니면 링크를 화면에 노출해
  직접 전달한다(`config.INVITE_TTL_DAYS`, 기본 7일 만료). 초대 목록은 각 초대의
  **만료 시각**(만료된 건 "만료됨" 뱃지)을 표시하고, 행마다 **재생성**
  (`POST /system/users/invite/{id}/regenerate` — 같은 이메일·역할로 새 토큰 발급,
  이전 링크 무효·TTL 리셋, 만료된 초대도 가능)과 **취소**
  (`POST /system/users/invite/{id}/delete`) 버튼을 둔다. 만료된 초대도 TTL 기간
  동안은 목록에 남겨 재생성할 수 있고, 그보다 오래되면 기회적으로 정리된다.
- **권한 그룹** (groups) — `/system/groups`, 헤더 "설정" 드롭다운의 "권한 그룹" 항목,
  시스템 관리(`manage_system`) 권한 전용. 역할 프리셋(권한 묶음)을 코드 배포
  없이 편집한다. 그룹마다 세분 권한 체크박스로 기본 권한을 정하고
  (`POST /system/groups/{name}`), 새 커스텀 그룹을 추가(`POST /system/groups` —
  이름은 영문 소문자·숫자·밑줄, `db.normalize_group_name` 검증)하거나
  삭제(`POST /system/groups/{name}/delete`)한다. 빌트인(관리자·아카이브·보기
  전용)은 권한 묶음만 편집 가능하고 이름·삭제는 잠김. **소속 사용자가 있는
  그룹은 삭제 거부**(먼저 사용자 역할을 옮겨야 함), 그룹 권한 편집이
  `manage_users` 보유 활성 계정을 0 으로 만들면 거부(관리 잠김 방지 —
  새 프리셋으로 시뮬레이션). 그룹 권한을 바꾸면 소속 사용자에게 즉시 반영되되
  사용자별 오버라이드는 유지된다. pending/blocked/withdrawn 은 접근 게이트
  상태라 여기서 다루지 않는다.
- **API 키** (api_keys) — `/system/api-keys`, 헤더 "설정" 드롭다운의 "API Key 관리"
  항목(`manage_users` 권한). 외부 소프트웨어용 **시스템 키**(owner=NULL)
  발급·폐기. 권한(보기/아카이브 + **클러스터 보내기/받기**)과 만료(영구·1일·1개월·
  1년·사용자 지정 일) 선택, 키 원문은 발급 직후 1회만 표시. 클러스터 권한 키는
  `/api/cluster/*` 채널 전용(아래 "클러스터" 화면 참조). 개인용 키는 아래 "개인 API Key" 화면.
- **클러스터** (cluster) — `/system/cluster`, 헤더 "설정" 드롭다운의 "클러스터" 항목
  (`manage_system` 권한). 여러 인스턴스를 연결해 아카이브를 선택적으로 주고받는
  federation 관리. **이 노드**(UUID 표시·표시 이름 편집 `POST /system/cluster/node`),
  **동기화 설정**(조정 주기·시스템 보호 기본값 `POST /system/cluster/sync-settings`),
  **피어 연결**(주소+발급 키+방향으로 추가 `POST /system/cluster/peers` — 핸드셰이크로
  피어 UUID·버전 획득, 자기연결·중복 거부, 키는 `WCCG_SECRET_KEY` 암호화 저장),
  방향 토글(`POST /system/cluster/peers/{id}`)·연결 해제(`/delete`)와 피어별 상태
  (연결됨/대기/일시 오류/오류/폐기됨)·마지막 동기화를 보여준다. `WCCG_SECRET_KEY`
  미설정 시 피어 등록이 막히고 경고를 띄운다. 상세는 `docs/CLUSTER.md`.
- **개인 API Key** (personal_api_keys) — `/settings/api-keys`, 로그인 사용자
  본인용 (개인설정 드롭다운에서 진입). **`use_api_keys` 권한 전용** — 없으면
  메뉴·계정 링크가 숨고 화면 직접 접근도 403, 토큰 사용도 401(빌트인 기본은
  admin·archive_manager·archiver 보유, viewer 제외). 크롬 확장 등이
  `Authorization: Bearer` 로 `/api/v1` 에 접근할 때 쓰는 본인 귀속
  토큰(owner_user_id=본인)을 발급·폐기. 권한(보기/아카이브)은 발급 시 선택하되
  내 역할 범위로 클램프(보기=`view`↑, 아카이브=`archive`↑ 만 부여, 그 이상은
  무시), 발급
  (`POST /settings/api-keys`)·폐기(`POST /settings/api-keys/{id}/delete`)는
  본인 토큰만(IDOR 방어, 타인·시스템 키는 404). 원문은 발급 직후 1회만 표시.
  발급 폼 앵커 `#ext-token-form` — 확장이 미연결 시 이 화면을 연다.
- **내 아카이브** (my_archives) — `/settings/archives`, 로그인 사용자 본인용
  (개인설정 드롭다운에서 진입). 본인이 대시보드·크롬 확장으로 **직접 요청한**
  단발 아카이빙 실행 이력(`archive_logs.requested_by` = 본인)을 최신순으로
  보여준다 — 시각·상태·URL·HTTP·출처·스냅샷 링크 + 상태 필터·페이징.
  예약(schedule)·사이트 전체(crawl)·CLI 실행은 requested_by 가 NULL 이라
  포함되지 않는다. 인증 off(loopback)에는 '본인'이 없어 빈 목록.
- **사람 확인 필요** (needs_human) — `/archive/needs-human`, 관리자 전용. 처리할
  작업이 있을 때만 헤더 "아카이브" 드롭다운에 배지와 함께 항목이 뜬다(`needs_human_count`).
  자동(스텔스 엔진)으로 통과하지 못한 인터랙티브 챌린지가 있으면 사람이 직접
  풀어 통과시킨다. **중요: 대시보드의 모든 안내는 워커가 DB 에 기록한 needs_human
  사실에만 의존하며, 대시보드(serve) 프로세스의 `WCCG_LIVE_CHALLENGE` 설정과
  무관하다** — 워커와 serve 가 다른 프로세스·env 라도(serve 엔 그 플래그가 없어도)
  대기 작업이 누락 없이 보인다. 발견성을 위해 관리자에게 대기 작업이 있을 때
  네 군데서 안내한다: (1) **목록 화면의 상태 배지** — 진행 중 작업이 사람 대기로
  전환되면 `/archive/list`·사이트 상세의 "아카이빙 중" 배지가 라이브 화면으로 가는
  "사람 확인 대기"(amber 알림) 링크로 바뀐다. 서버 렌더라 JS·브라우저 설정과
  무관하게 보이고, 진행 폴링이 needs_human 변화도 감지해 새로고침한다. (2) 헤더
  "사람 확인 (n)" 알림 메뉴(대기 건이 있을 때), (3) 어느 화면에 있든 대기 작업이
  있으면 상단에 전역 배너가 서버 렌더로 떠 바로 처리 화면으로 보낸다(단건이면
  라이브 화면 직행, 여러 건이면 목록), (4) 대기 목록 + "처리" 버튼으로 라이브
  화면 진입. 사람 처리 창(`WCCG_LIVE_CHALLENGE_TIMEOUT_SECONDS`, 기본 5분)을 놓쳐
  실패한 작업은 아카이브 로그(`/log/archive`)에서 재시도하면 라이브 세션이 다시 열린다
  (자동 백오프 재시도도 동일). 한편 **워커**에서 `WCCG_LIVE_CHALLENGE=on` 인데
  캡처 엔진이 스텔스(patchright/headful)가 아니면 라이브가 비활성이며, worker 가
  그 이유를 시스템 로그(`/log/system`)에 한 번 경고로 남긴다.
- **라이브 챌린지 처리** (live) — `/archive/jobs/{id}/live`, 관리자 전용.
  worker 의 헤드풀 브라우저 스크린샷(`/live/shot`, image/jpeg)을 약 0.8초마다
  폴링해 보여주고, 화면 클릭/드래그/문자 입력을 `live_commands` 큐로 보내면
  (`POST /live/click`·`/live/key`) worker 가 page.mouse/keyboard 로 재생한다
  (좌표·타이밍·드래그 재현). 여는 관리자가 세션을 클레임(입력 권한자)하고,
  다른 관리자는 보기 전용. "화면 갱신"·"취소"(`POST /live/cancel`)·"사람 확인
  완료"(`POST /live/solve`, 소유자 전용) 제공. 통과하면(`/live/state` 폴링)
  자동으로 캡처가 이어지고 화면이 닫힌다. 로봇 확인을 풀었는데 잔여 위젯/마커로
  자동 판정이 안 풀리면 "사람 확인 완료"로 현재 화면을 강제 채택해 진행시킬 수
  있다(`live_force_solve` 플래그 → worker 가 다음 폴링에 현재 DOM 으로 캡처).
  라이브 화면은 **스크린샷 이미지 전용**(아카이빙 DOM 임베드 아님)이라
  원칙 5 와 무관하다. 데이터센터 IP 평판으론 사람이 눌러도 통과 미보장.
