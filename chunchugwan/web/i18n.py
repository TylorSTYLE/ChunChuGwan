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
from ..credentials import MAX_JWT_LENGTH, MAX_PASSWORD_LENGTH, MAX_USERNAME_LENGTH

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
    "아카이브 사이트 목록": "Archived sites",
    "전체 문서(파일)": "All documents (files)",
    "새 아카이빙": "New archive",
    "스케줄": "Schedules",
    "로그": "Logs",
    "아카이빙 로그": "Archive logs",
    "아카이브 로그": "Archive logs",
    "시스템 로그": "System logs",
    "감사 로그": "Audit log",
    "누가 아카이빙·열람·문서 다운로드·관리 작업을 했는지 기록":
        "Record of who archived, viewed, downloaded documents, or performed admin actions",
    "모든 종류": "All types",
    "모든 요청자": "All actors",
    "요청자": "Actor",
    "감사 기록이 없습니다.": "No audit records yet.",
    "시간": "Time",
    "표시이름": "Display name",
    "표시이름을 저장했습니다.": "Saved display name.",
    "루프백 주소는 아카이빙할 수 없습니다.": "Loopback addresses cannot be archived.",
    "입력한 주소가 사설 IP 대역(로컬 네트워크)입니다 — 태그를 선택해야 아카이빙할 수 있습니다.":
        "The address is a private-IP (local network) — select a tag to archive it.",
    "로그인이 필요한 사이트의 자격증명 연결은 사이트 상세 화면에서 관리합니다.":
        "Credentials for sites requiring login are managed on the site detail page.",
    "HAR 파일 업로드는 사이트 상세 화면에서 지원합니다.":
        "HAR file upload is supported on the site detail page.",
    "이 도메인에 등록된 자격증명을 연결하거나 새로 추가할 수 있습니다. 아카이빙 시 로그인에 사용됩니다.":
        "Link a credential registered for this domain or add a new one — used to log in when archiving.",
    "시스템 상태": "System status",
    "현재 버전과 저장된 데이터 규모입니다.": "Current version and stored-data size.",
    "유지관리": "Maintenance",
    "검색 인덱스와 저장공간을 정리합니다.": "Maintain the search index and storage.",
    "아직 색인되지 않은 스냅샷을 다시 색인합니다.": "Re-index snapshots that are not yet indexed.",
    "압축·자원 공유로 저장공간을 줄입니다 (내용은 그대로).":
        "Reduce storage by compression and resource sharing (content preserved).",
    "아카이빙·크롤·문서 수집·로컬 네트워크 동작을 설정합니다.":
        "Configure archiving, crawling, document fetching, and local-network behavior.",
    "사이트 전체 아카이브(크롤)의 기본 범위·간격입니다.":
        "Default scope and interval for whole-site archiving (crawl).",
    "새 사이트 아카이브에 허용하는 상한과 실패 시 재시도 대기입니다. 기본값은 이 상한 이내로 조정됩니다.":
        "Maximum range allowed for new site archives, plus retry waits on failure. "
        "Defaults are clamped within these limits.",
    "스냅샷을 찍을 때의 추가 캡처 동작입니다.": "Extra capture behavior when taking snapshots.",
    "확장이 보낸 1회성 로그인 자격증명의 보관 시간입니다.":
        "Retention time for one-time login credentials sent by the extension.",
    "페이지가 링크한 문서 파일을 받을 때의 한도입니다.":
        "Limits when fetching document files linked from a page.",
    "사설 IP(로컬 네트워크) 주소를 아카이빙할 때 붙이는 태그입니다.":
        "Tags attached when archiving private-IP (local-network) addresses.",
    "회원 가입과 이메일 본인 인증 정책입니다.": "Sign-up and email-verification policy.",
    "주의: 초기 권한이 승인 대기(pending)가 아니면 가입·SSO 자동 생성 계정이 관리자 승인 없이 곧바로 권한을 갖습니다.":
        "Caution: if the initial role is not pending (approval required), sign-up and "
        "auto-provisioned SSO accounts gain permissions immediately without admin approval.",
    "회원 가입 허용 여부와 가입 시 초기 권한입니다.":
        "Whether sign-up is allowed and the initial role on sign-up.",
    "패스워드 계정이 로그인 전에 메일로 이메일을 검증하게 합니다.":
        "Require password accounts to verify their email by mail before login.",
    "메일 발송과 API 키 등 서버 연동 설정입니다.":
        "Server integration settings such as mail delivery and API keys.",
    "초대·이메일 인증 메일을 보내는 SMTP 서버입니다.":
        "SMTP server used to send invitation and verification emails.",
    "API 키 관리로 이동": "Go to API key management",
    "데이터 전체를 바꾸는 작업입니다 — 신중히 사용하세요.":
        "Operations that change all data — use with care.",
    "전체 백업·복원과 아카이브 내보내기·가져오기입니다.":
        "Full backup/restore and archive export/import.",
    "다른 춘추관 인스턴스로 전체 데이터를 옮길 때 켭니다 — 켜면 아카이빙이 중단됩니다.":
        "Enable when migrating all data to another ChunChuGwan instance — archiving stops while on.",
    "사용자": "Users",
    "API Key 관리": "Manage API keys",
    "시스템": "System",
    "시스템 설정": "System settings",
    "크롬 확장": "Chrome extension",
    "검색": "Search",
    "계정": "Account",
    "내 아카이브": "My archives",
    "로그아웃": "Log out",
    # 사람 보조 챌린지 해결 (라이브)
    "사람 확인": "Human check",
    "사람 확인 필요": "Human check needed",
    "처리 중": "In progress",
    "사람 확인 처리": "Human-assisted solve",
    "자동으로 통과하지 못한 챌린지 — 직접 풀어서 통과시킵니다": "Challenges that auto-solve couldn't pass — solve them yourself",
    "사람 확인이 필요한 작업이 없습니다.": "No jobs need human check.",
    "이미 처리되었거나 만료된 작업입니다 — 목록에서 다시 확인하세요.": "This job is already handled or has expired — check the list again.",
    "사람 확인 필요 — 클릭해서 지금 처리하세요": "Human check needed — click to solve now",
    "사람 처리 창(기본 5분)을 놓쳐 실패한 작업은": "Jobs that missed the human-solve window (5 min by default) can be retried from",
    "에서 다시 시도하면 라이브 세션이 다시 열립니다.": " — retrying reopens a live session.",
    "진입 시각": "Entered at",
    "처리": "Solve",
    "사람 확인 완료": "Mark human check done",
    "로봇 확인을 직접 통과시켰다면 현재 화면으로 캡처를 진행합니다. 계속할까요?":
        "If you've passed the robot check yourself, capture proceeds with the current screen. Continue?",
    "진행 요청됨 — 잠시만 기다리세요…": "Proceeding requested — please wait…",
    "로봇 확인을 통과했는데도 자동으로 진행되지 않으면 '사람 확인 완료'를 눌러 현재 화면 그대로 캡처를 진행시킬 수 있습니다.":
        "If you passed the robot check but capture doesn't proceed automatically, click \"Mark human check done\" to proceed with the current screen.",
    "처리되었습니다 — 캡처를 이어서 진행합니다. 잠시 후 결과는 로그에서 확인하세요.": "Solved — capture is continuing. Check the result in the logs shortly.",
    "다른 관리자가 처리 중입니다 — 보기 전용입니다.": "Another admin is handling this — view only.",
    "화면 갱신": "Refresh screen",
    "입력할 문자열…": "Text to type…",
    "문자 입력": "Send text",
    "이 작업을 취소할까요?": "Cancel this job?",
    "화면 위를 클릭하면 서버 브라우저의 같은 위치를 누릅니다. 드래그도 그대로 전달됩니다. 챌린지(체크박스·그림 찾기 등)를 통과시키면 자동으로 캡처가 이어집니다.": "Clicking on the screen clicks the same spot in the server browser. Drags are relayed too. Once you pass the challenge (checkbox, image-select, etc.) capture continues automatically.",
    "클릭": "click",
    "누름": "down",
    "뗌": "up",
    "드래그": "drag",
    "입력": "type",
    "처리됨": "solved",
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
    "닫기": "Close",
    "{version} 새 소식": "What's new in {version}",
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
    # SPA 크롤 회차 — 재시도 정책 문장을 조각으로 번역(횟수 동적)
    "대기 후 재시도": "retry after waiting",
    "최대": "max",
    "회": " tries",
    "크롤을 취소했습니다.": "Crawl cancelled.",
    "실패한 페이지를 다시 시도합니다.": "Retrying failed pages.",
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
    # ---- 역할 라벨 (빌트인 그룹 + db.STATE_ROLE_LABELS — 커스텀은 원문 폴백) ----
    "관리자": "Admin",
    "아카이브 관리": "Archive manager",
    "아카이브": "Archiver",
    "보기 전용": "Viewer",
    "권한없음": "No access",
    "차단됨": "Blocked",
    "탈퇴": "Withdrawn",
    # ---- 권한 그룹 화면 (groups.html) ----
    "권한 그룹": "Permission groups",
    "역할은 세분 권한의 묶음(프리셋)입니다. 여기서 각 역할의 기본 권한을 편집하거나 새 권한 그룹을 추가·삭제할 수 있습니다. 기본 그룹(관리자·아카이브 관리·아카이브·보기 전용)은 권한 묶음만 바꿀 수 있고 이름은 잠겨 있습니다. 사용자별 가감은 사용자 관리의 '세분 권한'에서 합니다.":
        "Roles are bundles (presets) of granular permissions. Here you can edit each "
        "role's default permissions or add and remove custom permission groups. The "
        "built-in groups (Admin, Archive manager, Archiver, Viewer) can only have their "
        "permission bundle changed — their names are locked. Per-user adjustments are "
        "made under 'Granular permissions' in user management.",
    "그룹의 권한을 바꾸면 그 그룹에 속한 모든 사용자에게 즉시 반영됩니다(개별 오버라이드는 유지). 소속 사용자가 있는 그룹은 삭제할 수 없습니다 — 먼저 사용자 역할을 옮기세요.":
        "Changing a group's permissions takes effect immediately for every user in "
        "that group (individual overrides are kept). A group with members cannot be "
        "deleted — move those users to another role first.",
    "소속 사용자": "Members",
    "기본": "Built-in",
    "표시 이름": "Display name",
    "그룹 삭제": "Delete group",
    "소속 사용자가 있어 삭제할 수 없습니다": "Cannot delete — group has members",
    "새 권한 그룹": "New permission group",
    "이름(영문 소문자·숫자·밑줄)": "Name (lowercase letters, digits, underscore)",
    "편집자": "Editor",
    "그룹 추가": "Add group",
    "권한 그룹 {name} 을(를) 삭제할까요?": "Delete permission group {name}?",
    # ---- 세분 권한 라벨 (db.PERMISSION_LABELS, ctx=perm) ----
    "perm|보기·검색": "View & search",
    "perm|아카이빙": "Archiving",
    "perm|삭제": "Delete",
    "perm|자격증명 관리": "Manage credentials",
    "perm|시스템 관리": "Manage system",
    "perm|사용자 관리": "Manage users",
    "perm|인증 스냅샷 전체 열람": "View all authenticated snapshots",
    "perm|개인 API Key": "Personal API Key",
    "perm|감사 로그 보기": "View audit logs",
    "perm|시스템 로그 보기": "View system logs",
    "perm|아카이브 로그 보기": "View archive logs",
    "perm|휴지통 관리": "Manage trash",
    # ---- 휴지통 ----
    "휴지통": "Trash",
    "삭제한 아카이브가 여기에 보관됩니다 — 복원하거나 영구 삭제할 수 있습니다.":
        "Deleted archives are kept here — you can restore or permanently delete them.",
    "휴지통 기능이 꺼져 있어 삭제 시 즉시 영구 삭제됩니다. 아래는 이전에 보관된 항목입니다.":
        "The trash is off, so deletes are permanent immediately. The items below were kept earlier.",
    "보관 기간": "Retention",
    "일": " days",
    "자동 영구삭제가 꺼져 있습니다 (수동 삭제 전까지 보관).":
        "Auto-deletion is off (kept until deleted manually).",
    "휴지통이 비어 있습니다.": "The trash is empty.",
    "삭제 시각": "Deleted at",
    "삭제자": "Deleted by",
    "보관 기한": "Expires",
    "영구 삭제": "Delete permanently",
    "복원했습니다.": "Restored.",
    "영구 삭제했습니다.": "Permanently deleted.",
    "이 항목을 영구 삭제할까요? 되돌릴 수 없습니다.":
        "Permanently delete this item? This cannot be undone.",
    "이 사이트의 모든 페이지·스냅샷·크롤·스케줄을 휴지통으로 옮길까요? 휴지통에서 복원할 수 있습니다.":
        "Move this site's pages, snapshots, crawls, and schedules to the trash? "
        "You can restore them from the trash.",
    "이 페이지의 모든 스냅샷을 휴지통으로 옮길까요? 휴지통에서 복원할 수 있습니다.":
        "Move this page's snapshots to the trash? You can restore them from the trash.",
    "아카이브 삭제 시 즉시 지우지 않고 휴지통에 보관했다가 기간 경과 시 자동 삭제합니다. 끄면 삭제가 즉시 영구 삭제됩니다.":
        "When archives are deleted, they are kept in the trash and auto-deleted after the "
        "retention period instead of being removed at once. Turn off to delete immediately.",
    "휴지통 사용": "Use trash",
    "보관 기간(일, 0=자동삭제 끔)": "Retention (days, 0 = no auto-delete)",
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
    "사람 확인 대기": "Awaiting human check",
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
    # 현황 — 크롬 확장 안내 카드
    "더 빠르고 편리하게 아카이브 하세요!": "Archive faster and easier!",
    "크롬 확장을 설치하면 보고 있는 페이지를 클릭 한 번으로 아카이브하고, 아카이브 히스토리도 바로 확인할 수 있습니다.":
        "Install the Chrome extension to archive the page you're viewing in one click "
        "and check its archive history right away.",
    "크롬 확장 내려받기": "Download Chrome extension",
    "설치 방법": "How to install",
    "내려받은 ZIP 파일의 압축을 풉니다.": "Unzip the downloaded file.",
    "크롬 주소창에": "Open",
    "를 엽니다.": "in Chrome's address bar.",
    "우측 상단 ‘개발자 모드’를 켭니다.": "Turn on “Developer mode” at the top right.",
    "‘압축해제된 확장 프로그램을 로드’를 눌러 압축 푼 폴더를 선택합니다.":
        "Click “Load unpacked” and select the unzipped folder.",
    "확장 아이콘을 눌러 이 춘추관 주소와, 개인 API Key 화면에서 발급한 키를 입력하면 연결됩니다.":
        "Click the extension icon and enter this ChunChuGwan address and a key issued "
        "on the Personal API Key page to connect.",
    "확장 파일을 찾을 수 없습니다": "Extension files not found",
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
    "24시간제 HH:MM (예: 09:00, 23:30)": "24-hour HH:MM (e.g. 09:00, 23:30)",
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
    "인증됨": "Authenticated",
    "로그인 자격증명으로 캡처된 스냅샷입니다 — 소유자/관리자만 볼 수 있습니다.":
        "Captured with login credentials — visible only to the owner and admins.",
    "최종 URL": "Final URL",
    "렌더링": "Rendered",
    "스크린샷": "Screenshot",
    "데스크탑 스크린샷": "Desktop screenshot",
    "모바일 스크린샷": "Mobile screenshot",
    "텍스트": "Text",
    "불러오는 중…": "Loading…",
    "텍스트를 불러오지 못했습니다.": "Failed to load text.",
    "전체 페이지 스크린샷": "Full-page screenshot",
    "전체 페이지 데스크탑 스크린샷": "Full-page desktop screenshot",
    "전체 페이지 모바일 스크린샷": "Full-page mobile screenshot",
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
    "이 사이트의 페이지가 링크한 문서 파일(PDF·워드·한글 등)입니다. 같은 내용의 문서는 한 번만 저장됩니다.":
        "Document files (PDF, Word, HWP, …) linked from this site's pages. "
        "Identical documents are stored once.",
    "문서가 더 있습니다 — 전체 목록 보기": "More documents — view the full list",
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
    # ---- 브라우저(확장) 클라이언트 캡처 ----
    "브라우저 캡처": "Browser capture",
    "불완전": "Incomplete",
    "브라우저 확장으로 캡처": "Captured via browser extension",
    "브라우저 확장으로 캡처된 스냅샷입니다. 로그인 상태로 캡처되어 민감 정보가 포함될 수 있으며 모든 사용자에게 보입니다.":
        "Captured by the browser extension. It may have been captured while logged in, "
        "so it can contain sensitive information and is visible to all users.",
    "일부 자원·프레임·스크린샷 수집이 실패한 불완전 캡처입니다.":
        "Incomplete capture — some resources, frames, or the screenshot failed to be collected.",
    "로컬(브라우저) 캡처 스냅샷이 포함된 비교입니다 — 브라우저 렌더 환경(해상도·dpr·확대) 차이로 변경이 과장될 수 있습니다.":
        "This comparison includes a local (browser) capture — differences may be exaggerated "
        "due to browser rendering environment (resolution, dpr, zoom).",
    "로컬(브라우저) 캡처 스냅샷은 해상도가 달라 스크린샷 비교를 제공하지 않습니다.":
        "Screenshot comparison is not available for local (browser) captures because their "
        "resolution differs.",
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
    "아카이브 설정": "Archive settings",
    "사용자 설정": "User settings",
    "서버 환경설정": "Server settings",
    "데이터 관리": "Data management",
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
    "DB(사용자·세션 등 인증 데이터 포함)와 스냅샷 파일, rules.json 을 통째로 담은 .ccg.backup 파일을 내려받습니다. 아래 전체 복원에서 그대로 되돌릴 수 있습니다.":
        "Downloads a .ccg.backup file containing the entire DB (including auth data "
        "such as users and sessions), snapshot files, and rules.json. It can be "
        "restored as-is via Full restore below.",
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
    "전체 아카이브 내보내기": "Full archive export",
    "페이지·스냅샷·확인 기록·크롤 회차·인증서·아카이브 로그와 스냅샷 파일을 담습니다 (인증 데이터 제외). 다른 인스턴스로 아카이브를 옮기거나 합칠 때 사용합니다.":
        "Contains pages, snapshots, checks, crawl runs, certificates, archive "
        "logs, and snapshot files (no auth data). Use this to move or merge "
        "archives between instances.",
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
    "복원은 .ccg.backup 확장자 파일만 받습니다.":
        "Restore only accepts files with the .ccg.backup extension.",
    "복원 완료 (백업: {created_at}, 페이지 {pages}개, 스냅샷 {snapshots}개)":
        "Restore complete (backup: {created_at}, {pages} pages, {snapshots} snapshots)",
    "가져오기 실패: {e}": "Import failed: {e}",
    "가져오기는 .ccg.export 확장자 파일만 받습니다.":
        "Import only accepts files with the .ccg.export extension.",
    "가져오기 완료 [{mode}]: 페이지 +{pages}, 스냅샷 +{snapshots} (스킵 {skipped}), 확인 기록 +{checks}, 크롤 +{crawls}, 인증서 +{certs}, 로그 +{logs}":
        "Import complete [{mode}]: pages +{pages}, snapshots +{snapshots} "
        "(skipped {skipped}), checks +{checks}, crawls +{crawls}, "
        "certificates +{certs}, logs +{logs}",
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
    # ---- 인증 보호 (rate limit) ----
    "인증 보호 (무차별 대입 방어)": "Authentication protection (brute-force defense)",
    "로그인·2단계 인증·이메일 코드의 시도 횟수를 제한합니다. 한도를 넘으면 잠시 차단됩니다.":
        "Limits the number of attempts for login, two-factor, and email codes. "
        "Exceeding a limit blocks further attempts for a while.",
    "로그인 시도 한도(이메일별)": "Login attempt limit (per email)",
    "로그인 시도 한도(IP별)": "Login attempt limit (per IP)",
    "로그인 카운트 창(분)": "Login count window (minutes)",
    "2단계 인증 시도 한도": "Two-factor attempt limit",
    "이메일 코드 오답 한도": "Email code wrong-answer limit",
    "이메일 코드 재발송 한도(시간당)": "Email code resend limit (per hour)",

    # ---- 이메일 본인 인증 ----
    "이메일 본인 인증": "Email verification",
    "회원 가입(패스워드 계정)으로 만든 계정이 메일로 받은 코드로 이메일 소유를 확인하게 합니다. SSO(OIDC) 계정은 IdP 가 검증하므로 제외됩니다.":
        "Require accounts created via sign-up (password accounts) to confirm "
        "ownership of their email with a code sent by mail. SSO (OIDC) accounts "
        "are excluded since the IdP verifies them.",
    "메일(SMTP) 설정이 없어 이 기능은 켜더라도 동작하지 않습니다 — 아래 메일(SMTP) 설정을 먼저 채우세요.":
        "This feature does nothing even when enabled because mail (SMTP) is not "
        "configured — fill in the Mail (SMTP) settings below first.",
    "이메일 본인 인증 사용": "Enable email verification",
    "인증 코드 만료 시간(분)": "Verification code expiry (minutes)",
    "이메일 본인 인증 설정을 저장했습니다.": "Email verification settings saved.",
    "인증 코드 만료 시간은 {lo} ~ {hi}분 사이여야 합니다.":
        "The verification code expiry must be between {lo} and {hi} minutes.",
    "{email} (으)로 보낸 인증 코드를 입력하세요. 코드는 {n}분 후 만료됩니다.":
        "Enter the verification code sent to {email}. The code expires in {n} minutes.",
    # SPA 는 이메일·만료 시간을 동적으로 끼워 넣어 문장을 조각으로 번역한다.
    "(으)로 보낸 인증 코드를 입력하세요.": "is the address we sent your verification code to. Enter it below.",
    "코드는": "Expires in",
    "분 후 만료됩니다.": " minutes.",
    "인증 코드를 다시 보냈습니다.": "Verification code resent.",
    "인증 코드": "Verification code",
    "코드를 받지 못했나요?": "Didn't get the code?",
    "인증 코드 다시 보내기": "Resend verification code",
    "메일 발송(SMTP)이 설정되지 않아 인증 코드를 보낼 수 없습니다. 관리자에게 문의하세요.":
        "Mail (SMTP) is not configured, so no verification code can be sent. "
        "Please contact your administrator.",
    "계정 설정으로 돌아가기": "Back to account settings",
    "인증 코드를 메일로 보냈습니다.": "A verification code has been emailed.",
    "코드가 올바르지 않거나 만료되었습니다.": "The code is incorrect or has expired.",
    "메일 발송이 설정되지 않아 코드를 보낼 수 없습니다.":
        "Mail is not configured, so the code cannot be sent.",
    "코드 발송에 실패했습니다. 잠시 후 다시 시도하세요.":
        "Failed to send the code. Please try again later.",
    "이메일 본인 인증을 완료했습니다.": "Email verification complete.",
    "이메일 인증": "Email verified",
    "SSO 계정은 IdP 가 이메일을 검증합니다.": "SSO accounts have their email verified by the IdP.",
    "미인증": "Unverified",
    "인증 코드 받기": "Get a verification code",
    "코드를 메일로 보낸 뒤 인증 화면으로 이동합니다.":
        "Sends a code by email and takes you to the verification screen.",
    "이메일 본인 인증이 켜져 있지 않거나 메일(SMTP) 설정이 없어 지금은 인증할 수 없습니다.":
        "Email verification is off or mail (SMTP) is not configured, so you "
        "cannot verify right now.",
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
    "관리자=전체 관리, 아카이브 관리=아카이빙·삭제, 아카이브=아카이빙 가능, 보기 전용=열람만, 권한없음=가입 승인 대기(안내 페이지 외 접근 불가), 차단됨=접근 불가. 권한없음 사용자는 권한을 부여해 승인합니다. 차단하면 해당 사용자의 모든 세션이 즉시 로그아웃됩니다. 최초 등록된 관리자의 권한은 변경할 수 없습니다.":
        "Admin = full control, Archive manager = archive & delete, Archiver = can "
        "archive, Viewer = read-only, "
        "No access = awaiting sign-up approval (nothing but the notice page), "
        "Blocked = no access. Approve a 'No access' user by granting them a role. "
        "Blocking logs out all of the user's sessions immediately. The "
        "founder admin's role cannot be changed.",
    "탈퇴=본인이 탈퇴한 계정(로그인 불가) — 권한을 되돌릴 수 없고, 계정 정보를 삭제하면 같은 이메일로 다시 가입하거나 초대할 수 있습니다.":
        "Withdrawn = the user closed their own account (cannot log in) — the role "
        "cannot be restored; deleting the account record frees the email for "
        "sign-up or invites again.",
    "역할은 권한 묶음(프리셋)입니다 — 아래 '세분 권한'에서 사용자별로 개별 권한을 더하거나 뺄 수 있습니다 (별표 = 프리셋과 다름). 역할을 바꾸면 세분 권한은 프리셋으로 초기화됩니다.":
        "A role is a preset bundle of permissions — under 'Permissions' below you "
        "can add or remove individual permissions per user (asterisk = differs from "
        "the preset). Changing the role resets permissions to that preset.",
    "세분 권한": "Permissions",
    "프리셋과 다름": "Differs from preset",
    "{email} 의 세분 권한을 저장했습니다.": "Saved permissions for {email}.",
    "이 계정 상태에서는 세분 권한을 조정할 수 없습니다 — 먼저 역할을 부여하세요.":
        "Permissions cannot be adjusted in this account state — assign a role first.",
    "사용자 관리 권한을 가진 마지막 계정입니다 — 역할을 바꿀 수 없습니다.":
        "This is the last account with user-management permission — its role "
        "cannot be changed.",
    "사용자 관리 권한을 가진 마지막 계정입니다 — 이 권한은 뗄 수 없습니다.":
        "This is the last account with user-management permission — that "
        "permission cannot be removed.",
    "사용자 관리 권한이 없습니다": "You do not have user-management permission",
    "시스템 관리 권한이 없습니다": "You do not have system-management permission",
    "자격증명 관리 권한이 없습니다": "You do not have credential-management permission",
    "활성 세션": "Active sessions",
    "가입일": "Joined",
    "권한 변경": "Change role",
    "(나)": "(me)",
    "최초 관리자": "Founder",
    "패스키": "Passkey",
    "변경 불가": "Locked",
    "탈퇴 — 삭제만 가능": "Withdrawn — delete only",
    "{email} 계정 정보를 완전히 삭제하려면 이메일 주소를 다시 입력하세요. 삭제하면 되돌릴 수 없으며, 같은 이메일로 다시 가입하거나 초대할 수 있게 됩니다.":
        "To permanently delete the account record of {email}, re-enter the email "
        "address. This cannot be undone, and the email becomes available for "
        "sign-up or invites again.",
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
    "메일 발송이 설정되지 않아 초대 링크가 화면에 표시됩니다 — 시스템 → 메일(SMTP) 설정에서 설정하거나 링크를 직접 전달하세요.":
        "Mail is not configured, so invite links are shown on screen — configure it in "
        "System → Mail (SMTP) settings, or share the link directly.",
    # ---- 메일(SMTP) 설정 (시스템 화면) ----
    "메일(SMTP) 설정": "Mail (SMTP) settings",
    "사용자 초대 메일을 보낼 SMTP 서버입니다. 호스트를 비우면 메일 발송이 꺼지고 초대 링크가 화면에 표시됩니다. 환경변수(WCCG_SMTP_*)로도 설정할 수 있으며, 여기서 저장한 값이 우선합니다.":
        "SMTP server used to send user invite emails. Leave the host blank to disable "
        "mail (invite links are then shown on screen). It can also be set via "
        "environment variables (WCCG_SMTP_*); values saved here take precedence.",
    "SMTP 호스트": "SMTP host",
    "포트": "Port",
    "TLS 모드": "TLS mode",
    "로그인 사용자": "Login user",
    "비우면 인증 생략": "Blank = no auth",
    "로그인 비밀번호": "Login password",
    "변경하려면 입력 (비우면 유지)": "Enter to change (blank = keep)",
    "저장된 비밀번호 삭제": "Delete saved password",
    "WCCG_SECRET_KEY 가 설정되지 않아 비밀번호를 저장할 수 없습니다 (환경변수 WCCG_SMTP_PASSWORD 는 그대로 쓸 수 있습니다).":
        "WCCG_SECRET_KEY is not set, so the password cannot be saved (the "
        "WCCG_SMTP_PASSWORD environment variable still works).",
    "발신자 주소": "Sender address",
    "비우면 로그인 사용자": "Blank = login user",
    "테스트 메일 보내기": "Send test email",
    "메일(SMTP) 설정을 저장했습니다.": "Saved mail (SMTP) settings.",
    "TLS 모드가 올바르지 않습니다.": "Invalid TLS mode.",
    "SMTP 포트는 1 ~ 65535 사이여야 합니다.": "SMTP port must be between 1 and 65535.",
    "WCCG_SECRET_KEY 가 설정되지 않아 SMTP 비밀번호를 저장할 수 없습니다.":
        "WCCG_SECRET_KEY is not set, so the SMTP password cannot be saved.",
    "테스트 메일을 받을 이메일 주소가 없습니다.":
        "No email address available to receive the test mail.",
    "SMTP 호스트가 설정되지 않았습니다.": "SMTP host is not configured.",
    "테스트 메일 발송에 실패했습니다: {e}": "Failed to send test email: {e}",
    "{email} 로 테스트 메일을 보냈습니다.": "Sent a test email to {email}.",
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
    "외부 소프트웨어가 Authorization: Bearer 또는 X-API-Key 헤더로 /api/v1 에 접근할 때 쓰는 시스템 키입니다. 보기=아카이브 데이터 조회, 아카이브=아카이빙 트리거. 키 원문은 발급 직후 한 번만 표시되며, 폐기하면 즉시 무효화됩니다. 모든 관리자가 공동으로 관리합니다. 개인용 크롬 확장 키는 각자 개인 API Key 화면에서 발급합니다.":
        "System keys for external software accessing /api/v1 with an Authorization: "
        "Bearer or X-API-Key header. View = read archived data, Archive = trigger "
        "archiving. The key itself is shown only once right after issuing; revoking "
        "takes effect immediately. All admins manage these together. Personal Chrome "
        "extension keys are issued by each user on the Personal API Key page.",
    "복사": "Copy",
    "복사됨": "Copied",
    "복사 실패": "Copy failed",
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
    # ---- 딥링크 안내 (go_missing) ----
    "아카이브 없음": "Not archived",
    "아카이브된 스냅샷이 없습니다": "No archived snapshot",
    "이 URL 은 아직 아카이브되지 않았습니다.": "This URL has not been archived yet.",
    # ---- 확장 1회성 세션 자격증명 (시스템 설정) ----
    "확장 자격증명 설정": "Extension credentials",
    "크롬 확장의 ‘로그인 페이지 아카이브’가 보낸 1회성 세션 자격증명을 자동 폐기하기까지의 최대 보관 시간입니다. 정상 흐름에선 캡처 직후 삭제되며, 이 값은 오류·재기동으로 삭제가 누락된 자격증명을 정리하는 안전망입니다.":
        "Maximum time to keep a one-shot session credential sent by the Chrome "
        "extension's “archive logged-in page” before discarding it. Normally it is "
        "deleted right after capture; this value is a safety net that cleans up "
        "credentials left behind by errors or restarts.",
    "자격증명 보관 시간(시간)": "Credential retention (hours)",
    "자격증명 보관 시간은 {lo} ~ {hi}시간 사이여야 합니다.":
        "Credential retention must be between {lo} and {hi} hours.",
    "확장 자격증명 설정을 저장했습니다.": "Extension credential settings saved.",
    # ---- 캡처 설정 (system) ----
    "캡처 설정": "Capture settings",
    "아카이빙할 때 데스크탑 스크린샷 외에 모바일 해상도(화면비율) 스크린샷도 함께 저장합니다. 같은 URL 을 안드로이드 크롬 모바일 브라우저로 한 번 더 열어 찍으며, 이후 새로 만들어지는 스냅샷에만 적용됩니다(기존 스냅샷은 그대로).":
        "When archiving, also save a mobile-resolution (aspect-ratio) screenshot "
        "in addition to the desktop one. The same URL is reopened once more as an "
        "Android Chrome mobile browser for the extra shot; this applies only to "
        "snapshots created afterward (existing snapshots are unchanged).",
    "모바일 해상도 스크린샷도 함께 저장": "Also save a mobile-resolution screenshot",
    "캡처 설정을 저장했습니다.": "Capture settings saved.",
    # ---- 문서 아카이브 설정 (system) ----
    "문서 아카이브 설정": "Document archive settings",
    "아카이빙하는 페이지가 링크한 문서 파일(PDF·워드·한글 등)을 받을 때의 한도입니다. 한 스냅샷에서 받는 문서 수, 문서 1개의 최대 크기, 다운로드 타임아웃을 정합니다. 이후 새로 저장되는 스냅샷에 적용됩니다(기존 스냅샷은 그대로).":
        "Limits for downloading document files (PDF, Word, HWP, etc.) linked from the "
        "page being archived. Sets how many documents are saved per snapshot, the "
        "maximum size of a single document, and the download timeout. Applies to "
        "snapshots saved afterward (existing snapshots are unchanged).",
    "스냅샷당 문서 수": "Documents per snapshot",
    "문서 1개 크기 한도(MB)": "Per-document size limit (MB)",
    "다운로드 타임아웃(초)": "Download timeout (seconds)",
    "문서 수 한도는 {lo} ~ {hi}개 사이여야 합니다.":
        "Document count limit must be between {lo} and {hi}.",
    "문서 크기 한도는 {lo} ~ {hi}MB 사이여야 합니다.":
        "Document size limit must be between {lo} and {hi} MB.",
    "문서 다운로드 타임아웃은 {lo} ~ {hi}초 사이여야 합니다.":
        "Document download timeout must be between {lo} and {hi} seconds.",
    "문서 아카이브 설정을 저장했습니다.": "Document archive settings saved.",
    # ---- 사이트 로그인 자격증명 (site_credentials) ----
    "로그인 자격증명": "Login credentials",
    "로그인 자격증명 관리": "Manage login credentials",
    "— 이 사이트 캡처 시 사용할 로그인 정보": "— login info used when capturing this site",
    "이 사이트를 아카이빙할 때 춘추관이 로그인하는 데 쓸 자격증명입니다. 비밀은 WCCG_SECRET_KEY 로 대칭 암호화해 저장하며, 화면에는 다시 표시되지 않습니다.":
        "Credentials ChunChuGwan uses to log in when archiving this site. Secrets are "
        "stored symmetrically encrypted with WCCG_SECRET_KEY and are never shown again.",
    "이 사이트 로그인이 필요한 경우 쓸 자격증명을 관리합니다 (관리자 전용).":
        "Manage credentials to use when this site requires login (admin only).",
    "WCCG_SECRET_KEY 가 설정되지 않아 자격증명을 저장할 수 없습니다.":
        "WCCG_SECRET_KEY is not set, so credentials cannot be stored.",
    "환경변수 WCCG_SECRET_KEY 에 임의의 비밀 문자열을 설정하고 대시보드를 다시 시작하면 등록할 수 있습니다.":
        "Set any secret string in the WCCG_SECRET_KEY environment variable and "
        "restart the dashboard to register credentials.",
    "종류": "Type",
    "만든 사람": "Created by",
    "이 자격증명을 삭제합니다. 되돌릴 수 없습니다.":
        "Delete this credential. This cannot be undone.",
    "등록된 자격증명이 없습니다.": "No credentials registered yet.",
    "새 자격증명 등록": "Add a credential",
    "예: 관리자 계정": "e.g. admin account",
    "사용자명": "Username",
    "비밀번호": "Password",
    "세션 상태 (storage_state JSON)": "Session state (storage_state JSON)",
    "브라우저에서 로그인한 뒤 Playwright 의 storage_state() 등으로 추출한 JSON 을 붙여넣으세요. 쿠키·localStorage 가 포함됩니다.":
        "Log in in your browser, then paste the JSON exported via Playwright's "
        "storage_state() or similar. It includes cookies and localStorage.",
    "또는 HAR 파일 업로드": "Or upload a HAR file",
    "로그인한 상태로 기록한 HAR 파일(브라우저 개발자도구 네트워크 탭 → 내보내기)을 올리면 쿠키를 자동 추출해 세션 상태로 저장합니다. 이 사이트 도메인의 쿠키만 가져오며, HAR 을 올리면 위 JSON 입력은 무시되고 localStorage 는 포함되지 않습니다.":
        "Upload a HAR file recorded while logged in (browser DevTools Network tab "
        "→ export) to automatically extract its cookies into the session state. "
        "Only cookies for this site's domain are imported; when a HAR is uploaded "
        "the JSON field above is ignored and localStorage is not included.",
    "로그인한 상태로 기록한 HAR 파일을 올리면 쿠키를 자동 추출해 세션 상태로 저장합니다. HAR 을 올리면 위 JSON 입력은 무시됩니다.":
        "Upload a HAR file recorded while logged in to automatically extract its "
        "cookies into the session state. When a HAR is uploaded the JSON field "
        "above is ignored.",
    # HAR 파싱 오류 (credentials.storage_state_from_har)
    "HAR 파일이 너무 큽니다.": "The HAR file is too large.",
    "HAR 파일을 UTF-8 로 읽을 수 없습니다.": "The HAR file could not be read as UTF-8.",
    "HAR 파일이 올바른 JSON 이 아닙니다.": "The HAR file is not valid JSON.",
    "올바른 HAR 파일이 아닙니다 (log.entries 가 없습니다).":
        "Not a valid HAR file (missing log.entries).",
    "HAR 파일에서 쿠키를 찾지 못했습니다.": "No cookies were found in the HAR file.",
    # 종류 라벨 (credentials.KIND_LABELS)
    "HTTP 기본 인증": "HTTP basic auth",
    "세션 쿠키": "Session cookie",
    "JWT (Bearer 토큰)": "JWT (Bearer token)",
    "Bearer 토큰": "Bearer token",
    "캡처 시 Authorization: Bearer 헤더로 주입됩니다. 'Bearer ' 접두사 없이 토큰 값만 넣으세요.":
        "Injected as an Authorization: Bearer header during capture. "
        "Enter only the token value, without the 'Bearer ' prefix.",
    "토큰을 입력하세요.": "Enter a token.",
    "토큰에 공백·줄바꿈을 넣을 수 없습니다.":
        "The token cannot contain spaces or line breaks.",
    # 라우트·검증 메시지
    "자격증명 없음": "Credential not found",
    "잘못된 자격증명 종류입니다.": "Invalid credential type.",
    "로그인 자격증명 추가": "Add login credentials",
    "이 사이트에 로그인이 필요하면 자격증명을 등록합니다. 비밀은 WCCG_SECRET_KEY 로 암호화 저장되며, 사이트 상세에서도 관리할 수 있습니다.":
        "Register credentials if this site requires login. Secrets are stored "
        "encrypted with WCCG_SECRET_KEY and can also be managed from the site detail page.",
    "비우면 자동 지정": "Auto-named if left blank",
    "이 사이트에 이미 같은 이름의 자격증명이 있습니다: {name}":
        "This site already has a credential with that name: {name}",
    "연결 안 함": "Don't connect",
    "새 자격증명 추가…": "Add new credential…",
    "이 도메인에 등록된 자격증명이 있으면 골라서 연결하고, 없으면 새로 추가할 수 있습니다. 아카이빙 시 로그인에 사용됩니다 (사이트 상세에서도 관리).":
        "If this domain has registered credentials you can pick one to connect; "
        "otherwise add a new one. Used to log in during archiving "
        "(also managed from the site detail page).",
    "잘못된 자격증명 선택입니다.": "Invalid credential selection.",
    "이 도메인에 등록된 자격증명이 아닙니다.": "Not a credential registered for this domain.",
    "이미 있는 이름입니다: {name}": "Name already exists: {name}",
    "자격증명을 등록했습니다.": "Credential added.",
    "자격증명을 삭제했습니다.": "Credential deleted.",
    "이름을 입력하세요.": "Enter a name.",
    "사용자명을 입력하세요.": "Enter a username.",
    "비밀번호를 입력하세요.": "Enter a password.",
    "세션 상태(storage_state) JSON 을 입력하세요.":
        "Enter the session state (storage_state) JSON.",
    "세션 상태 JSON 이 너무 큽니다.": "The session state JSON is too large.",
    "세션 상태가 올바른 JSON 이 아닙니다.": "The session state is not valid JSON.",
    "세션 상태 JSON 형식이 아닙니다 (cookies 키가 필요합니다).":
        "Not a session state JSON (a 'cookies' key is required).",
    # ---- 계정 설정 (account) ----
    "계정 설정": "Account settings",
    # 개인 API Key (확장 토큰)
    "개인 API Key": "Personal API Key",
    "크롬 확장 등 외부 도구가 쓰는 본인 전용 API Key 는 별도 화면에서 관리합니다.":
        "Manage your personal API Key for the Chrome extension and other tools on its own page.",
    "크롬 확장이 Authorization: Bearer 헤더로 /api/v1 에 접근할 때 쓰는 본인 전용 API Key 입니다. 권한(보기·아카이브)은 발급 시 내 역할 범위 안에서 선택하며, 원문은 발급 직후 한 번만 표시됩니다. 폐기하면 그 키를 쓰는 확장의 접근이 즉시 차단됩니다.":
        "Your personal API Key for the Chrome extension to access /api/v1 with an "
        "Authorization: Bearer header. Pick its permissions (view/archive) when issuing, "
        "within your role's limits; the key is shown only once right after issuing. "
        "Revoking it cuts off the extension using it immediately.",
    "발급한 API Key 가 없습니다.": "No API Keys issued yet.",
    "이름 (예: chrome-ext)": "Name (e.g. chrome-ext)",
    "현재 권한으로는 API Key 를 발급할 수 없습니다.": "Your current role can't issue API Keys.",
    "개인 API Key 사용 권한이 없습니다.": "You do not have permission to use personal API Keys.",
    "{name} 키를 폐기할까요? 이 키를 쓰는 확장의 접근이 즉시 차단됩니다.":
        "Revoke the key '{name}'? The extension using it loses access immediately.",
    "개인 API Key 를 발급했습니다 — 아래 키를 지금 복사하세요. 다시 표시되지 않습니다.":
        "Issued a personal API Key — copy it below now. It will not be shown again.",
    "개인 API Key 를 폐기했습니다.": "Personal API Key revoked.",
    "API Key 없음": "API Key not found",
    # 내 아카이브 (my_archives) — 본인이 요청한 단발 아카이빙 이력
    "대시보드·크롬 확장에서 내가 직접 요청한 아카이빙 실행 기록입니다. 예약·사이트 전체 아카이브(크롤)·CLI 실행은 포함되지 않습니다.":
        "Archiving runs you triggered yourself from the dashboard or the Chrome "
        "extension. Scheduled runs, full-site archives (crawls), and CLI runs are "
        "not included.",
    "조건에 맞는 기록이 없습니다.": "No records match the filter.",
    "아직 요청한 아카이브가 없습니다. 새 아카이빙을 실행하면 여기에 기록됩니다.":
        "You haven't requested any archives yet. Run a new archive and it will show up here.",
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
    "정말 탈퇴하려면 이메일({email})을 입력하세요. 탈퇴하면 즉시 로그아웃되고 다시 로그인할 수 없습니다.":
        "To close your account, type the email ({email}). Withdrawing logs you "
        "out immediately and you cannot log in again.",
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
    "SSO 로그인 →": "SSO login →",
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
    # 로컬 네트워크 태그 병합 (같은 IP:포트의 중복 태그 정리)
    "같은 사설 IP·포트를 가리키는 두 태그를 하나로 합칩니다. 출처 태그의 "
    "페이지·크롤이 대상 태그로 옮겨지고 출처 태그는 삭제됩니다.":
        "Merges two tags that point to the same private IP and port into one. "
        "The source tag's pages and crawls move to the target tag, and the "
        "source tag is deleted.",
    "출처 태그를 대상 태그로 병합할까요? 출처 태그는 삭제되며, 두 태그가 "
    "같은 IP·포트를 가리킬 때만 병합됩니다.":
        "Merge the source tag into the target tag? The source tag is deleted, "
        "and the merge only proceeds when both tags point to the same IP and port.",
    "출처 태그(삭제됨)": "Source tag (deleted)",
    "대상 태그(유지)": "Target tag (kept)",
    "병합": "Merge",
    "같은 태그끼리는 병합할 수 없습니다.": "A tag cannot be merged with itself.",
    "참조가 없는 태그는 병합할 수 없습니다 — 삭제를 사용하세요.":
        "A tag with no references cannot be merged — use delete instead.",
    "두 태그가 같은 사설 네트워크(같은 IP·포트)를 가리킬 때만 "
    "병합할 수 있습니다.":
        "Tags can only be merged when they point to the same private network "
        "(same IP and port).",
    "'{src}' 태그를 '{tgt}'(으)로 병합했습니다 "
    "(페이지 {p}개·크롤 {c}개·스케줄 {s}개 이전).":
        "Merged tag '{src}' into '{tgt}' "
        "({p} page(s), {c} crawl(s), {s} schedule(s) moved).",
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
    "모두 재시도": "Retry all",
    "실패한 작업을 모두 재시도할까요?": "Retry all failed runs?",
    "실패한 작업을 모두 재시도합니다 — 백그라운드에서 진행됩니다.":
        "Retrying all failed runs — this proceeds in the background.",
    "재시도가 등록되었습니다 — 백그라운드에서 진행됩니다.":
        "Retry queued — this proceeds in the background.",
    "재시도할 실패 작업이 없습니다.": "No failed runs to retry.",
    "이 사이트 내보내기": "Export this site",
    "파일 준비중…": "Preparing file…",
    "내보내기 파일을 다운로드했습니다.": "Export file downloaded.",
    "— 이 사이트의 페이지·스냅샷만 담은 .ccg.export 파일":
        "— a .ccg.export file with only this site's pages and snapshots",
    "다시 아카이빙": "Re-archive",
    "같은 범위·옵션으로 사이트 전체를 다시 아카이빙합니다. 계속할까요?":
        "Re-archive the entire site with the same scope and options. Continue?",
    "같은 시작 URL·범위·옵션으로 사이트 아카이브를 다시 실행합니다.":
        "Re-run the site archive with the same start URL, scope, and options.",
    "실패한 페이지만 큐로 되돌려 다시 시도합니다 (성공한 페이지는 그대로).":
        "Re-queue and retry only the failed pages (successful ones are left as is).",
    "사이트 내보내기": "Export site",
    "소속 페이지·스냅샷을 아카이브 내보내기 파일(.ccg.export)로 다운로드합니다 — 가져오기로 복원할 수 있습니다.":
        "Download this site's pages and snapshots as an archive export file "
        "(.ccg.export) — it can be restored via import.",
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
    "대체 이름": "Alternative names",
    "일련번호": "Serial number",
    "서명 알고리즘": "Signature algorithm",
    "유효 기간": "Valid",
    "확인 기간": "Seen",
    "지문": "Fingerprint",
    "현재": "Current",
    "이전 버전": "Previous",
    "곧 만료": "Expiring soon",
    "검증 안 됨": "Unverified",
    "캡처가 인증서 검증을 통과하지 못했습니다 (자체 서명 등)":
        "The capture did not pass certificate verification (self-signed, etc.)",
    "아카이빙·크롤이 진행 중인 사이트입니다 — 완료 후 다시 시도하세요":
        "Archiving or crawling is in progress for this site — try again after "
        "it finishes",
    "사이트 삭제됨: {key} (페이지 {p}개, 스냅샷 {s}개, 크롤 {c}개)":
        "Site deleted: {key} ({p} page(s), {s} snapshot(s), {c} crawl(s))",
    # ---- 검색 (/search) ----
    "아카이브 본문·문서에서 검색…": "Search archived text and documents…",
    "도메인 (선택)": "Domain (optional)",
    "URL당 최신만": "Latest per URL",
    "한국어는 3글자 이상이 정확합니다 — 1~2글자 검색어는 부분일치로 처리되어 결과가 많거나 느릴 수 있습니다. 검색 대상은 페이지 본문과 첨부 문서(PDF·워드·한글 등) 본문입니다.":
        "Korean queries of 3+ characters are most accurate — 1–2 character queries "
        "fall back to substring matching, which can be slow or return many results. "
        "Search covers page text and attached document bodies (PDF, Word, HWP, etc.).",
    "이 환경의 SQLite 빌드에 FTS5 가 없어 검색을 쓸 수 없습니다. 기존 아카이빙은 영향받지 않습니다.":
        "Search is unavailable because this SQLite build lacks FTS5. Existing "
        "archiving is unaffected.",
    "검색어를 입력하세요.": "Enter a search query.",
    "일치하는 결과가 없습니다.": "No matching results.",
    "짧은 검색어라 부분일치로 찾았습니다.": "Short query — matched by substring.",
    "검색 권한이 없습니다": "You do not have permission to search",
    # ---- 검색 인덱스 (시스템 메뉴 카드 / 정합성) ----
    "검색 인덱스": "Search index",
    "비활성": "Disabled",
    "정상": "OK",
    "불일치 {n}개": "{n} inconsistent",
    "미색인 {n}개": "{n} unindexed",
    "색인됨": "Indexed",
    "미색인": "Unindexed",
    "FTS 행": "FTS rows",
    "과소 색인": "Under-indexed",
    "전체 다시 색인": "Reindex all",
    "재색인 중": "Reindexing",
    "이미 전체 다시 색인이 진행 중입니다.": "A full reindex is already in progress.",
    "검색 인덱스 전체 다시 색인을 시작했습니다 — 아래에 진행 상황이 표시됩니다.":
        "Started a full reindex of the search index — progress is shown below.",
    "마지막 전체 다시 색인이 실패했습니다 — 시스템 로그를 확인하세요.":
        "The last full reindex failed — check the system logs.",
    "아카이브 본문과 첨부 문서 본문의 전문 검색 인덱스(FTS5)입니다. 새 스냅샷은 저장 시 자동 색인됩니다. 가져오기·구형 스냅샷, compact 로 첨부 문서가 새로 생긴 스냅샷은 다시 색인이 필요할 수 있습니다. 아래 버튼은 인덱스를 비우고 전체 스냅샷을 다시 색인합니다(첨부 문서 본문 포함) — 스냅샷이 많으면 시간이 걸릴 수 있습니다.":
        "Full-text search index (FTS5) over archived page text and attached "
        "document bodies. New snapshots are indexed automatically on save. "
        "Imported and legacy snapshots, and snapshots whose attached documents "
        "were newly migrated by compact, may need re-indexing. The button below "
        "clears the index and re-indexes every snapshot (including document "
        "bodies) — this can take a while with many snapshots.",
    "검색 인덱스를 비우고 전체 스냅샷을 다시 색인합니다(첨부 문서 본문 포함). 스냅샷이 많으면 시간이 걸릴 수 있습니다. 계속할까요?":
        "Clear the search index and re-index every snapshot (including document "
        "bodies)? This can take a while with many snapshots.",
    "검색 인덱스를 쓸 수 없습니다 — 이 SQLite 빌드에 FTS5 가 없습니다.":
        "Search index is unavailable — this SQLite build lacks FTS5.",
    "검색 인덱스 전체 다시 색인 완료 — 스냅샷 {n}개":
        "Search index rebuilt — {n} snapshot(s)",

    # ---- 최초 설정(setup.html) ----
    "최초 설정": "Initial setup",
    "최초 설정 토큰": "Initial setup token",
    "(WCCG_SETUP_TOKEN)": "(WCCG_SETUP_TOKEN)",
    "이 서버는 최초 설정 보호 토큰을 요구합니다. 아래 작업에 서버 환경변수 WCCG_SETUP_TOKEN 값을 입력하세요.":
        "This server requires an initial-setup protection token. Enter the server's "
        "WCCG_SETUP_TOKEN environment variable value for the actions below.",
    "최초 구동입니다. 새 관리자 계정을 만들거나, 백업 파일에서 복원하거나, 다른 춘추관에서 네트워크로 데이터를 가져올 수 있습니다.":
        "This is the first run. You can create a new admin account, restore from a "
        "backup file, or pull all data over the network from another ChunChuGwan.",
    "네트워크 이전": "Network migration",
    "받는 중": "Receiving",
    "⚠ 평문 http 연결입니다 — 토큰이 노출될 수 있습니다.":
        "⚠ Plain http connection — the token may be exposed.",
    "일부 파일을 받지 못했습니다 ({n}개). 전체 재시도하거나, 무시하고 이전을 마무리할 수 있습니다(빠진 스냅샷 파일은 표시되지 않을 수 있습니다).":
        "Some files could not be received ({n}). You can retry them all, or ignore "
        "and finish the migration (missing snapshot files may not display).",
    # SPA setup — 개수 동적 부분을 뺀 조각 버전
    "일부 파일을 받지 못했습니다. 전체 재시도하거나, 무시하고 이전을 마무리할 수 있습니다(빠진 스냅샷 파일은 표시되지 않을 수 있습니다).":
        "Some files could not be received. You can retry them all, or ignore and "
        "finish the migration (missing snapshot files may not display).",
    "실패한 파일 목록": "Failed files",
    "전체 재시도": "Retry all",
    "빠진 파일을 무시하고 이전을 마무리할까요? 받은 데이터로 서비스를 시작합니다.":
        "Ignore missing files and finish the migration? The service starts with the "
        "received data.",
    "무시하고 이전 종료": "Ignore and finish",
    "관리자 계정 생성": "Create admin account",
    "백업 파일에서 복원": "Restore from backup file",
    "전체 백업(tar.gz)을 올려 그 시점 상태로 복원합니다. 복원 후에는 백업의 계정으로 로그인합니다.":
        "Upload a full backup (tar.gz) to restore to that point. After restoring, log "
        "in with the account from the backup.",
    "다른 춘추관에서 이전": "Migrate from another ChunChuGwan",
    "이전(마이그레이션) 모드를 켠 다른 춘추관의 주소와 발급된 토큰을 입력하면 모든 데이터를 가져옵니다. 받는 쪽은 같은 WCCG_SECRET_KEY 를 써야 외부 사이트 자격증명을 복호화할 수 있습니다.":
        "Enter the address and issued token of another ChunChuGwan that has migration "
        "mode on to pull all its data. The receiving side must use the same "
        "WCCG_SECRET_KEY to decrypt external site credentials.",
    "소스 주소": "Source address",
    "(예: https://NAS주소:8765)": "(e.g. https://NAS-address:8765)",
    "이전 토큰": "Migration token",
    "이전 시작": "Start migration",

    # ---- 이전 모드(system.html) ----
    "다른 춘추관으로 이전": "Migrate to another ChunChuGwan",
    "이전(마이그레이션) 모드": "Migration mode",
    "켜짐": "On",
    "꺼짐": "Off",
    "이전 모드를 켜면 인증 토큰을 발급하고, 새로 설치한 춘추관의 최초 설정 화면에서 이 서버의 주소와 토큰을 입력하면 모든 데이터(아카이브·인증 포함)를 네트워크로 가져갑니다. 이전 모드인 동안에는 이 서버의 모든 스크래핑·스케줄·크롤이 중단됩니다.":
        "Turning on migration mode issues an auth token; on a newly installed "
        "ChunChuGwan's initial setup screen, entering this server's address and token "
        "pulls all data (archive and auth included) over the network. While in "
        "migration mode, all scraping, schedules, and crawls on this server are halted.",
    "토큰은 전체 데이터 접근 권한을 가지므로 안전한 채널로 전달하세요. 받는 쪽이 평문 http 로 접속하면 토큰이 노출될 수 있습니다(https 권장). 받는 쪽은 같은 WCCG_SECRET_KEY 를 써야 외부 사이트 로그인 자격증명을 복호화할 수 있습니다.":
        "The token grants full data access, so deliver it over a secure channel. If "
        "the receiver connects over plain http the token may be exposed (https "
        "recommended). The receiver must use the same WCCG_SECRET_KEY to decrypt "
        "external site login credentials.",
    "발급된 토큰 — 지금만 표시됩니다. 안전하게 복사하세요.":
        "Issued token — shown only now. Copy it securely.",
    "소스 주소는 받는 쪽이 이 서버에 접근할 수 있는 URL 입니다 (예: http://NAS주소:8765). WCCG_PUBLIC_URL 을 설정하면 여기에 표시됩니다.":
        "The source address is the URL the receiver can reach this server at (e.g. "
        "http://NAS-address:8765). It shows here when WCCG_PUBLIC_URL is set.",
    "토큰 발급 시각": "Token issued at",
    "토큰 재발급": "Reissue token",
    "이전 모드를 끄고 스크래핑·스케줄·크롤을 재개할까요? 발급된 토큰은 무효화됩니다.":
        "Turn off migration mode and resume scraping, schedules, and crawls? The "
        "issued token will be invalidated.",
    "이전 모드 끄기 (스크래핑 재개)": "Turn off migration mode (resume scraping)",
    "이전 모드를 켜면 이 서버의 스크래핑·스케줄·크롤이 모두 중단됩니다. 계속할까요?":
        "Turning on migration mode halts all scraping, schedules, and crawls on this "
        "server. Continue?",
    "이전 모드 켜기 + 토큰 발급": "Turn on migration mode + issue token",

    # ---- 이전 관련 라우트 메시지 ----
    "이미 설정이 완료되었습니다": "Setup is already complete",
    "이전 모드가 켜졌습니다 — 스크래핑·스케줄·크롤이 중단됩니다.":
        "Migration mode is on — scraping, schedules, and crawls are halted.",
    "이전 모드를 껐습니다 — 스크래핑·스케줄·크롤이 재개됩니다.":
        "Migration mode is off — scraping, schedules, and crawls resume.",
    "이전(마이그레이션) 모드입니다 — 데이터 이전 중에는 아카이빙할 수 없습니다. 시스템 설정에서 이전 모드를 끄세요.":
        "Migration mode is on — you cannot archive during data migration. Turn off "
        "migration mode in system settings.",
    # 스토리지(blob 백엔드) 마이그레이션 — system/general 화면
    "스토리지": "Storage",
    "blob 저장 백엔드를 로컬과 S3 사이에서 옮깁니다. 마이그레이션 중에는 캡처·스케줄·크롤이 중지되고, 0건 실패로 끝나야 활성 백엔드가 전환됩니다.":
        "Move the blob storage backend between local and S3. Capture, schedules, and "
        "crawls pause during migration, and the active backend switches only after it "
        "finishes with zero failures.",
    "blob 저장 백엔드": "Blob storage backend",
    "활성 백엔드": "Active backend",
    "S3 (객체 저장소)": "S3 (object storage)",
    "로컬 저장소": "Local storage",
    "전환 방향": "Migration direction",
    "로컬 → S3": "Local → S3",
    "S3 → 로컬": "S3 → Local",
    "마이그레이션 시작": "Start migration",
    "마이그레이션 중…": "Migrating…",
    "마이그레이션 중": "Migrating",
    "마이그레이션 중에는 캡처·스케줄·크롤이 중지됩니다. 진행하시겠습니까?":
        "Capture, schedules, and crawls will pause during migration. Continue?",
    "마이그레이션을 완료했습니다.": "Migration complete.",
    "마이그레이션 실패": "Migration failed",
    "일부 파일이 실패했습니다 — 재시도하세요.": "Some files failed — please retry.",
    "실패한 파일": "Failed files",
    "원본 정리 대기": "Source cleanup pending",
    "마이그레이션이 완료되었습니다. 아래 원본을 수동으로 삭제한 뒤 정리 완료를 확인하세요 (원본은 자동 삭제되지 않습니다).":
        "Migration is complete. Manually delete the source below, then confirm cleanup "
        "(the source is not deleted automatically).",
    "원본 위치": "Source location",
    "정리 완료 확인": "Confirm cleanup",
    "원본 정리를 확인했습니다.": "Source cleanup confirmed.",
    # S3 DB 백업 — system/general 화면
    "DB 백업 (S3)": "DB backup (S3)",
    "index.db 와 rules.json 을 S3 의 db-backups/ 에 백업합니다 (S3 모드 전용). 전체 백업의 대체 내구성 수단입니다.":
        "Back up index.db and rules.json to db-backups/ on S3 (S3 mode only) — a "
        "durability alternative to full backups.",
    "S3 모드에서만 사용할 수 있습니다.": "Available in S3 mode only.",
    "마지막 백업": "Last backup",
    "S3 백업 개수": "S3 backup count",
    "목록 조회 오류": "List error",
    "지금 백업": "Back up now",
    "백업 중…": "Backing up…",
    "백업 주기(시간)": "Backup interval (hours)",
    "보존 개수": "Keep count",
    "DB 백업을 완료했습니다.": "DB backup complete.",
    # 첫 구동 분기 + 복구 (setup 마법사·복구-선택·스냅샷 토글, P5b)
    "기존 아카이브 데이터를 발견해 보존했습니다. 관리자 계정을 만들어 시작하세요.":
        "Existing archive data was found and preserved. Create an admin account to start.",
    "고급 — 감지된 데이터를 대체하는 작업": "Advanced — actions that replace detected data",
    "아래 작업은 방금 감지된 기존 데이터를 대체합니다. 의도한 경우에만 사용하세요.":
        "The actions below replace the existing data just detected. Use only if intended.",
    "S3 에 DB 백업이 있습니다. 복원하면 사용자 계정을 포함한 전체가 백업 시점으로 복구됩니다.":
        "A DB backup exists on S3. Restoring recovers everything, including user accounts, "
        "to the backup point.",
    "S3 DB 백업에서 복원": "Restore from S3 DB backup",
    "S3 DB 백업 복원": "Restore S3 DB backup",
    "S3 db-backups/ 의 최신 백업으로 복원합니다. 완료되면 백업의 계정으로 로그인합니다.":
        "Restore from the latest backup in S3 db-backups/. When done, log in with the "
        "backup's account.",
    "S3 DB 백업에서 복원하면 사용자 계정을 포함한 전체 인덱스가 백업 시점으로 채워집니다. 계속할까요?":
        "Restoring from the S3 DB backup fills the entire index — including user accounts — "
        "to the backup point. Continue?",
    "또는 새로 시작 (관리자 생성)": "Or start fresh (create admin)",
    "아카이브 blob 을 발견했지만 인덱스(DB)가 비어 있습니다. 복구모드로 blob 에서 인덱스를 재구축할 수 있습니다 — 복구된 스냅샷은 기본적으로 관리자 전용으로 제한되고, 복구 후 관리자 계정을 만듭니다.":
        "Archive blobs were found but the index (DB) is empty. Recovery mode can rebuild "
        "the index from blobs — recovered snapshots are admin-only by default, and you "
        "create an admin account afterward.",
    "복구모드": "Recovery mode",
    "복구모드 시작": "Start recovery mode",
    "인덱스 재구축 중": "Rebuilding index",
    "복구 재시도": "Retry recovery",
    "복구를 완료했습니다. 복구된 스냅샷은 기본적으로 관리자 전용으로 제한됩니다 — 관리자 계정을 만든 뒤 공개 정책을 선택하세요.":
        "Recovery complete. Recovered snapshots are admin-only by default — create an admin "
        "account, then choose the disclosure policy.",
    "복구 스냅샷 분류 대기": "Recovered snapshots awaiting classification",
    "복구된 스냅샷 {n}개가 보안상 관리자 전용으로 제한되어 있습니다 (로그인 캡처 여부를 알 수 없어 보수적으로 제한).":
        "{n} recovered snapshots are restricted to admins for safety (conservative — whether "
        "they were login captures is unknown).",
    "각 스냅샷 화면에서 개별로 검토해 공개하거나, 아래에서 한 번에 전체 공개로 바꿀 수 있습니다.":
        "Review and expose them individually on each snapshot page, or expose all at once below.",
    "복구 스냅샷 전체 공개": "Expose all recovered snapshots",
    "복구된 스냅샷을 모두 전체 공개로 바꿀까요? 로그인 캡처였을 수 있어 비공개 정보가 노출될 수 있습니다.":
        "Expose all recovered snapshots to everyone? They may have been login captures, so "
        "private information could be disclosed.",
    "복구 스냅샷 {n}개를 전체 공개로 바꿨습니다.": "Exposed {n} recovered snapshots.",
    "접근": "Access",
    "관리자 전용 (제한됨)": "Admin only (restricted)",
    "전체 공개": "Public",
    "전체 공개로 전환": "Make public",
    "관리자 전용으로 제한": "Restrict to admins",
    "이 스냅샷을 관리자 전용으로 제한할까요?": "Restrict this snapshot to admins?",
    "이 스냅샷을 모든 사용자에게 공개할까요? 로그인 캡처였다면 비공개 정보가 노출될 수 있습니다.":
        "Make this snapshot visible to everyone? If it was a login capture, private "
        "information could be disclosed.",
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
# 자격증명 길이 제한 (이름은 MAX_DISPLAY_NAME_LENGTH 과 같은 50자라 위에서 이미 등록됨)
_EN[f"사용자명은 {MAX_USERNAME_LENGTH}자 이하여야 합니다."] = (
    f"The username must be at most {MAX_USERNAME_LENGTH} characters."
)
_EN[f"비밀번호는 {MAX_PASSWORD_LENGTH}자 이하여야 합니다."] = (
    f"The password must be at most {MAX_PASSWORD_LENGTH} characters."
)
_EN[f"토큰은 {MAX_JWT_LENGTH}자 이하여야 합니다."] = (
    f"The token must be at most {MAX_JWT_LENGTH} characters."
)

# ---- SvelteKit SPA 화면 문자열 (#10 i18n 추출) ----
# 단위/조사("개·건·명")는 영어에서 수 뒤에 붙지 않으므로 빈 문자열로 둔다
# (예: "총 10건" → "Total 10").
_EN.update({
    "2단계 인증 설정": "Set up 2FA",
    "2단계 인증(TOTP)": "Two-factor auth (TOTP)",
    "6자리 코드": "6-digit code",
    "SMTP 미설정 — 초대 링크를 직접 전달합니다.": "SMTP not configured — share the invite link directly.",
    "SSO 전용 계정의 2단계 인증은 IdP(Authentik)에서 관리합니다.": "Two-factor auth for SSO-only accounts is managed in the IdP (Authentik).",
    "TOTP QR": "TOTP QR",
    "Touch ID·보안 키·휴대폰 등을 패스워드 로그인의 2단계 인증 수단으로 등록합니다.": "Register Touch ID, a security key, or your phone as a second factor for password login.",
    "URL": "URL",
    "개": "",
    "개당 크기(MB)": "Size each (MB)",
    "건": "",
    "검색 결과가 없습니다.": "No search results.",
    "검색 인덱스가 아직 준비되지 않았습니다.": "The search index isn't ready yet.",
    "고유 용량": "Unique size",
    "구형 스냅샷의 문서가 남아 있습니다. compact 를 실행하면 통합 목록에 반영됩니다.": "Documents from older snapshots remain. Run compact to include them in the unified list.",
    "나란히": "Side by side",
    "내가 대시보드·확장에서 직접 요청한 단발 아카이빙 이력입니다.": "One-off archive runs you requested directly from the dashboard or extension.",
    "등록 중…": "Registering…",
    "등록된 스케줄이 없습니다.": "No schedules.",
    "등록된 태그가 없습니다.": "No tags.",
    "등록된 패스키가 없습니다.": "No passkeys registered.",
    "로그가 없습니다.": "No logs.",
    "마지막": "Last",
    "메일(SMTP)": "Mail (SMTP)",
    "명": "",
    "모바일 스크린샷도 저장": "Also save mobile screenshot",
    "문서 그룹": "Document groups",
    "문서 아카이브 한도": "Document archive limits",
    "문서가 없습니다.": "No documents.",
    "발급된 개인 키가 없습니다.": "No personal keys issued.",
    "발급된 시스템 키가 없습니다.": "No system keys issued.",
    "백업·복원·데이터 이전·재색인·네트워크 태그 편집·SMTP 설정은 이어서 추가됩니다.": "Backup, restore, data migration, reindex, network-tag editing, and SMTP settings are coming next.",
    "백업·복원·데이터 이전·재색인·SMTP 설정은 이어서 추가됩니다.": "Backup, restore, data migration, reindex, and SMTP settings are coming next.",
    "태그 추가": "Add tag",
    "태그 병합": "Merge tags",
    "원본": "Source",
    "대상": "Target",
    "TLS 인증서": "TLS certificates",
    "검증됨": "Verified",
    "보내는 주소": "Sender address",
    "테스트 메일을 보냈습니다.": "Test email sent.",
    "백업·복원·데이터 이전·재색인은 이어서 추가됩니다.": "Backup, restore, data migration, and reindex are coming next.",
    "완료했습니다.": "Done.",
    "백업·복원": "Backup & restore",
    "백업 복원": "Restore from backup",
    "정말 복원하시겠습니까? 현재 데이터가 백업 시점으로 교체됩니다.": "Restore now? Current data will be replaced with the backup.",
    "가져오기 모드": "Import mode",
    "덮어쓰기": "Overwrite",
    "데이터 이전·재색인은 이어서 추가됩니다.": "Data migration and reindex are coming next.",
    "최적화했습니다.": "Optimized.",
    "최적화할 항목이 없습니다.": "Nothing to optimize.",
    "최적화 중…": "Optimizing…",
    "백업 준비중…": "Preparing backup…",
    "내보내기 준비중…": "Preparing export…",
    "재색인 실패": "Reindex failed",
    "재색인을 완료했습니다.": "Reindex complete.",
    "유지보수": "Maintenance",
    "저장공간·검색": "Storage & search",
    "검색 인덱스 전체 재색인": "Reindex search (full)",
    "데이터 이전은 이어서 추가됩니다.": "Data migration is coming next.",
    "데이터 이전": "Data migration",
    "이전 모드 켜짐": "Migration mode on",
    "이전 모드 꺼짐": "Migration mode off",
    "이 토큰은 다시 표시되지 않습니다 — 받는 쪽에 안전하게 전달하세요.": "This token is shown only once — share it securely with the receiving instance.",
    "이전 모드 끄기": "Turn migration off",
    "이전 모드 켜기": "Turn migration on",
    "보관 시간(시간)": "Retention (hours)",
    "본문 비교": "Text diff",
    "비우면 이메일이 표시됩니다.": "Leave empty to show your email.",
    "사설 IP(로컬 네트워크) 대상은 네트워크 태그가 필요하며, 자격증명 연결은 추후 지원됩니다.": "Private-IP (local network) targets need a network tag; credential linking is coming later.",
    "사용 중": "In use",
    "사용 중입니다.": "In use.",
    "사이트 검색": "Search sites",
    "사이트 상세": "Site detail",
    "사이트 아카이브 기본값": "Site archive defaults",
    "사이트 아카이브 최대값": "Site archive limits",
    "사이트 아카이브 회차": "Site archive runs",
    "사이트 재아카이빙": "Re-archive site",
    "사이트 전체 아카이브 (같은 호스트)": "Archive whole site (same host)",
    "새 패스키 이름 (예: 맥북 Touch ID)": "New passkey name (e.g. MacBook Touch ID)",
    "세션을 로그아웃했습니다.": "Session logged out.",
    "스냅샷 이력": "Snapshot history",
    "스냅샷당 수": "Per snapshot",
    "스케줄 등록": "Add schedule",
    "스케줄 해제": "Remove schedule",
    "아래 키를 지금 복사하세요. 다시 표시되지 않습니다.": "Copy the key now. It won't be shown again.",
    "아직 아카이브가 없습니다.": "No archives yet.",
    "아직 요청한 아카이빙이 없습니다.": "No archive requests yet.",
    "아카이빙 등록": "Queue archive",
    "역할": "Role",
    "이 권한 그룹을 삭제할까요?": "Delete this permission group?",
    "이 사이트 삭제": "Delete this site",
    "이 사이트의 모든 페이지·스냅샷·크롤·스케줄을 삭제할까요? 되돌릴 수 없습니다.": "Delete all pages, snapshots, crawls, and schedules for this site? This cannot be undone.",
    "이 키를 폐기할까요?": "Revoke this key?",
    "이 페이지 삭제": "Delete this page",
    "이 페이지의 모든 스냅샷을 삭제할까요? 되돌릴 수 없습니다.": "Delete all snapshots for this page? This cannot be undone.",
    "이름(영문/숫자/_)": "Name (letters/digits/_)",
    "인증 앱(Google Authenticator 등)으로 QR 을 스캔하거나 키를 입력한 뒤, 표시되는 코드를 입력하세요.": "Scan the QR with an authenticator app (e.g. Google Authenticator) or enter the key, then type the code it shows.",
    "재시도 대기(초, 쉼표)": "Retry backoff (seconds, comma-separated)",
    "저장 용량": "Storage size",
    "저장했습니다.": "Saved.",
    "전체 도메인": "All domains",
    "전체 레벨": "All levels",
    "전체 상태": "All statuses",
    "전체 출처": "All sources",
    "절감 용량": "Saved space",
    "정말 탈퇴할까요? 이 작업은 되돌릴 수 없습니다.": "Really withdraw? This cannot be undone.",
    "지연(초)": "Delay (seconds)",
    "차이": "Diff",
    "참조": "Reference",
    "첨부 문서": "Attached documents",
    "초대 링크를 직접 전달하세요.": "Share the invite link directly.",
    "초대 메일을 보냈습니다.": "Invitation email sent.",
    "초대 메일을 다시 보냈습니다.": "Invitation email resent.",
    "재생성": "Regenerate",
    "총": "Total",
    "최대 페이지": "Max pages",
    "최신 2개 비교": "Compare latest two",
    "최신만": "Latest only",
    "캡처": "Capture",
    "커스텀 그룹 추가": "Add custom group",
    "코드 만료(분)": "Code expiry (minutes)",
    "콘텐츠 동일해도 강제 저장": "Force save even if unchanged",
    "크롬 확장 등 본인 도구가 사용할 토큰입니다. 권한은 내 역할 범위 안에서만 부여됩니다.": "A token for your own tools like the Chrome extension. Permissions are granted only within your role's scope.",
    "키 이름": "Key name",
    "탈퇴하면 모든 세션이 종료되고 같은 이메일로 재가입할 수 없습니다 (관리자 삭제 전까지).": "Withdrawing ends all sessions and blocks re-signup with the same email (until an admin deletes the account).",
    "패스키를 등록했습니다.": "Passkey registered.",
    "페이지 재아카이빙": "Re-archive page",
    "편집": "Edit",
    "표시 라벨": "Display label",
    "픽셀 차이": "Pixel diff",
    "확인 이메일": "Confirmation email",
    "확인을 위해 이메일 입력": "Enter your email to confirm",
    "확장 자격증명": "Extension credentials",
    "확장(브라우저) 캡처가 포함되어 스크린샷 비교는 제공하지 않습니다 (렌더 환경 차이).": "Includes extension (browser) capture, so screenshot comparison isn't available (rendering-environment differences).",
    "회원 가입 허용": "Allow sign-up",
    # ---- 새 아카이빙 폼 리디자인 (archive/new) ----
    "캡처 범위": "Scope",
    "단일 페이지": "Single page",
    "사이트 전체": "Whole site",
    "없음": "None",
    "1회": "Once",
    "공개 주소": "Public address",
    "사설 IP — 태그 필요": "Private IP — tag required",
    "루프백 — 아카이빙 불가": "Loopback — can't archive",
    "아카이빙할 페이지의 전체 주소를 입력하세요.": "Enter the full URL of the page to archive.",
    "입력한 URL 한 페이지만 스냅샷으로 저장합니다.": "Captures only this single URL as a snapshot.",
    "같은 호스트의 경로 프리픽스 이하를 모두 따라가 저장합니다. 비우면 시스템 기본값이 적용됩니다.": "Follows every page under the same host and path prefix. Leave blank to use system defaults.",
    "기본값은 본문 해시가 바뀐 경우에만 새 스냅샷을 만듭니다.": "By default a new snapshot is created only when the content hash changes.",
    # ---- 공통 컴포넌트 (Pager·PageSize 등) ----
    "페이지 이동": "Pagination",
    "페이지당 항목 수": "Items per page",
    # 알림 토스트(run(fn, ok) 의 ok 인자 — t(변수)로 번역)
    "표시 이름을 변경했습니다.": "Display name updated.",
    "인증 앱에 등록한 뒤 코드를 입력하세요.": "Register it in your authenticator app, then enter the code.",
    "2단계 인증을 켰습니다.": "Two-factor authentication enabled.",
    "2단계 인증을 껐습니다.": "Two-factor authentication disabled.",
    "패스키를 삭제했습니다.": "Passkey deleted.",
    # 아카이빙 상태 라벨(STATUS_LABELS — t(변수)로 번역)
    "새 스냅샷": "New snapshot",
    "변경됨": "Changed",
    "변경 없음": "Unchanged",
    "강제 저장": "Forced save",
    # 에러 화면(+error.svelte)·diff 비교 불가 안내
    "문제가 발생했습니다": "Something went wrong",
    "요청한 페이지를 표시할 수 없습니다.": "The requested page could not be displayed.",
    "비교할 수 없습니다.": "Comparison is not available.",
})

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
