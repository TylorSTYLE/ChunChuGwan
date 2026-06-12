"""웹 UI 다국어 (i18n).

한국어 원문이 곧 메시지 키다 (gettext msgid 방식). 한국어는 카탈로그 없이
원문 그대로 출력하고, 다른 언어는 "한국어 원문 → 번역" dict 하나로 추가한다.
카탈로그에 없는 문자열은 원문(한국어)으로 폴백한다.

- 로케일 결정: 로그인 사용자의 users.locale → Accept-Language → 한국어.
- 같은 원문이 문맥에 따라 다르게 번역돼야 하면 ctx 를 쓴다 —
  카탈로그 키는 "{ctx}|{원문}" (예: "diff|이전").
- 새 언어 추가: SUPPORTED_LOCALES·LOCALE_NAMES 에 코드/이름을 등록하고
  CATALOGS 에 번역 dict, _INTERVAL_UNITS 에 주기 단위 표기를 추가한다.
"""

from __future__ import annotations

from fastapi import Request

from .. import config
from ..auth import MAX_API_KEY_NAME_LENGTH, MAX_DISPLAY_NAME_LENGTH

DEFAULT_LOCALE = "ko"
SUPPORTED_LOCALES = ("ko", "en")
LOCALE_NAMES = {"ko": "한국어", "en": "English"}

# 주기 표기 단위 (큰 단위부터, 개월 = 30일). 미등록 로케일은 한국어 단위로 폴백.
_INTERVAL_UNITS: dict[str, tuple[tuple[int, str], ...]] = {
    "ko": ((30 * 86400, "개월"), (7 * 86400, "주"), (86400, "일"), (3600, "시간"), (60, "분")),
    "en": ((30 * 86400, "mo"), (7 * 86400, "w"), (86400, "d"), (3600, "h"), (60, "m")),
}

_EN: dict[str, str] = {
    # ---- 공통 / 헤더 ----
    "춘추관": "ChunChuGwan",
    "개인 웹 아카이브": "Personal web archive",
    "현황": "Overview",
    "목록": "Archives",
    "새 아카이빙": "New archive",
    "스케줄": "Schedules",
    "아카이빙 로그": "Archive logs",
    "시스템 로그": "System logs",
    "사용자": "Users",
    "시스템": "System",
    "계정": "Account",
    "로그아웃": "Log out",
    "메뉴": "Menu",
    "테마 전환 (자동 → 라이트 → 다크)": "Switch theme (auto → light → dark)",
    "테마: 자동": "Theme: auto",
    "테마: 라이트": "Theme: light",
    "테마: 다크": "Theme: dark",
    "언어": "Language",
    "표시 언어": "Display language",
    "언어 변경": "Change language",
    "언어를 변경했습니다.": "Language updated.",
    "지원하지 않는 언어입니다.": "Unsupported language.",
    "시간대": "Time zone",
    "기준 시각 국가/지역": "Country/region",
    "시간대 변경": "Change time zone",
    "시간대를 변경했습니다.": "Time zone updated.",
    "지원하지 않는 타임존입니다.": "Unsupported time zone.",
    "선택한 시간대 기준": "Based on selected time zone",
    "아시아": "Asia",
    "유럽": "Europe",
    "아메리카": "Americas",
    "태평양·오세아니아": "Pacific & Oceania",
    "아프리카·중동": "Africa & Middle East",
    "시각": "Time",
    "상태": "Status",
    "용량": "Size",
    "소요": "Duration",
    "출처": "Source",
    "오류": "Error",
    "보기": "View",
    "삭제": "Delete",
    "저장": "Save",
    "취소": "Cancel",
    "확인": "Verify",
    "신규": "New",
    "변경": "Changed",
    "action|변경": "Change",
    "동일": "Unchanged",
    "동일(강제)": "Unchanged (forced)",
    "실패": "Error",
    "도메인": "Domain",
    "스냅샷": "Snapshots",
    "one|스냅샷": "Snapshot",
    "해시": "Hash",
    "이전": "Previous",
    "다음": "Next",
    "전체": "All",
    "필터": "Filter",
    "URL 필터…": "Filter by URL…",
    "이메일": "Email",
    "패스워드": "Password",
    "권한": "Role",
    "이름": "Name",
    "CSRF 검증 실패": "CSRF validation failed",
    "차단된 계정입니다. 관리자에게 문의하세요.": "This account is blocked. Contact an administrator.",
    # ---- 사이트 전체 아카이브 (크롤) ----
    "사이트": "Sites",
    "one|사이트": "Site",
    "사이트 아카이브": "Site archives",
    "사이트 전체 아카이브": "Archive entire site",
    "같은 호스트에서 시작 URL 의 경로 프리픽스 이하 페이지를 링크를 따라가며 차례로 아카이빙합니다. 페이지 간 간격을 두어 대상 서버에 부담을 주지 않고, 실패한 페이지는 잠시 후 자동 재시도됩니다.":
        "Follows links on the same host under the start URL's path prefix and "
        "archives each page in turn. Pages are fetched with a delay to avoid "
        "stressing the target server, and failed pages are retried after a while.",
    "최대 페이지 수": "Max pages",
    "최대 깊이": "Max depth",
    "페이지 간 간격(초)": "Delay between pages (s)",
    "범위": "Scope",
    "진행 중": "Running",
    "완료됨": "Done",
    "취소됨": "Cancelled",
    "완료": "Done",
    "대기": "Pending",
    "재시도 대기": "Awaiting retry",
    "재시도": "Retry at",
    "등록 시각": "Created",
    "종료 시각": "Finished",
    "옵션": "Options",
    "깊이": "Depth",
    "시도": "Attempts",
    "결과": "Result",
    "실패 일괄 재시도": "Retry all failed",
    "목록으로": "Back to list",
    "아카이브에 없는 페이지": "Page not in the archive",
    "이 링크의 페이지는 아카이브되지 않았습니다 — 크롤 범위 밖이거나 아직/끝내 저장되지 않았습니다.":
        "The linked page was not archived — it is outside the crawl scope, "
        "or was not (yet) saved.",
    "크롤": "Crawl",
    "크롤 없음": "Crawl not found",
    "잘못된 URL": "Invalid URL",
    "원본 페이지 열기 (라이브 사이트)": "Open original page (live site)",
    "실패 재시도": "Retry on failure",
    "대기 후 재시도, 페이지당 최대 {n}회 시도 (시스템 화면에서 설정)":
        "wait then retry, up to {n} attempts per page (configured on the System screen)",
    "같은 사이트의 아카이브가 이미 진행 중이라 이 크롤에 병합되었습니다 (기존 옵션 유지).":
        "An archive of this site is already in progress, so your request was "
        "merged into this crawl (existing options kept).",
    # ---- 사이트 아카이브 스케줄 (크롤 스케줄) ----
    "시작 URL": "Start URL",
    "옵션 (페이지·깊이·간격)": "Options (pages · depth · delay)",
    "등록된 사이트 아카이브 스케줄이 없습니다. 새 아카이빙에서 '사이트 전체 아카이브'와 주기를 함께 선택하세요.":
        "No site archive schedules registered. On New archive, check 'Archive "
        "entire site' and pick an interval.",
    "사이트 아카이브 스케줄을 해제합니다. 저장된 스냅샷과 진행 중인 크롤은 그대로 남습니다.":
        "This removes the site archive schedule. Saved snapshots and any "
        "running crawl are kept.",
    "주기를 선택하면 같은 옵션으로 사이트 전체를 주기적으로 다시 수집합니다 (스케줄 화면에서 변경/해제).":
        "If you pick an interval, the entire site is re-crawled periodically "
        "with the same options (change or remove it on the Schedules screen).",
    "스케줄 없음": "Schedule not found",
    # ---- 역할 라벨 (db.ROLE_LABELS) ----
    "관리자": "Admin",
    "아카이브": "Archiver",
    "보기 전용": "Viewer",
    "권한없음": "No access",
    "차단됨": "Blocked",
    "탈퇴": "Withdrawn",
    # ---- 목록 (index) ----
    "아카이브 목록": "Archived pages",
    "아카이빙이 백그라운드에서 시작되었습니다": "Archiving started in the background",
    "완료되면 목록이 자동 갱신됩니다.": "The list refreshes automatically when it finishes.",
    "아카이브된 페이지가 없습니다.": "No archived pages yet.",
    "시작하려면": "To get started, use the",
    "메뉴나": "menu or the",
    "명령을 사용하세요.": "command.",
    "도메인 / 범위": "Domain / scope",
    "마지막 활동": "Last activity",
    "자동": "Auto",
    "시작": "started",
    "아카이빙 중": "Archiving",
    "재아카이빙": "Re-archive",
    "스냅샷 {n}개를 포함한 아카이브 전체를 삭제합니다. 되돌릴 수 없습니다.":
        "This deletes the entire archive including {n} snapshot(s). It cannot be undone.",
    # ---- 현황 (dashboard) ----
    "아카이브 페이지": "Archived pages",
    "전체 스냅샷": "Total snapshots",
    "이번 주 스냅샷": "Snapshots this week",
    "최근 24시간": "Last 24 hours",
    "총 용량": "Total size",
    "용량 트렌드": "Storage trend",
    "기간": "Period",
    "오늘": "Today",
    "이번 주": "This week",
    "이번 달": "This month",
    "올해": "This year",
    "최근 아카이브": "Recent snapshots",
    "아직 스냅샷이 없습니다.": "No snapshots yet.",
    "최근 로그": "Recent logs",
    "로그가 없습니다. 아카이빙을 실행하면 결과가 여기에 기록됩니다.":
        "No logs yet. Archive runs are recorded here.",
    "전체 로그 →": "All logs →",
    # ---- 타임라인 (timeline) ----
    "타임라인": "Timeline",
    "재아카이빙이 백그라운드에서 시작되었습니다. 잠시 후 새로고침하세요.":
        "Re-archiving started in the background. Refresh in a moment.",
    "콘텐츠가 동일해도 새 스냅샷을 저장합니다":
        "Save a new snapshot even if the content is identical",
    "강제": "Force",
    "자동 재아카이빙": "Auto re-archiving",
    "주기": "Interval",
    "다음 실행": "Next run",
    "마지막 실행": "Last run",
    "미설정 (최소 1시간 ~ 최대 1개월)": "Not set (1 hour to 1 month)",
    "주기 변경": "Change interval",
    "주기 설정": "Set interval",
    "해제": "Remove",
    "스냅샷이 없습니다.": "No snapshots.",
    "캡처 시각": "Captured at",
    "이전과 비교": "Compare with previous",
    "스냅샷 {t} 을 삭제합니다. 되돌릴 수 없으며, 다음 스냅샷의 변경 표시는 자동으로 보정됩니다.":
        "This deletes snapshot {t}. It cannot be undone; the next snapshot's "
        "change status is recalculated automatically.",
    "변경 없음 확인 기록 (최근 {n}건)": "No-change checks (last {n})",
    # ---- 스케줄 (schedules) ----
    "주기 최소 1시간 ~ 최대 1개월 · 1일 단위 주기는 실행 시각 지정 가능":
        "Interval from 1 hour to 1 month · daily+ intervals can run at a set time",
    "등록된 자동 재아카이빙이 없습니다. 페이지 타임라인에서 주기를 설정하세요.":
        "No auto re-archiving registered. Set an interval on a page's timeline.",
    "자동 재아카이빙을 해제합니다. 저장된 스냅샷은 그대로 남습니다.":
        "This removes auto re-archiving. Saved snapshots are kept.",
    "반복 주기는 1시간(1h) 이상 1개월(1mo) 이하여야 합니다":
        "The interval must be between 1 hour (1h) and 1 month (1mo)",
    "다음 실행 변경": "Change next run",
    "브라우저 로컬 시간 기준": "In your browser's local time",
    "잘못된 시각 형식: {v}": "Invalid time format: {v}",
    "실행 시각은 1일 단위 주기(1일~1개월)에서만 지정할 수 있습니다":
        "A run time can only be set for daily intervals (1 day to 1 month)",
    "직접 입력 주기는 숫자여야 합니다": "Custom interval must be a number",
    "직접 입력 주기는 1 이상이어야 합니다": "Custom interval must be 1 or greater",
    "직접 입력…": "Custom…",
    "unit|분": "min",
    "unit|시간": "hr",
    "unit|일": "day",
    "1일 단위 주기에서 실행할 시각 (서버 시간 기준, 비우면 등록 시점 기준)":
        "Time of day for daily+ intervals (server time; leave empty to run "
        "relative to registration)",
    # ---- 새 아카이빙 (archive_new) ----
    "https:// 생략 가능": "https:// can be omitted",
    "트래킹 파라미터(utm_* 등)는 자동으로 제거된 정규화 URL 로 저장됩니다.":
        "Tracking parameters (utm_*, etc.) are removed; the normalized URL is stored.",
    "자동 재아카이빙 주기": "Auto re-archiving interval",
    "사용 안 함 (1회만 아카이빙)": "Off (archive once)",
    "{label}마다": "Every {label}",
    "주기를 선택하면 아카이빙 완료 후 자동 재아카이빙이 등록됩니다. 타임라인 화면에서 언제든 변경/해제할 수 있습니다.":
        "If you pick an interval, auto re-archiving is registered once the archive "
        "completes. You can change or remove it anytime on the timeline.",
    "주기를 선택하면 아카이빙 완료 후 자동 재아카이빙이 등록됩니다. 직접 입력은 1시간~1개월 범위, 1일 단위 주기는 실행 시각(서버 시간)도 지정할 수 있습니다. 타임라인 화면에서 언제든 변경/해제할 수 있습니다.":
        "If you pick an interval, auto re-archiving is registered once the archive "
        "completes. Custom intervals range from 1 hour to 1 month; daily+ intervals "
        "can also run at a set time (server time). You can change or remove it "
        "anytime on the timeline.",
    "아카이빙 시작": "Start archiving",
    "1시간": "1 hour",
    "3시간": "3 hours",
    "6시간": "6 hours",
    "12시간": "12 hours",
    "1일": "1 day",
    "3일": "3 days",
    "1주일": "1 week",
    "1개월": "1 month",
    "7일": "7 days",
    "30일": "30 days",
    # ---- 스냅샷 뷰어 (snapshot) ----
    "타임라인으로": "Back to timeline",
    "최종 URL": "Final URL",
    "렌더링": "Rendered",
    "스크린샷": "Screenshot",
    "텍스트": "Text",
    "전체 페이지 스크린샷": "Full-page screenshot",
    "첨부 문서 ({n})": "Attached documents ({n})",
    "순번": "No.",
    "문서명": "Document name",
    "형식": "Format",
    "메타데이터 없음": "Metadata not found",
    # ---- 문서 목록 (documents) ----
    "문서": "Documents",
    "고유 문서 {n}개 · 저장 {size}": "{n} unique documents · {size} stored",
    "중복 제거로 {saved} 절약": "{saved} saved by deduplication",
    "아카이브된 페이지들이 링크한 문서 파일(PDF·워드·한글 등)의 통합 목록입니다. 같은 내용의 문서는 한 번만 저장되고 여러 스냅샷이 참조합니다.":
        "All document files (PDF, Word, HWP, …) linked by archived pages in one "
        "list. Identical documents are stored once and shared by every snapshot "
        "that references them.",
    "압축 전 스냅샷에 남아 있는 문서 파일이 있습니다 — 시스템 화면에서 저장공간 최적화를 실행하면 이 목록에 포함되고 중복이 제거됩니다.":
        "Some documents are still stored inside pre-compaction snapshots — run "
        "storage compaction from the System screen to include and deduplicate "
        "them here.",
    "출처 페이지": "Source page",
    "참조 스냅샷": "Snapshots",
    "마지막 저장": "Last saved",
    "외 {n}곳": "+{n} more",
    "아직 저장된 문서가 없습니다.": "No documents archived yet.",
    # ---- diff 뷰어 ----
    "비교": "Compare",
    "두 스냅샷의 정규화 텍스트가 같습니다.": "The normalized text of both snapshots is identical.",
    "+{n}줄": "+{n} lines",
    "-{n}줄": "-{n} lines",
    "{n}줄 동일": "{n} identical lines",
    "텍스트 비교": "Text diff",
    "diff|이전": "Before",
    "diff|이후": "After",
    "스크린샷 비교": "Screenshot diff",
    "변경 픽셀": "Changed pixels",
    "픽셀 diff": "Pixel diff",
    "이전 스크린샷": "Before screenshot",
    "이후 스크린샷": "After screenshot",
    "픽셀 diff 하이라이트": "Pixel diff highlight",
    # ---- 로그 (logs / system logs) ----
    "로그 열람 권한이 없습니다": "You do not have permission to view logs",
    # "action|재시도" 는 사이트 상세(실패한 작업) 절에 이미 있다
    "재시도가 백그라운드에서 시작되었습니다. 잠시 후 새로고침하세요.":
        "Retry started in the background. Refresh shortly.",
    "이미 같은 URL 의 아카이빙이 진행 중입니다.":
        "Archiving for this URL is already in progress.",
    "로그 없음": "Log not found",
    "실패한 로그만 재시도할 수 있습니다": "Only failed runs can be retried",
    "레벨": "Level",
    "모든 레벨": "All levels",
    "모든 출처": "All sources",
    "로거": "Logger",
    "메시지": "Message",
    "시스템 로그가 없습니다. 앱(대시보드·워커·CLI)의 동작 기록이 여기에 쌓입니다.":
        "No system logs yet. Application activity (dashboard, worker, CLI) is recorded here.",
    "페이지:": "Page:",
    "스냅샷:": "Snapshot:",
    "필터 해제": "Clear filter",
    "모든 도메인": "All domains",
    "모든 상태": "All statuses",
    "시작일": "Start date",
    "종료일": "End date",
    "조건에 맞는 로그가 없습니다.": "No logs match the filter.",
    "표시 줄 수": "Rows per page",
    "{n}줄": "{n} rows",
    "상세": "Details",
    "파일": "File",
    "설명": "Description",
    "합계 ({n}개)": "Total ({n} files)",
    "단계": "Step",
    "내용": "Detail",
    "총 {n}건": "{n} entries",
    "{p}/{t} 페이지": "page {p}/{t}",
    "자원 인라인 단일 HTML": "Single HTML with inlined resources",
    "단일 HTML (gzip, 공유 자원 참조)": "Single HTML (gzip, shared-resource refs)",
    "렌더링 후 DOM 소스": "Post-render DOM source",
    "렌더링 후 DOM 소스 (gzip)": "Post-render DOM source (gzip)",
    "추출·정규화 텍스트": "Extracted & normalized text",
    "캡처 메타 정보": "Capture metadata",
    # ---- 시스템 (system) ----
    "버전": "Version",
    "아카이브 루트": "Archive root",
    "저장 공간": "Storage",
    "페이지": "Pages",
    "확인 기록": "Checks",
    "스냅샷 파일": "Snapshot files",
    "공유 자원": "Shared resources",
    "합계": "Total",
    "유지 관리": "Maintenance",
    "저장공간 최적화": "Storage optimization",
    "대상 {n}개": "{n} pending",
    "대상 없음": "None pending",
    "구형 스냅샷을 압축 저장 형태(공유 자원 추출 + HTML gzip + 스크린샷 WebP + 문서 파일 공유 저장소 이전)로 변환하고, 사이트 공통 인라인 스타일을 공유 자원으로 추출하고, 자원 참조를 인덱스한 뒤 어떤 스냅샷도 참조하지 않는 공유 자원을 삭제합니다. 내용 보존이라 스냅샷이 담는 정보는 그대로이며, 여러 번 실행해도 안전합니다(멱등). 새 스냅샷은 저장 시점에 자동으로 압축·인덱스됩니다.":
        "Converts legacy snapshots to the compact storage form (shared-resource "
        "extraction + gzipped HTML + WebP screenshots + moving document files to "
        "the shared store), extracts site-wide inline stylesheets as shared "
        "resources, indexes resource references, then deletes shared resources "
        "no snapshot references. Content is preserved — snapshots keep exactly "
        "the same information — and it is idempotent, so running it multiple "
        "times is safe. New snapshots are compacted and indexed on save.",
    "기존 스냅샷을 압축·인덱스하고 참조 없는 공유 자원을 정리합니다. 스냅샷이 많으면 시간이 걸릴 수 있습니다. 계속할까요?":
        "Compact and index existing snapshots, then clean up unreferenced shared "
        "resources? With many snapshots this can take a while.",
    "최적화 실행": "Run optimization",
    "전체 백업": "Full backup",
    "DB(사용자·세션 등 인증 데이터 포함)와 스냅샷 파일, rules.json 을 통째로 담은 tar.gz 를 내려받습니다. 아래 전체 복원에서 그대로 되돌릴 수 있습니다.":
        "Downloads a tar.gz containing the entire DB (including auth data such as "
        "users and sessions), snapshot files, and rules.json. It can be restored "
        "as-is via Full restore below.",
    "전체 백업 다운로드": "Download full backup",
    "전체 복원": "Full restore",
    "현재 데이터(인증 포함)를 모두 지우고 업로드한 전체 백업 시점으로 되돌립니다. 되돌릴 수 없습니다. 세션도 백업 시점으로 돌아가므로 복원 후 다시 로그인해야 할 수 있습니다.":
        "Erases all current data (including auth) and reverts to the uploaded "
        "backup. This cannot be undone. Sessions also revert, so you may need to "
        "log in again after restoring.",
    "정말 복원할까요? 현재 데이터가 모두 백업 시점으로 교체됩니다.":
        "Really restore? All current data is replaced with the backup.",
    "복원": "Restore",
    "위험 구역": "Danger zone",
    "아카이브 내보내기": "Archive export",
    "페이지·스냅샷·확인 기록과 스냅샷 파일만 담습니다 (인증 데이터·실행 로그 제외). 다른 인스턴스로 아카이브를 옮기거나 합칠 때 사용합니다.":
        "Contains only pages, snapshots, checks, and snapshot files (no auth data "
        "or run logs). Use this to move or merge archives between instances.",
    "내보내기 다운로드": "Download export",
    "아카이브 가져오기": "Archive import",
    "가져오기": "Import",
    "기존 유지, 같은 스냅샷은 스킵 (여러 번 실행해도 안전)":
        "keep existing data, skip duplicate snapshots (safe to run repeatedly)",
    "기존 아카이브 데이터를 지우고 가져오기 (인증 데이터는 유지)":
        "erase existing archive data, then import (auth data is kept)",
    "overwrite 모드: 기존 아카이브 데이터(페이지·스냅샷·확인 기록·파일)를 모두 지우고 가져옵니다. 계속할까요?":
        "overwrite mode: erases all existing archive data (pages, snapshots, checks, "
        "files) before importing. Continue?",
    "관리자만 접근할 수 있습니다": "Admin access only",
    "최적화할 항목이 없습니다 — 스냅샷이 모두 압축·인덱스 형태입니다.":
        "Nothing to optimize — all snapshots are already compacted and indexed.",
    "최적화 실패: {e}": "Optimization failed: {e}",
    "최적화 완료: 변환 {converted}/{total}개 · 공유 자원 {externalized}개 추출 · 문서 {documents}개 이전 · 공통 스타일 {styles}개 추출(스냅샷 {styled}개) · 참조 백필 {indexed}개 · 고아 자원 {swept}개 정리 ({saved} 절약)":
        "Optimization finished: converted {converted}/{total} · extracted "
        "{externalized} shared resources · moved {documents} documents · "
        "extracted {styles} shared stylesheet(s) from {styled} snapshot(s) · "
        "backfilled {indexed} reference(s) · cleaned {swept} orphaned "
        "resource(s) (saved {saved})",
    "복원 실패: {e}": "Restore failed: {e}",
    "복원 완료 (백업: {created_at}, 페이지 {pages}개, 스냅샷 {snapshots}개)":
        "Restore complete (backup: {created_at}, {pages} pages, {snapshots} snapshots)",
    "가져오기 실패: {e}": "Import failed: {e}",
    "가져오기 완료 [{mode}]: 페이지 +{pages}, 스냅샷 +{snapshots} (스킵 {skipped}), 확인 기록 +{checks}":
        "Import complete [{mode}]: pages +{pages}, snapshots +{snapshots} "
        "(skipped {skipped}), checks +{checks}",
    "알 수 없는 모드: {mode}": "Unknown mode: {mode}",
    # ---- 사이트 아카이브 설정 (system) ----
    "사이트 아카이브 설정": "Site archive settings",
    "사이트 전체 아카이브의 기본 옵션과 실패 페이지의 재시도 대기 시간을 설정합니다. 기본 옵션은 새 크롤 등록 시의 초깃값이고(등록할 때 변경 가능), 재시도 대기는 진행 중인 크롤에도 즉시 적용됩니다. 대기 횟수 + 1 이 페이지당 최대 시도 횟수입니다.":
        "Configures the default options for site-wide archives and the retry "
        "wait times for failed pages. The defaults are the initial values when "
        "registering a new crawl (changeable at registration); retry waits "
        "apply immediately, including to running crawls. The number of waits "
        "+ 1 is the maximum attempts per page.",
    "실패 재시도 대기(초, 쉼표 구분)": "Retry waits on failure (s, comma-separated)",
    "페이지당 최대 {n}회 시도": "up to {n} attempts per page",
    "사이트 아카이브 설정을 저장했습니다.": "Site archive settings saved.",
    "재시도 대기는 쉼표로 구분한 초 단위 숫자 목록이어야 합니다 (예: 300, 900)":
        "Retry waits must be a comma-separated list of seconds (e.g. 300, 900)",
    # ---- 가입 설정 (system) ----
    "가입 설정": "Sign-up settings",
    "로그인 화면의 회원 가입 기능과 가입 계정의 초기 권한을 설정합니다 (SSO 자동 생성 계정에도 적용). '권한없음'으로 가입한 사용자는 관리자가 사용자 관리에서 권한을 부여할 때까지 서비스를 이용할 수 없습니다.":
        "Controls the sign-up feature on the login screen and the initial role of "
        "newly signed-up accounts (also applies to auto-provisioned SSO accounts). "
        "Users who sign up with 'No access' cannot use the service until an "
        "administrator grants them a role in user management.",
    "로그인 화면에서 회원 가입 허용": "Allow sign-up on the login screen",
    "가입 초기 권한": "Initial role for sign-ups",
    "가입 설정을 저장했습니다.": "Sign-up settings saved.",
    "가입 초기 권한으로 쓸 수 없는 역할: {role}":
        "Role cannot be used as the initial sign-up role: {role}",
    # ---- 사용자 관리 (users) ----
    "사용자 관리": "User management",
    "관리자=전체 관리, 아카이브=아카이빙 가능, 보기 전용=열람만, 권한없음=가입 승인 대기(안내 페이지 외 접근 불가), 차단됨=접근 불가. 권한없음 사용자는 권한을 부여해 승인합니다. 차단하면 해당 사용자의 모든 세션이 즉시 로그아웃됩니다. 최초 등록된 관리자의 권한은 변경할 수 없습니다.":
        "Admin = full control, Archiver = can archive, Viewer = read-only, "
        "No access = awaiting sign-up approval (nothing but the notice page), "
        "Blocked = no access. Approve a 'No access' user by granting them a role. "
        "Blocking logs out all of the user's sessions immediately. The "
        "founder admin's role cannot be changed.",
    "탈퇴=본인이 탈퇴한 계정(로그인 불가) — 권한을 되돌릴 수 없고, 계정 정보를 삭제하면 같은 이메일로 다시 가입하거나 초대할 수 있습니다.":
        "Withdrawn = the user closed their own account (cannot log in) — the role "
        "cannot be restored; deleting the account record frees the email for "
        "sign-up or invites again.",
    "활성 세션": "Active sessions",
    "가입일": "Joined",
    "권한 변경": "Change role",
    "(나)": "(me)",
    "최초 관리자": "Founder",
    "패스키": "Passkey",
    "변경 불가": "Locked",
    "탈퇴 — 삭제만 가능": "Withdrawn — delete only",
    "{email} 계정 정보를 완전히 삭제할까요? 되돌릴 수 없으며, 같은 이메일로 다시 가입하거나 초대할 수 있게 됩니다.":
        "Permanently delete the account record of {email}? This cannot be undone, "
        "and the email becomes available for sign-up or invites again.",
    "본인 계정의 모든 세션을 로그아웃합니다. 지금 이 로그인도 종료됩니다. 계속할까요?":
        "Log out all sessions of your own account? This login ends too. Continue?",
    "{email} 의 모든 세션을 로그아웃할까요?": "Log out all sessions of {email}?",
    "본인 계정의 권한을 변경합니다. 관리자 권한을 잃으면 이 화면에 다시 접근할 수 없습니다. 계속할까요?":
        "Change your own role? If you lose admin you cannot access this screen "
        "again. Continue?",
    "{email} 계정을 차단할까요? 모든 세션이 즉시 로그아웃됩니다.":
        "Block {email}? All of their sessions are logged out immediately.",
    "이메일 초대": "Email invite",
    "초대 링크는 {n}일 후 만료되며, 같은 이메일을 다시 초대하면 새 링크로 교체됩니다.":
        "Invite links expire after {n} day(s); re-inviting the same email replaces "
        "the link.",
    "메일 발송이 설정되지 않아(WCCG_SMTP_*) 초대 링크가 화면에 표시됩니다 — 직접 전달하세요.":
        "Mail is not configured (WCCG_SMTP_*), so invite links are shown on screen — "
        "share them directly.",
    "초대": "Invite",
    "초대한 사람": "Invited by",
    "만료": "Expires",
    "{email} 초대를 취소할까요? 링크가 즉시 무효화됩니다.":
        "Cancel the invite for {email}? The link becomes invalid immediately.",
    "부여할 수 없는 역할: {role}": "Role cannot be assigned: {role}",
    "사용자 없음": "User not found",
    "최초 관리자의 권한은 변경할 수 없습니다.": "The founder admin's role cannot be changed.",
    "탈퇴한 계정의 권한은 변경할 수 없습니다 — 계정 정보를 삭제하세요.":
        "A withdrawn account's role cannot be changed — delete the account record.",
    "최초 관리자는 삭제할 수 없습니다.": "The founder admin cannot be deleted.",
    "본인 계정은 여기서 삭제할 수 없습니다.": "You cannot delete your own account here.",
    "{email} 계정 정보를 삭제했습니다. 같은 이메일로 다시 가입하거나 초대할 수 있습니다.":
        "Deleted the account record of {email}. The email can sign up or be "
        "invited again.",
    "{email} 권한을 '{label}'(으)로 변경했습니다.": "Changed the role of {email} to '{label}'.",
    "{email} 이름을 '{name}'(으)로 변경했습니다.": "Changed the name of {email} to '{name}'.",
    "{email} 이름을 제거했습니다.": "Removed the display name of {email}.",
    "{email} 의 모든 세션을 로그아웃했습니다.": "Logged out all sessions of {email}.",
    "초대할 수 없는 역할: {role}": "Role cannot be invited: {role}",
    "{email} 은 이미 가입된 이메일입니다.": "{email} is already registered.",
    "{email} 초대를 만들었지만 메일 발송에 실패했습니다 — 링크를 직접 전달하세요: {link}":
        "Created an invite for {email} but sending the email failed — share the "
        "link directly: {link}",
    "{email} 에게 초대 메일을 보냈습니다.": "Sent an invite email to {email}.",
    "{email} 초대 링크 (메일 미설정 — 직접 전달하세요): {link}":
        "Invite link for {email} (mail not configured — share it directly): {link}",
    "초대를 취소했습니다.": "Invite cancelled.",
    "초대 없음": "Invite not found",
    # ---- API 키 (api_keys) ----
    "API 키": "API keys",
    "API 키 관리": "API key management",
    "외부 소프트웨어가 /api/v1 REST API 에 접근할 때 쓰는 키를 발급·폐기합니다. 키마다 보기/아카이브 권한과 만료를 설정합니다.":
        "Issue and revoke keys external software uses to access the /api/v1 REST "
        "API. Each key gets view/archive permissions and an expiry.",
    "외부 소프트웨어가 Authorization: Bearer 또는 X-API-Key 헤더로 /api/v1 에 접근할 때 쓰는 키입니다. 보기=아카이브 데이터 조회, 아카이브=아카이빙 트리거. 키 원문은 발급 직후 한 번만 표시되며, 폐기하면 즉시 무효화됩니다. 모든 관리자가 공동으로 관리합니다.":
        "Keys for external software accessing /api/v1 with an Authorization: Bearer "
        "or X-API-Key header. View = read archived data, Archive = trigger archiving. "
        "The key itself is shown only once right after issuing; revoking takes effect "
        "immediately. All admins manage keys together.",
    "복사": "Copy",
    "복사됨": "Copied",
    "키": "Key",
    "발급자": "Issued by",
    "perm|아카이브": "Archive",
    "만료됨": "Expired",
    "영구": "Permanent",
    "{name} 키를 폐기할까요? 이 키를 쓰는 외부 소프트웨어의 접근이 즉시 차단됩니다.":
        "Revoke the key '{name}'? External software using this key loses access "
        "immediately.",
    "폐기": "Revoke",
    "발급된 키가 없습니다.": "No keys issued yet.",
    "새 키 발급": "Issue a new key",
    "키 이름 (예: rss-bot)": "Key name (e.g. rss-bot)",
    "1개월 (30일)": "1 month (30 days)",
    "1년 (365일)": "1 year (365 days)",
    "사용자 지정 (일)": "Custom (days)",
    "만료까지 일 수": "Days until expiry",
    "발급": "Issue",
    "권한을 하나 이상 선택하세요.": "Select at least one permission.",
    "사용자 지정 만료는 1 ~ {n}일 사이여야 합니다.":
        "Custom expiry must be between 1 and {n} days.",
    "알 수 없는 만료 선택: {expiry}": "Unknown expiry option: {expiry}",
    "'{name}' 키를 발급했습니다 — 아래 키를 지금 복사하세요. 다시 표시되지 않습니다.":
        "Issued the key '{name}' — copy it below now. It will not be shown again.",
    "API 키 없음": "API key not found",
    "키를 폐기했습니다.": "Key revoked.",
    "키 이름을 입력하세요.": "Enter a key name.",
    "키 이름에 제어 문자를 쓸 수 없습니다.": "The key name cannot contain control characters.",
    # ---- 계정 설정 (account) ----
    "계정 설정": "Account settings",
    "사용자 이름": "Display name",
    "표시 이름 (비우면 이메일로 표시)": "Display name (leave empty to show your email)",
    "이름 변경": "Change name",
    "패스워드 변경": "Change password",
    "현재 패스워드": "Current password",
    "새 패스워드": "New password",
    "새 패스워드 확인": "Confirm new password",
    "변경하면 다른 기기의 세션은 모두 로그아웃됩니다.":
        "Changing it logs out sessions on other devices.",
    "SSO 전용 계정입니다. 패스워드는 IdP(Authentik)에서 관리하세요.":
        "SSO-only account. Manage the password in your IdP (Authentik).",
    "2단계 인증": "Two-factor authentication",
    "TOTP (인증 앱)": "TOTP (authenticator app)",
    "활성": "Enabled",
    "미설정": "Not set",
    "설정": "Set up",
    "{n}개": "{n}",
    "관리": "Manage",
    "SSO 로그인은 IdP(Authentik)의 2FA 를 사용합니다.":
        "SSO logins use the 2FA of your IdP (Authentik).",
    "위험 영역": "Danger zone",
    "탈퇴하면 즉시 로그아웃되고 다시 로그인할 수 없습니다. 계정 정보 삭제(같은 이메일 재가입)는 관리자에게 요청하세요.":
        "Withdrawing logs you out immediately and you can no longer log in. "
        "Ask an administrator to delete the account record "
        "(required to sign up again with the same email).",
    "정말 탈퇴할까요? 다시 로그인할 수 없습니다.":
        "Really close your account? You will not be able to log in again.",
    "패스워드 확인": "Confirm password",
    "확인을 위해 이메일({email})을 입력": "Type the email ({email}) to confirm",
    "계정 탈퇴": "Close account",
    "계정 삭제": "Delete account",
    "사용자 이름을 변경했습니다.": "Display name updated.",
    "패스워드를 변경했습니다. 다른 기기의 세션은 로그아웃되었습니다.":
        "Password changed. Sessions on other devices were logged out.",
    "SSO 전용 계정은 패스워드가 없습니다. IdP(Authentik)에서 관리하세요.":
        "SSO-only accounts have no password. Manage it in your IdP (Authentik).",
    "현재 패스워드가 올바르지 않습니다.": "The current password is incorrect.",
    "새 패스워드가 서로 일치하지 않습니다.": "The new passwords do not match.",
    "관리자 계정은 탈퇴할 수 없습니다.": "Admin accounts cannot be closed.",
    "패스워드가 올바르지 않습니다.": "The password is incorrect.",
    "확인 이메일이 일치하지 않습니다.": "The confirmation email does not match.",
    "탈퇴한 계정입니다.": "This account has been closed.",
    # ---- 로그인 / 가입 / 초대 (auth) ----
    "로그인": "Log in",
    "이메일 또는 패스워드가 올바르지 않습니다.": "Incorrect email or password.",
    "Authentik으로 로그인 →": "Log in with Authentik →",
    "계정이 없나요?": "No account?",
    "가입하기": "Sign up",
    "가입": "Sign up",
    "(8자 이상)": "(8+ characters)",
    "이미 계정이 있나요?": "Already have an account?",
    "관리자 등록": "Admin registration",
    "최초 구동입니다. 대시보드를 사용하려면 먼저 관리자 계정을 등록하세요. 이 페이지는 등록이 끝나면 다시 표시되지 않습니다.":
        "First run. Register an admin account to use the dashboard. This page is "
        "not shown again once registration is done.",
    "관리자 이메일": "Admin email",
    "등록": "Register",
    "th|등록": "Created",
    "이미 관리자가 등록되어 있습니다": "An admin is already registered",
    "초대 수락": "Accept invite",
    "유효하지 않거나 만료된 초대 링크입니다. 관리자에게 다시 초대를 요청하세요.":
        "Invalid or expired invite link. Ask an administrator for a new invite.",
    "로그인으로": "Go to login",
    "계정이 '{role}' 권한으로 만들어집니다. 패스워드를 설정하세요.":
        "will be created with the '{role}' role. Set a password.",
    "이미 가입된 이메일입니다.": "This email is already registered.",
    "이미 가입된 이메일입니다. 로그인하세요.": "This email is already registered. Log in instead.",
    "회원 가입이 비활성화되어 있습니다.": "Sign-up is disabled.",
    # ---- 가입 승인 대기 (pending) ----
    "가입 승인 대기 중": "Awaiting approval",
    "가입해 주셔서 감사합니다. 현재 계정은 관리자의 승인을 기다리고 있습니다.":
        "Thank you for signing up. Your account is awaiting administrator approval.",
    "관리자가 권한을 부여하면 바로 이용할 수 있습니다. 잠시 후 다시 방문해 주세요.":
        "You can start using the service as soon as an administrator grants you "
        "a role. Please check back later.",
    # ---- 2단계 로그인 / TOTP / 패스키 ----
    "등록된 패스키로 본인 확인을 완료하세요.": "Verify with a registered passkey.",
    "패스키로 인증": "Authenticate with a passkey",
    "인증 앱에 표시된 6자리 코드를 입력하세요.":
        "Enter the 6-digit code from your authenticator app.",
    "OTP 코드": "OTP code",
    "코드가 올바르지 않습니다.": "Incorrect code.",
    "패스키 인증이 취소되었습니다.": "Passkey authentication was cancelled.",
    "패스워드 인증이 필요합니다": "Password authentication required",
    "등록된 패스키가 없습니다": "No registered passkeys",
    "credential 누락": "Missing credential",
    "진행 중인 인증이 없습니다 — 다시 시도하세요": "No authentication in progress — try again",
    "등록되지 않은 패스키입니다": "Unregistered passkey",
    "패스키 인증에 실패했습니다": "Passkey authentication failed",
    "2단계 인증 (TOTP)": "Two-factor authentication (TOTP)",
    "활성화됨 — 패스워드 로그인 시 OTP 코드가 요구됩니다.":
        "Enabled — an OTP code is required for password logins.",
    "해제하려면 패스워드 확인": "Confirm password to disable",
    "2FA 해제": "Disable 2FA",
    "SSO 전용 계정입니다. 2FA는 IdP(Authentik)에서 관리하세요.":
        "SSO-only account. Manage 2FA in your IdP (Authentik).",
    "Google Authenticator 등 인증 앱으로 QR을 스캔한 뒤, 표시된 코드를 입력해 등록을 완료하세요.":
        "Scan the QR with an authenticator app (e.g. Google Authenticator), then "
        "enter the shown code to finish registration.",
    "수동 입력": "Manual entry",
    "확인 코드": "Verification code",
    "등록 완료": "Finish registration",
    "코드가 올바르지 않습니다. QR을 다시 스캔 후 시도하세요.":
        "Incorrect code. Re-scan the QR and try again.",
    "패스키 (2단계 인증)": "Passkeys (two-factor)",
    "패스키 {n}개 등록됨 — 패스워드 로그인 시 2단계 인증이 요구됩니다.":
        "{n} passkey(s) registered — two-factor is required for password logins.",
    "마지막 사용": "Last used",
    "등록된 패스키가 없습니다. Touch ID, 보안 키, 휴대폰 등을 패스워드 로그인의 2단계 인증 수단으로 등록할 수 있습니다.":
        "No passkeys registered. You can register Touch ID, a security key, or a "
        "phone as a second factor for password logins.",
    "새 패스키 이름": "New passkey name",
    "예: 맥북 Touch ID": "e.g. MacBook Touch ID",
    "패스키 등록": "Register a passkey",
    "패스키 등록이 취소되었습니다.": "Passkey registration was cancelled.",
    "SSO 전용 계정은 패스키를 등록할 수 없습니다": "SSO-only accounts cannot register passkeys",
    "진행 중인 등록이 없습니다 — 다시 시도하세요": "No registration in progress — try again",
    "패스키 등록 검증에 실패했습니다": "Passkey registration verification failed",
    "이미 등록된 패스키입니다": "This passkey is already registered",
    "패스키 없음": "Passkey not found",
    "요청 실패": "Request failed",
    # ---- OIDC ----
    "OIDC 가 설정되지 않았습니다": "OIDC is not configured",
    "IdP 오류: {e}": "IdP error: {e}",
    "code/state 누락": "Missing code/state",
    "state 불일치 또는 만료 — 로그인을 다시 시도하세요":
        "State mismatch or expired — try logging in again",
    "OIDC 응답에 이메일 클레임이 없습니다": "The OIDC response has no email claim",
    "IdP 가 검증하지 않은 이메일이라 기존 계정에 연결할 수 없습니다":
        "The IdP has not verified this email, so it cannot be linked to an existing account",
    "OIDC 토큰 검증 실패": "OIDC token validation failed",
    # ---- 입력 검증 (auth.validate_*) ----
    "올바른 이메일 형식이 아닙니다.": "Invalid email format.",
    "이름에 제어 문자를 쓸 수 없습니다.": "The name cannot contain control characters.",
    # ---- 대시보드 라우트 (app) ----
    "페이지 없음": "Page not found",
    "스냅샷 없음": "Snapshot not found",
    "허용되지 않은 파일": "File not allowed",
    "파일 없음": "File not found",
    "잘못된 자원 이름": "Invalid resource name",
    "자원 없음": "Resource not found",
    "스크린샷 없음": "Screenshot not found",
    "content.md 없음: {d}": "content.md missing: {d}",
    "비교하려면 스냅샷이 2개 이상 필요합니다 (현재 {n}개)":
        "At least 2 snapshots are required to compare (currently {n})",
    "잘못된 범위: from={f} to={t} (1 ~ {n})": "Invalid range: from={f} to={t} (1 – {n})",
    "아카이빙 권한이 없습니다": "You do not have archiving permission",
    "삭제 권한이 없습니다": "You do not have delete permission",
    "아카이빙 실패: {e}": "Archiving failed: {e}",
    "아카이빙이 진행 중인 페이지입니다 — 완료 후 다시 시도하세요":
        "This page is being archived — try again after it finishes",
    "삭제됨: {url} (스냅샷 {n}개)": "Deleted: {url} ({n} snapshot(s))",
    "스냅샷 삭제됨: {t}": "Snapshot deleted: {t}",
    # 로컬 네트워크 태그 (사설 IP 아카이빙 게이트 — netcheck)
    "로컬 네트워크 태그": "Local network tag",
    "선택 안 함 (공개 주소)": "None (public address)",
    "사설 IP 대역(로컬 네트워크) 주소는 태그를 선택해야 아카이빙할 수 있습니다. "
    "루프백 주소는 아카이빙할 수 없습니다.":
        "Addresses in private IP ranges (local networks) can only be archived "
        "with a tag selected. Loopback addresses cannot be archived.",
    "등록된 로컬 네트워크 태그가 없습니다 — 시스템 화면에서 먼저 추가하세요.":
        "No local network tags are registered — add one on the System page first.",
    "사설 IP 대역(로컬 네트워크)의 웹서버를 아카이빙할 때 어느 네트워크인지 "
    "구분하는 태그입니다. 사설 대역 주소는 태그를 지정해야 아카이빙할 수 있고, "
    "루프백 주소는 항상 아카이빙할 수 없습니다. ID(GUID)는 자동 발급됩니다.":
        "Tags that identify which local network a web server on a private IP "
        "range belongs to. Private-range addresses can only be archived with a "
        "tag, and loopback addresses can never be archived. The ID (GUID) is "
        "issued automatically.",
    "사용": "In use",
    "태그를 삭제할까요? 페이지·크롤이 사용 중이면 삭제되지 않습니다.":
        "Delete this tag? It cannot be deleted while pages or crawls use it.",
    "등록된 태그가 없습니다 — 사설 IP 대역 아카이빙이 모두 거부됩니다.":
        "No tags registered — all private IP range archiving is rejected.",
    "이름 (예: 집 NAS)": "Name (e.g. Home NAS)",
    "설명 (선택)": "Description (optional)",
    "추가": "Add",
    "루프백 주소는 아카이빙할 수 없습니다": "Loopback addresses cannot be archived",
    "로컬 네트워크(사설 IP) 주소는 로컬 네트워크 태그를 선택해야 "
    "아카이빙할 수 있습니다 — 태그는 시스템 화면에서 관리합니다":
        "Local network (private IP) addresses can only be archived with a local "
        "network tag selected — tags are managed on the System page",
    "알 수 없는 로컬 네트워크 태그입니다": "Unknown local network tag",
    "태그 이름을 입력하세요.": "Enter a tag name.",
    "태그 이름은 {n}자 이하여야 합니다.":
        "The tag name must be at most {n} characters.",
    "태그 설명은 {n}자 이하여야 합니다.":
        "The tag description must be at most {n} characters.",
    "이미 있는 태그 이름입니다: {name}": "A tag with this name already exists: {name}",
    "로컬 네트워크 태그 '{name}'을(를) 추가했습니다.":
        "Added local network tag '{name}'.",
    "로컬 네트워크 태그 없음": "Local network tag not found",
    "'{name}' 태그는 사용 중이라 삭제할 수 없습니다 (참조 {n}개).":
        "The tag '{name}' is in use and cannot be deleted ({n} reference(s)).",
    "로컬 네트워크 태그 '{name}'을(를) 삭제했습니다.":
        "Deleted local network tag '{name}'.",
    # 사이트(서브도메인) 단위 아카이브 — 목록·사이트 상세
    "사이트 {n}개": "{n} site(s)",
    "사이트 필터…": "Filter sites…",
    "아카이브는 사이트(서브도메인) 단위로 묶입니다 — www 와 도메인 자체는 같은 사이트입니다. 사이트를 누르면 페이지·크롤 회차 목록이 보입니다.":
        "Archives are grouped by site (subdomain) — www and the bare domain are "
        "the same site. Click a site to see its pages and crawl runs.",
    "크롤 회차": "Crawl runs",
    "크롤 진행 중": "Crawling",
    "페이지 {p}개 · 스냅샷 {s}개 · 크롤 회차 {c}개 · 용량 {size}":
        "{p} page(s) · {s} snapshot(s) · {c} crawl run(s) · {size}",
    "실패한 작업": "Failed runs",
    "최근 실행이 실패로 끝난 페이지입니다. 크롤 중 실패한 페이지도 포함되며, 재시도가 성공하면 목록에서 사라집니다.":
        "Pages whose latest run ended in failure, including pages that failed "
        "during a crawl. A successful retry removes them from this list.",
    "action|재시도": "Retry",
    "실패 기록 없음": "Failed run not found",
    "재시도가 등록되었습니다 — 크롤러가 곧 다시 시도합니다.":
        "Retry queued — the crawler will try again shortly.",
    "사이트 삭제": "Delete site",
    "페이지 {p}개와 크롤 회차 {c}개를 포함한 사이트 아카이브 전체를 삭제합니다. 되돌릴 수 없습니다.":
        "This deletes the entire site archive including {p} page(s) and {c} "
        "crawl run(s). This cannot be undone.",
    "이 사이트(서브도메인)에 속한 페이지 아카이브와 크롤 회차입니다. www 와 도메인 자체는 같은 사이트로 취급됩니다.":
        "Page archives and crawl runs that belong to this site (subdomain). "
        "www and the bare domain are treated as the same site.",
    "아직 페이지가 없습니다 — 크롤이 진행되면 여기에 쌓입니다.":
        "No pages yet — they will appear here as crawls make progress.",
    "크롤 회차가 없습니다 — 새 아카이빙에서 '사이트 전체 아카이브'를 선택하면 만들어집니다.":
        "No crawl runs — select 'Archive entire site' on the new archive "
        "screen to create one.",
    "크롤 스케줄": "Crawl schedules",
    "스케줄 관리(해제·시각 변경)는": "Manage schedules (remove, change time) on the",
    "화면에서 합니다.": "screen.",
    "사이트 없음": "Site not found",
    # 사이트 인증서 (site_certificates — 버전 이력)
    "인증서": "Certificates",
    "인증서 없음": "Certificate not found",
    "https 아카이빙 때 받은 서버 인증서의 버전 이력입니다. 인증서가 갱신되면 새 버전으로 기록되고 이전 버전은 남습니다.":
        "Version history of server certificates collected during https "
        "archiving. When a certificate is renewed it is recorded as a new "
        "version and earlier versions are kept.",
    "호스트": "Host",
    "주체": "Subject",
    "유효 기간": "Valid",
    "확인 기간": "Seen",
    "지문": "Fingerprint",
    "현재": "Current",
    "이전 버전": "Previous",
    "검증 안 됨": "Unverified",
    "캡처가 인증서 검증을 통과하지 못했습니다 (자체 서명 등)":
        "The capture did not pass certificate verification (self-signed, etc.)",
    "아카이빙·크롤이 진행 중인 사이트입니다 — 완료 후 다시 시도하세요":
        "Archiving or crawling is in progress for this site — try again after "
        "it finishes",
    "사이트 삭제됨: {key} (페이지 {p}개, 스냅샷 {s}개, 크롤 {c}개)":
        "Site deleted: {key} ({p} page(s), {s} snapshot(s), {c} crawl(s))",
}

# 설정값이 들어간 검증 메시지 — 원문이 f-string 이라 임포트 시점 값으로 키를 만든다
_EN[f"패스워드는 {config.MIN_PASSWORD_LENGTH}자 이상이어야 합니다."] = (
    f"The password must be at least {config.MIN_PASSWORD_LENGTH} characters."
)
_EN[f"재시도 대기는 1개 이상 {config.CRAWL_RETRY_BACKOFF_MAX_STEPS}개 이하여야 합니다"] = (
    f"There must be between 1 and {config.CRAWL_RETRY_BACKOFF_MAX_STEPS} retry waits"
)
_EN[f"이름은 {MAX_DISPLAY_NAME_LENGTH}자 이하여야 합니다."] = (
    f"The name must be at most {MAX_DISPLAY_NAME_LENGTH} characters."
)
_EN[f"키 이름은 {MAX_API_KEY_NAME_LENGTH}자 이하여야 합니다."] = (
    f"The key name must be at most {MAX_API_KEY_NAME_LENGTH} characters."
)

CATALOGS: dict[str, dict[str, str]] = {"en": _EN}


def resolve_locale(request: Request) -> str:
    """미인증 요청의 표시 언어 — Accept-Language → 기본(ko).

    로그인 사용자는 미들웨어가 users.locale 로 덮어쓴다.
    """
    return _parse_accept_language(request.headers.get("accept-language", ""))


def _parse_accept_language(header: str) -> str:
    """Accept-Language 헤더에서 지원 언어 중 q 가 가장 높은 것을 고른다."""
    candidates: list[tuple[float, str]] = []
    for i, part in enumerate(header.split(",")):
        piece = part.strip()
        if not piece:
            continue
        lang, _, params = piece.partition(";")
        q = 1.0
        if params.strip().startswith("q="):
            try:
                q = float(params.strip()[2:])
            except ValueError:
                q = 0.0
        primary = lang.strip().lower().split("-")[0]
        if primary in SUPPORTED_LOCALES:
            # q 동률이면 헤더에 먼저 나온 언어 우선
            candidates.append((-q + i * 1e-6, primary))
    if not candidates:
        return DEFAULT_LOCALE
    return min(candidates)[1]


def translate(locale: str, text: str, *, ctx: str | None = None, **params) -> str:
    """카탈로그 번역. 없으면 원문 폴백, params 가 있으면 str.format 적용."""
    catalog = CATALOGS.get(locale, {})
    out = catalog.get(f"{ctx}|{text}") if ctx is not None else None
    if out is None:
        out = catalog.get(text, text)
    return out.format(**params) if params else out


def gettext_for(locale: str):
    """템플릿에 주입할 번역 함수 (`_`) — 로케일을 고정한 translate."""
    def _(text: str, ctx: str | None = None, **params) -> str:
        return translate(locale, text, ctx=ctx, **params)
    return _


def t(request: Request, text: str, *, ctx: str | None = None, **params) -> str:
    """라우트 핸들러용 번역 — 미들웨어가 적재한 request.state.locale 사용."""
    locale = getattr(request.state, "locale", DEFAULT_LOCALE)
    return translate(locale, text, ctx=ctx, **params)


def format_interval(locale: str, seconds: int) -> str:
    """초를 로케일에 맞는 주기 표기로 (ko '1일 12시간' / en '1d 12h').

    scheduler.format_interval 의 로케일 버전 — CLI 표기는 그대로 두고
    웹 화면만 이 함수를 쓴다.
    """
    units = _INTERVAL_UNITS.get(locale, _INTERVAL_UNITS[DEFAULT_LOCALE])
    parts: list[str] = []
    for unit, label in units:
        n, seconds = divmod(seconds, unit)
        if n:
            parts.append(f"{n}{label}")
    return " ".join(parts) or f"0{units[-1][1]}"


def interval_label(request: Request, seconds: int) -> str:
    """라우트 핸들러용 — 요청 로케일로 주기 표기."""
    return format_interval(getattr(request.state, "locale", DEFAULT_LOCALE), seconds)


def schedule_label(request: Request, seconds: int, run_at: str | None) -> str:
    """주기 + 실행 시각 표기 (예: '1일 · 09:00') — scheduler.format_schedule 의 로케일 버전."""
    label = interval_label(request, seconds)
    return f"{label} · {run_at}" if run_at else label
