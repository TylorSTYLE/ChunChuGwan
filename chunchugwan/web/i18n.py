"""웹 UI 다국어 (i18n).

한국어 원문이 곧 메시지 키다 (gettext msgid 방식). 한국어는 카탈로그 없이
원문 그대로 출력하고, 다른 언어는 "한국어 원문 → 번역" dict 하나로 추가한다.
카탈로그에 없는 문자열은 원문(한국어)으로 폴백한다.

- 로케일 결정: `wccg_lang` 쿠키(헤더의 언어 선택) → Accept-Language → 한국어.
- 같은 원문이 문맥에 따라 다르게 번역돼야 하면 ctx 를 쓴다 —
  카탈로그 키는 "{ctx}|{원문}" (예: "diff|이전").
- 새 언어 추가: SUPPORTED_LOCALES·LOCALE_NAMES 에 코드/이름을 등록하고
  CATALOGS 에 번역 dict, _INTERVAL_UNITS 에 주기 단위 표기를 추가한다.
"""

from __future__ import annotations

from fastapi import Request

from .. import config
from ..auth import MAX_DISPLAY_NAME_LENGTH

LANG_COOKIE = "wccg_lang"
LANG_COOKIE_MAX_AGE = 365 * 86400

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
    "로그": "Logs",
    "사용자": "Users",
    "시스템": "System",
    "계정": "Account",
    "로그아웃": "Log out",
    "언어": "Language",
    "테마 전환 (자동 → 라이트 → 다크)": "Switch theme (auto → light → dark)",
    "테마: 자동": "Theme: auto",
    "테마: 라이트": "Theme: light",
    "테마: 다크": "Theme: dark",
    "시간 표시 전환 (로컬 ↔ UTC)": "Switch time display (local ↔ UTC)",
    "시간: UTC": "Time: UTC",
    "시간: 로컬": "Time: local",
    "시각": "Time",
    "상태": "Status",
    "용량": "Size",
    "소요": "Duration",
    "출처": "Source",
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
    # ---- 역할 라벨 (db.ROLE_LABELS) ----
    "관리자": "Admin",
    "아카이브": "Archiver",
    "보기 전용": "Viewer",
    "차단됨": "Blocked",
    # ---- 목록 (index) ----
    "아카이브 목록": "Archived pages",
    "아카이빙이 백그라운드에서 시작되었습니다": "Archiving started in the background",
    "완료되면 목록이 자동 갱신됩니다.": "The list refreshes automatically when it finishes.",
    "아카이브된 페이지가 없습니다.": "No archived pages yet.",
    "시작하려면": "To get started, use the",
    "메뉴나": "menu or the",
    "명령을 사용하세요.": "command.",
    "마지막 캡처": "Last capture",
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
    "원본 URL": "Source URL",
    "크기": "Size",
    "메타데이터 없음": "Metadata not found",
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
    # ---- 로그 (logs) ----
    "아카이브 로그": "Archive logs",
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
    "아카이브 루트": "Archive root",
    "저장 공간": "Storage",
    "페이지": "Pages",
    "확인 기록": "Checks",
    "스냅샷 파일": "Snapshot files",
    "공유 자원": "Shared resources",
    "합계": "Total",
    "유지 관리": "Maintenance",
    "저장 공간 압축": "Storage compaction",
    "대상 {n}개": "{n} pending",
    "대상 없음": "None pending",
    "구형 스냅샷을 압축 저장 형태(공유 자원 추출 + HTML gzip + 스크린샷 WebP)로 변환합니다. 내용 보존 변환이라 스냅샷이 담는 정보는 그대로이며, 여러 번 실행해도 안전합니다(멱등). 새 스냅샷은 저장 시점에 자동으로 압축됩니다.":
        "Converts legacy snapshots to the compact storage form (shared-resource "
        "extraction + gzipped HTML + WebP screenshots). The conversion preserves "
        "content — snapshots keep exactly the same information — and is idempotent, "
        "so running it multiple times is safe. New snapshots are compacted on save.",
    "기존 스냅샷 파일을 압축 저장 형태로 변환합니다. 스냅샷이 많으면 시간이 걸릴 수 있습니다. 계속할까요?":
        "Convert existing snapshot files to the compact storage form? "
        "With many snapshots this can take a while.",
    "압축 실행": "Run compaction",
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
    "압축할 스냅샷이 없습니다.": "No snapshots to compact.",
    "스냅샷 {n}개 모두 이미 압축 형태입니다.": "All {n} snapshots are already in compact form.",
    "압축 실패: {e}": "Compaction failed: {e}",
    "압축 완료: 변환 {converted}/{total}개 · 공유 자원 {externalized}개 추출 · {before} → {after} ({saved} 절약)":
        "Compaction finished: converted {converted}/{total} · extracted "
        "{externalized} shared resources · {before} → {after} (saved {saved})",
    "복원 실패: {e}": "Restore failed: {e}",
    "복원 완료 (백업: {created_at}, 페이지 {pages}개, 스냅샷 {snapshots}개)":
        "Restore complete (backup: {created_at}, {pages} pages, {snapshots} snapshots)",
    "가져오기 실패: {e}": "Import failed: {e}",
    "가져오기 완료 [{mode}]: 페이지 +{pages}, 스냅샷 +{snapshots} (스킵 {skipped}), 확인 기록 +{checks}":
        "Import complete [{mode}]: pages +{pages}, snapshots +{snapshots} "
        "(skipped {skipped}), checks +{checks}",
    "알 수 없는 모드: {mode}": "Unknown mode: {mode}",
    # ---- 사용자 관리 (users) ----
    "사용자 관리": "User management",
    "관리자=전체 관리, 아카이브=아카이빙 가능, 보기 전용=열람만, 차단됨=접근 불가. 차단하면 해당 사용자의 모든 세션이 즉시 로그아웃됩니다. 최초 등록된 관리자의 권한은 변경할 수 없습니다.":
        "Admin = full control, Archiver = can archive, Viewer = read-only, Blocked = "
        "no access. Blocking logs out all of the user's sessions immediately. The "
        "founder admin's role cannot be changed.",
    "활성 세션": "Active sessions",
    "가입일": "Joined",
    "권한 변경": "Change role",
    "(나)": "(me)",
    "최초 관리자": "Founder",
    "패스키": "Passkey",
    "변경 불가": "Locked",
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
    "알 수 없는 역할: {role}": "Unknown role: {role}",
    "사용자 없음": "User not found",
    "최초 관리자의 권한은 변경할 수 없습니다.": "The founder admin's role cannot be changed.",
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
    "계정과 모든 세션·2FA 등록 정보가 즉시 삭제됩니다. 되돌릴 수 없습니다.":
        "Your account, all sessions, and 2FA registrations are deleted immediately. "
        "This cannot be undone.",
    "정말 계정을 삭제할까요? 되돌릴 수 없습니다.":
        "Really delete your account? This cannot be undone.",
    "패스워드 확인": "Confirm password",
    "확인을 위해 이메일({email})을 입력": "Type your email ({email}) to confirm",
    "계정 삭제": "Delete account",
    "사용자 이름을 변경했습니다.": "Display name updated.",
    "패스워드를 변경했습니다. 다른 기기의 세션은 로그아웃되었습니다.":
        "Password changed. Sessions on other devices were logged out.",
    "SSO 전용 계정은 패스워드가 없습니다. IdP(Authentik)에서 관리하세요.":
        "SSO-only accounts have no password. Manage it in your IdP (Authentik).",
    "현재 패스워드가 올바르지 않습니다.": "The current password is incorrect.",
    "새 패스워드가 서로 일치하지 않습니다.": "The new passwords do not match.",
    "관리자 계정은 삭제할 수 없습니다.": "Admin accounts cannot be deleted.",
    "패스워드가 올바르지 않습니다.": "The password is incorrect.",
    "확인 이메일이 일치하지 않습니다.": "The confirmation email does not match.",
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
}

# 설정값이 들어간 검증 메시지 — 원문이 f-string 이라 임포트 시점 값으로 키를 만든다
_EN[f"패스워드는 {config.MIN_PASSWORD_LENGTH}자 이상이어야 합니다."] = (
    f"The password must be at least {config.MIN_PASSWORD_LENGTH} characters."
)
_EN[f"이름은 {MAX_DISPLAY_NAME_LENGTH}자 이하여야 합니다."] = (
    f"The name must be at most {MAX_DISPLAY_NAME_LENGTH} characters."
)

CATALOGS: dict[str, dict[str, str]] = {"en": _EN}


def resolve_locale(request: Request) -> str:
    """요청의 표시 언어 — 쿠키(언어 선택) → Accept-Language → 기본(ko)."""
    cookie = request.cookies.get(LANG_COOKIE)
    if cookie in SUPPORTED_LOCALES:
        return cookie
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
