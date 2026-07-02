"""전역 설정. 환경변수 WCCG_ROOT(아카이브 위치), WCCG_HOST(대시보드 바인딩) 오버라이드 가능."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

# .env 파일 로드 — 로컬 실행(`uv run wccg`) 편의용. 실제 환경변수가 항상 우선이며
# (override=False), python-dotenv 미설치 시 조용히 건너뛴다. 도커 이미지엔 .env 를
# 동봉하지 않으므로(.dockerignore) 컨테이너에선 no-op 이고, compose 는 각 서비스의
# env_file 로 주입한다. 모든 WCCG_* 읽기가 이 모듈에 있으므로 여기서 한 번 로드하면
# 전 모듈에 적용된다.
try:
    from dotenv import find_dotenv, load_dotenv
except ImportError:  # python-dotenv 없음 — 환경변수만 사용
    pass
else:
    load_dotenv(find_dotenv(usecwd=True))  # CWD 부터 위로 .env 탐색 (없으면 no-op)

logger = logging.getLogger(__name__)

ARCHIVE_ROOT = Path(os.environ.get("WCCG_ROOT", "archive")).resolve()
SITES_DIR = ARCHIVE_ROOT / "sites"
DB_PATH = ARCHIVE_ROOT / "index.db"
CACHE_DIR = ARCHIVE_ROOT / "cache"          # 파생 산출물(픽셀 diff 등), 재생성 가능
RULES_PATH = ARCHIVE_ROOT / "rules.json"    # 도메인별 정규화 룰
RESOURCES_DIR = ARCHIVE_ROOT / "resources"  # 스냅샷 간 공유 자원 CAS (resources.py)
DOCUMENTS_DIR = ARCHIVE_ROOT / "documents"  # 문서 파일 CAS (documents.py — 인증 라우트 전용)
# S3 백엔드 read-through 캐시 위치 — 픽셀 diff 캐시(CACHE_DIR)와 분리해 compact 의
# CACHE_DIR rmtree 영향을 받지 않게 한다. 로컬 백엔드에서는 쓰이지 않는다.
BLOB_CACHE_DIR = ARCHIVE_ROOT / "blobcache"

# ---- S3/MinIO blob 백엔드 (선택 — blobstore.S3BlobStore) ----
# WCCG_S3_* 중 식별 정보(endpoint·bucket·access key·secret) 중 하나라도 있으면
# S3 백엔드를 요청한 것으로 보고, 필수 세트가 완전해야 활성화한다 (일부만 설정 시
# 조용히 로컬로 폴백하지 않고 부팅 시 명확히 실패 — 데이터 혼선 방지). 비밀값은
# env 전용으로 DB·로그·예외 메시지에 노출하지 않는다.
S3_ENDPOINT_URL = os.environ.get("WCCG_S3_ENDPOINT_URL", "").strip()
S3_BUCKET = os.environ.get("WCCG_S3_BUCKET", "").strip()
S3_REGION = os.environ.get("WCCG_S3_REGION", "").strip() or "us-east-1"
S3_ACCESS_KEY_ID = os.environ.get("WCCG_S3_ACCESS_KEY_ID", "").strip()
S3_SECRET_ACCESS_KEY = os.environ.get("WCCG_S3_SECRET_ACCESS_KEY", "")
S3_FORCE_PATH_STYLE = os.environ.get("WCCG_S3_FORCE_PATH_STYLE", "on") != "off"
S3_PREFIX = os.environ.get("WCCG_S3_PREFIX", "").strip()
# read-through 캐시 용량 상한 (MB) — 초과 시 LRU 제거
BLOB_CACHE_MAX_MB = int(os.environ.get("WCCG_BLOB_CACHE_MAX_MB", "2048"))
# 로컬↔S3 마이그레이션 동시 전송 워커 수 (파일 단위 copy 병렬도). 네트워크 I/O
# 바운드라 동시 전송으로 크게 빨라진다. 호출부에서 [1, 16] 으로 클램핑한다.
S3_MIGRATION_WORKERS = int(os.environ.get("WCCG_S3_MIGRATION_WORKERS", "4"))

# ---- S3 DB 백업 (db_backup.py — S3 모드에서 index.db+rules.json 을 db-backups/ 로) ----
# 주기(시간)와 보존 개수는 시스템 설정으로 변경하며, 오염·범위 밖이면 기본값으로
# 클램핑한다 (db.db_backup_interval_hours / db_backup_keep).
DB_BACKUP_INTERVAL_HOURS_DEFAULT = 24       # 정기 백업 주기 (시간)
DB_BACKUP_INTERVAL_HOURS_MIN = 1
DB_BACKUP_INTERVAL_HOURS_MAX = 720          # 30일
DB_BACKUP_KEEP_DEFAULT = 14                 # 보존할 최신 백업 개수
DB_BACKUP_KEEP_MIN = 1
DB_BACKUP_KEEP_MAX = 365

PAGE_LOAD_TIMEOUT_MS = 30_000
# load 도달 후 networkidle 추가 대기 상한 — 분석 스크립트·롱폴링이 있는
# 페이지는 networkidle 에 영영 도달하지 않으므로 짧게 기다리고 진행한다
NETWORK_IDLE_TIMEOUT_MS = 5_000
RESOURCE_FETCH_TIMEOUT_MS = 5_000   # 자원(이미지·CSS·폰트) 1개 인라인 fetch 타임아웃
RESOURCE_FETCH_CONCURRENCY = 6      # 자원 인라인 fetch 동시 실행 수
# 자원 인라인 전체(브라우저 fetch 루프) 데드라인. 자원별 AbortSignal.timeout 만으로는
# headful 실제 Chrome 에서 일부 fetch 가 끝내 안 끊겨 page.evaluate 가 무한 대기할 수
# 있어(page.evaluate 는 set_default_timeout 무시) 전체 상한을 둔다. JS 가 이 시각에
# 부분결과로 resolve 하고, Python 은 wait_for_function 으로 한 번 더 강제(백스톱)한다.
INLINE_OVERALL_TIMEOUT_MS = 60_000
# 한 스냅샷에서 인라인 실패 후 재시도(context.request)·과거캡처 폴백으로 받을 자원
# 개수 상한. 같은 @font-face url() 이 CSS 에 수백~수천 번 반복되는 페이지는 failed
# 목록이 폭발해, 중복 제거 후에도 남는 고유 자원이 많으면 거대 페이로드를
# page.evaluate(_APPLY_INLINE_JS)로 넘기다 메모리 폭증·hang 을 일으킨다. 상한 초과분은
# 원본 URL 을 유지한다 (뷰어는 그 자원만 라이브로 못 받을 뿐 본문은 정상).
RESOURCE_INLINE_MAX_COUNT = 300
HTTPS_PROBE_TIMEOUT_SECONDS = 10  # http URL 등록 시 https 지원 확인(승격 프로브) 타임아웃
# 인증서 수집 결과의 (host, port) TTL 캐시 — 크롤이 같은 호스트를 페이지마다
# 핸드셰이크하지 않게 한다 (certs.py)
CERT_CACHE_TTL_SECONDS = 600
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"

# ---- 캡처 엔진 (봇 탐지 우회용 스텔스 옵션, capture.py / browser_engine.py) ----
# WCCG_CAPTURE_ENGINE: 'playwright'(기본) | 'patchright'.
#   patchright 는 Playwright sync API 드롭인 패치로, Cloudflare 등이 쓰는
#   CDP Runtime.enable 기반 봇 탐지를 우회한다 (`uv sync --extra stealth` 로 설치,
#   미설치 시 playwright 로 자동 폴백). Cloudflare Turnstile 같은 관리형 챌린지는
#   엔진만으로는 부족하고 아래 헤드풀 + real Chrome + Xvfb 가상 디스플레이가
#   사실상 필요하다 (README 참조).
CAPTURE_ENGINE = (os.environ.get("WCCG_CAPTURE_ENGINE", "playwright").strip()
                  or "playwright")
# WCCG_CAPTURE_HEADFUL=on: 헤드리스 대신 헤드풀로 기동 (서버에선 Xvfb 전제).
# 기본 off — 헤드리스 유지 (기존 동작).
CAPTURE_HEADFUL = os.environ.get("WCCG_CAPTURE_HEADFUL", "off") == "on"
# WCCG_CAPTURE_CHANNEL: 비우면 번들 chromium, 'chrome' 이면 시스템 real Chrome.
# 진짜 Chrome 의 TLS/HTTP2 지문이라 네트워크 레벨 탐지에 강하다.
CAPTURE_CHANNEL = os.environ.get("WCCG_CAPTURE_CHANNEL", "").strip()
# 헤드풀 스텔스 경로에선 고정 UA 강제가 real Chrome UA/Client Hints 와 불일치해
# 오히려 탐지 신호가 된다 — 기본은 헤드풀일 때 UA 오버라이드 해제. on 이면 강제.
CAPTURE_FORCE_USER_AGENT = os.environ.get("WCCG_CAPTURE_FORCE_UA", "off") == "on"
# 챌린지 자동 통과 대기 — 스텔스 캡처(patchright/headful)에서 비상호작용 챌린지
# (Cloudflare JS 챌린지 등)는 몇 초 뒤 자동 통과한다. 챌린지가 감지되면 이 시간만큼
# 폴링하며 풀리길 기다리고, 풀리면 캡처를 진행한다(초과 시 차단으로 보고 실패).
# 헤드리스 기본 경로는 자동 통과 가망이 없어 대기하지 않고 즉시 실패한다(기존 동작).
CHALLENGE_WAIT_SECONDS = int(os.environ.get("WCCG_CHALLENGE_WAIT_SECONDS", "25"))
CHALLENGE_WAIT_POLL_MS = 2000

# 사람 보조 챌린지 해결 (live_challenge.py) — 위 자동 대기로도 안 풀린 인터랙티브
# 챌린지(클릭/입력이 필요한 Turnstile 등)를 사람이 대시보드에서 직접 조작해
# 통과시키는 최후 수단. 기본 off. 스텔스(patchright/headful)일 때만 의미가 있다.
# 켜면 worker 가 그 작업의 캡처 스레드를 점유한 채(= 큐 진행 일시중단) 사람
# 입력을 기다린다 — 개인 도구 전제의 단일 세션 직렬화. 데이터센터 IP 평판으로는
# 사람이 눌러도 통과가 보장되지 않는다(README 참조).
LIVE_CHALLENGE = os.environ.get("WCCG_LIVE_CHALLENGE", "off") == "on"
LIVE_CHALLENGE_TIMEOUT_SECONDS = int(
    os.environ.get("WCCG_LIVE_CHALLENGE_TIMEOUT_SECONDS", "300"))
LIVE_SHOT_INTERVAL_MS = 800     # 라이브 스크린샷 갱신 간격 (worker→화면)
LIVE_POLL_INTERVAL_MS = 300     # 입력 명령 폴링 간격 (화면→worker)
LIVE_VIEWPORT_W = 1280          # 라이브 세션 뷰포트 (좌표 매핑 단순화용 고정)
LIVE_VIEWPORT_H = 800

# ---- AI 자동 챌린지 해결 (B 단계 — ai_challenge.py) ----
# 자동 통과 대기(A)로도 안 풀린 '양성 인터스티셜'(동의·연령 확인·"계속하려면
# 클릭")을, 비전 분석 가능한 OpenAI 호환 LLM 으로 스크린샷을 판독해 마우스/키보드
# 입력을 대신 수행함으로써 통과시킨다. 못 풀면 사람 개입(C, live_challenge)으로
# 캐스케이드한다. 모든 운영값은 시스템 설정(settings)에서 관리하며(db.ai_challenge_settings),
# 여기 상수는 미설정·오염 시의 기본값과 클램프 범위다. 기본 비활성(opt-in).
AI_CHALLENGE_ENABLED_DEFAULT = False
AI_CHALLENGE_MAX_ROUNDS_DEFAULT = 3
AI_CHALLENGE_MAX_ROUNDS_MIN = 1
AI_CHALLENGE_MAX_ROUNDS_MAX = 10
AI_CHALLENGE_VERDICT_DELAY_MS_DEFAULT = 1500
AI_CHALLENGE_VERDICT_DELAY_MS_MIN = 0
AI_CHALLENGE_VERDICT_DELAY_MS_MAX = 15000
AI_CHALLENGE_MAX_ACTIONS_DEFAULT = 10
AI_CHALLENGE_MAX_ACTIONS_MIN = 1
AI_CHALLENGE_MAX_ACTIONS_MAX = 30
# 로컬 무거운 모델은 첫 모델 로드+연산에 최대 수 분이 걸릴 수 있어 read 타임아웃을
# 넉넉히 둔다 (httpx 단일 timeout 이라 connect/read/write 에 모두 적용). 기본 180초
# (3분 모델 로드 수용), 상한 300초.
AI_CHALLENGE_REQUEST_TIMEOUT_DEFAULT = 180
AI_CHALLENGE_REQUEST_TIMEOUT_MIN = 5
AI_CHALLENGE_REQUEST_TIMEOUT_MAX = 300
AI_CHALLENGE_SUCCESS_RECHECK_DEFAULT = True

# 편집 가능한 프롬프트 템플릿 — 출력 규약·뷰포트 설명·JSON 스키마까지 전부
# 관리자가 시스템 설정에서 편집할 수 있다. 미설정이면 아래 기본값으로 시드한다.
# 치환 토큰(ai_challenge 가 .replace 로 채움 — JSON 예시의 중괄호와 섞여 있어
# str.format 은 쓰지 않는다): {viewport_w} {viewport_h} {url} {title}
# {round_index} {max_rounds} {last_attempt} (판정 프롬프트는 추가로 {actions_taken}).
DEFAULT_AI_ACTION_PROMPT = (
    "당신은 웹 아카이빙 캡처 도중 나타난 '사람 확인' 게이트(동의 / 연령 확인 / "
    "\"계속하려면 클릭\" 같은 양성 인터스티셜)를 사람 대신 통과시키는 보조자다. "
    "첨부된 스크린샷을 보고 통과에 필요한 입력 동작을 결정하라.\n"
    "좌표계: 좌상단 (0,0), 정수 픽셀. 뷰포트는 {viewport_w}×{viewport_h}. 모든 "
    "좌표는 이 범위 안이어야 한다.\n"
    "현재 페이지: {url} · 제목: {title} · 라운드 {round_index}/{max_rounds}\n"
    "직전 시도 결과: {last_attempt}\n"
    "가능한 동작은 다음뿐이다(type·key 는 모두 키보드 입력): click(좌클릭 x,y) / "
    "type(텍스트 입력 text) / key(특수키 1회 — Enter, Tab, Escape 등 Playwright "
    "키명) / drag(좌클릭 드래그 from{x,y}→to{x,y}).\n"
    "오직 아래 JSON 객체 하나만 출력하라. 마크다운 펜스나 설명 문장을 절대 "
    "덧붙이지 마라.\n"
    "{\"analysis\":\"무엇이 보이고 어떻게 통과시킬지\",\"actions\":"
    "[{\"type\":\"click\",\"x\":0,\"y\":0,\"delay_ms\":0}],\"giveup\":false,"
    "\"reason\":null}\n"
    "입력이 필요 없거나 어떤 동작으로도 통과시킬 수 없다고 판단하면 actions 를 "
    "빈 배열로 두고 giveup 을 true, reason 에 사유를 적어라."
)
DEFAULT_AI_VERDICT_PROMPT = (
    "당신은 방금 수행된 입력 동작이 '사람 확인' 게이트를 실제로 통과시켰는지 "
    "판정한다. 아래 스크린샷은 동작 수행 후 잠시 대기한 뒤의 화면이다.\n"
    "현재 페이지: {url} · 제목: {title} · 라운드 {round_index}/{max_rounds}\n"
    "방금 수행한 동작: {actions_taken}\n"
    "게이트가 사라지고 본래 콘텐츠가 보이면 success, 아직 게이트가 남아 추가 "
    "입력으로 통과 가능해 보이면 continue, 통과 불가(사람 개입 필요)면 fail.\n"
    "오직 아래 JSON 객체 하나만 출력하라. 마크다운 펜스나 설명을 덧붙이지 마라.\n"
    "{\"analysis\":\"화면 상태 판단\",\"verdict\":\"success\",\"reason\":null}"
)

# ---- 모바일 해상도 스크린샷 (capture.py) ----
# 데스크탑 스크린샷과 별도로, 같은 URL 을 안드로이드 크롬으로 위장한 모바일
# 컨텍스트(모바일 UA·뷰포트·터치)로 한 번 더 열어 전체 페이지 스크린샷을 찍는다.
# User-Agent 는 컨텍스트 생성 옵션이라 로드된 페이지의 뷰포트만 바꿔서는 바꿀 수
# 없으므로, 같은 브라우저에 모바일 컨텍스트를 새로 띄워 재캡처한다 (= 재네비게이션).
# 활성화 여부는 시스템 설정(db.mobile_screenshot_enabled)에서 켠다. 해상도 기준은
# 모던 안드로이드 폰의 CSS 뷰포트(390 × 844).
MOBILE_SCREENSHOT_WIDTH = 390
MOBILE_SCREENSHOT_HEIGHT = 844
MOBILE_SCREENSHOT_SETTLE_MS = 400   # 로드 후 반응형 재배치·지연 로드 정착 여유
# 모바일 캡처에 쓸 User-Agent — 안드로이드 크롬 (Chrome Mobile). 이 UA 로 서버가
# 모바일 페이지를 내려주고, isMobile/hasTouch 와 함께 모바일 레이아웃으로 흐른다.
MOBILE_SCREENSHOT_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)

# ---- 저장 압축 (resources.py) ----
SCREENSHOT_WEBP_QUALITY = 85    # 스크린샷 PNG → WebP 변환 품질 (손실 압축)
RESOURCE_MIN_BYTES = 4096       # 이보다 작은 data URI 자원은 추출하지 않고 인라인 유지
# 고아 자원 정리(optimize.py sweep)에서 최근 생성·갱신 파일을 건너뛰는 유예(초) —
# 자원 파일 저장과 스냅샷 커밋 사이에 있는 진행 중 캡처와의 경합 방지
RESOURCE_ORPHAN_GRACE_SECONDS = 3600

# ---- 확장 클라이언트 캡처 적재 (ingest.py / api_routes) ----
# POST /api/v1/ingest 업로드 본문 상한 (Content-Length 기준, DoS 방지)
INGEST_MAX_MB = int(os.environ.get("WCCG_INGEST_MAX_MB", "50"))
INGEST_MAX_BYTES = INGEST_MAX_MB * 1024 * 1024

# ---- 링크된 문서 파일 아카이빙 (documents.py) ----
# 페이지가 링크한 문서(PDF·워드·한글 등)를 문서 CAS(documents/)에 저장하고
# 스냅샷은 snapshot_documents 행과 meta.json 의 documents 목록으로 참조한다.
DOCUMENT_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".hwp", ".hwpx", ".odt", ".odp", ".ods", ".rtf",
    ".pages", ".key", ".numbers", ".epub", ".zip",
)
# 한도 기본값과 허용 범위 — 시스템 설정(settings 테이블, 대시보드 시스템
# 화면)이 우선하며, 값 해석·클램핑은 documents.limits 가 맡는다 (오염 시 기본값).
DOCUMENT_MAX_COUNT_DEFAULT = 20             # 스냅샷당 문서 수 한도
DOCUMENT_MAX_COUNT_MIN = 1
DOCUMENT_MAX_COUNT_MAX = 10000
DOCUMENT_MAX_MB_DEFAULT = 50                 # 문서 1개 크기 한도 (MB)
DOCUMENT_MAX_MB_MIN = 1
DOCUMENT_MAX_MB_MAX = 1024
DOCUMENT_FETCH_TIMEOUT_DEFAULT = 120         # 문서 다운로드 타임아웃 (초)
DOCUMENT_FETCH_TIMEOUT_MIN = 5
DOCUMENT_FETCH_TIMEOUT_MAX = 3600

# ---- 텍스트 검색 인덱스 (searchindex.py — SQLite FTS5 trigram) ----
# 색인 원문은 스냅샷의 content.md(정규화 텍스트) + 첨부 문서 본문이다.
# 문서 본문 추출(doctext.py)은 이 크기 이하 파일만 시도한다 — 거대한 PDF
# 파싱이 아카이빙/백필을 과도하게 지연시키는 것을 막는다 (초과분은 파일
# 메타데이터만 색인). 한 문서에서 가져오는 본문 길이도 상한을 둔다.
SEARCH_DOC_TEXT_MAX_BYTES = 30 * 1024 * 1024   # 본문 추출 시도 파일 크기 상한 (30MB)
SEARCH_DOC_TEXT_MAX_CHARS = 2 * 1024 * 1024    # 문서 1개에서 색인할 본문 글자 수 상한

# URL 정규화 시 제거할 트래킹 파라미터 prefix
TRACKING_PARAM_PREFIXES = ("utm_", "fbclid", "gclid", "igshid", "ref_src")

# ---- 사이트 전체 아카이브 (crawler.py) ----
# CRAWL_DEFAULT_*(기본값)·CRAWL_MAX_*_LIMIT(설정 없을 때의 기본 상한)·
# CRAWL_RETRY_BACKOFF_SECONDS 는 시스템 설정(settings 테이블, 대시보드 시스템 화면)으로
# 오버라이드된다 — crawler.crawl_defaults / crawl_limits / retry_backoff 참조.
# CRAWL_MAX_*_CEILING 은 관리자가 설정할 수 있는 상한의 절대 천장(설정 불가).
CRAWL_DEFAULT_MAX_PAGES = 500
CRAWL_MAX_PAGES_LIMIT = 10000        # 상한(crawl_limits) 미설정 시 기본값
CRAWL_MAX_PAGES_CEILING = 1_000_000  # 상한 설정의 절대 천장
CRAWL_DEFAULT_MAX_DEPTH = 5
CRAWL_MAX_DEPTH_LIMIT = 20           # 상한 미설정 시 기본값
CRAWL_MAX_DEPTH_CEILING = 100        # 상한 설정의 절대 천장
CRAWL_DEFAULT_DELAY_SECONDS = 5      # 페이지 간 최소 간격 (대상 서버 부담 방지)
CRAWL_MIN_DELAY_SECONDS = 1
CRAWL_MAX_DELAY_SECONDS = 3600       # 지연 상한 미설정 시 기본값
CRAWL_MAX_DELAY_CEILING = 86400      # 지연 상한 설정의 절대 천장 (1일)
CRAWL_RETRY_BACKOFF_SECONDS = (300, 900)   # n차 실패 후 재시도 대기 — 최대 시도 = 길이 + 1
CRAWL_RETRY_BACKOFF_MIN_SECONDS = 10       # 재시도 대기 항목별 허용 범위 (설정 검증용)
CRAWL_RETRY_BACKOFF_MAX_SECONDS = 86400
CRAWL_RETRY_BACKOFF_MAX_STEPS = 5          # 대기 항목 수 한도 — 최대 시도 6회
CRAWL_STALE_CLAIM_SECONDS = 600      # 이보다 오래된 in_progress 는 중단으로 보고 복구
CRAWLER_POLL_SECONDS = 2             # serve·워커 크롤러 폴링 간격
ARCHIVE_POLL_SECONDS = 2             # serve·워커 단발 아카이빙 큐 폴링 간격 (archive_worker.py)
# 큐가 빈 뒤 이 시간만큼 더 유휴여야 브라우저를 내린다 — 폴링(2초)마다 close/재기동
# 스래싱을 막으면서 산발적 작업 사이에 chromium 을 재사용한다 (메모리 점유 ↔ 기동 비용 절충).
BROWSER_IDLE_CLOSE_SECONDS = 60

# ---- 아카이빙 워커 (`wccg worker`, worker.py) ----
# 크롤 스레드 수 = 동시에 진행되는 크롤(사이트) 수. 같은 크롤은 스레드가
# 몇 개든 한 번에 한 페이지만 처리된다 (db.claim_due_crawl_page).
CRAWL_WORKERS = int(os.environ.get("WCCG_CRAWL_WORKERS", "2"))
CRAWL_WORKERS_LIMIT = 8

# ---- 시스템 로그 (system_log.py — DB 적재, 대시보드 /system/logs 에서 열람) ----
# 보관 한도 행 수 — 핸들러가 적재 중 주기적으로 한도를 넘는 오래된 행을 정리한다.
SYSTEM_LOG_MAX_ROWS = int(os.environ.get("WCCG_SYSTEM_LOG_MAX_ROWS", "20000"))

# ---- 로그 파일 (선택 — 콘솔 로그를 회전 파일로도 남긴다, cli.py 가 설치) ----
# WCCG_LOG_FILE 가 설정되면 그 경로에 INFO 이상 로그를 회전 파일로 기록한다
# (도커는 볼륨에 마운트해 호스트에서 읽기 — docker-compose.yml 참조). 미설정
# 이면 콘솔(stderr)·DB(system_logs) 만. 다중 프로세스(dashboard/worker)는 서로
# 다른 파일을 써야 한다 — 회전이 같은 파일에서 경합하면 깨질 수 있다.
LOG_FILE = os.environ.get("WCCG_LOG_FILE", "").strip()
LOG_FILE_MAX_BYTES = int(os.environ.get("WCCG_LOG_FILE_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_FILE_BACKUPS = int(os.environ.get("WCCG_LOG_FILE_BACKUPS", "5"))

# ---- 확장 1회성 세션 자격증명 만료 (site_credentials.expires_at 안전망 TTL) ----
# 확장이 보낸 쿠키로 만든 세션 자격증명은 캡처 직후 삭제되지만, 오류·재기동으로
# 삭제가 누락된 행을 정리하는 안전망 만료 시간(시간). 시스템 설정이 우선한다.
EXT_CREDENTIAL_TTL_HOURS_DEFAULT = 24
EXT_CREDENTIAL_TTL_HOURS_MIN = 1
EXT_CREDENTIAL_TTL_HOURS_MAX = 168

# 기본 127.0.0.1 (localhost 전용). 컨테이너 등에서만 WCCG_HOST=0.0.0.0 으로 오버라이드.
DASHBOARD_HOST = os.environ.get("WCCG_HOST", "127.0.0.1")
DASHBOARD_PORT = 8765

# ---- 디버그 진단 포트 (web/debug_server.py) ----
# 별도 HTTP 포트로 내부 상태(큐·DB·로그·설정)를 노출하고 안전한 트리거(1회성
# 캡처·스케줄 1회 실행)를 제공한다 — 개발/테스트 중 문제를 빠르게 진단하기 위함.
# 기본 off — 켜져야만 serve/worker 가 이 포트를 연다. 릴리스 compose 는 이 토글을
# 주지 않으므로(=off) 포트가 열리지 않는다('릴리스에선 동작 안 함').
# 시크릿은 절대 노출하지 않고(원칙 6), 쓰기 트리거는 코어 모듈을 경유한다(원칙 1).
DEBUG_ENABLED = os.environ.get("WCCG_DEBUG", "off") == "on"
# 기본 127.0.0.1. 컨테이너에서 호스트(LAN)로 노출하려면 0.0.0.0 으로 바인딩하되
# 같은 네트워크의 누구나 접근할 수 있으므로 develop/테스트 전용으로만 쓴다.
DEBUG_HOST = os.environ.get("WCCG_DEBUG_HOST", "127.0.0.1")
DEBUG_PORT = int(os.environ.get("WCCG_DEBUG_PORT", "8799"))
# 선택적 하드닝 — 설정하면 디버그 요청에 X-Debug-Token 헤더를 요구한다(LAN 노출 시
# 권장). 빈값이면 토큰 검사 없이 (네트워크 배치 + WCCG_DEBUG 토글)로만 보호한다.
DEBUG_TOKEN = os.environ.get("WCCG_DEBUG_TOKEN", "").strip()

# ---- 스케줄러 (주기적 재아카이빙) ----
# 대시보드(serve) 프로세스 안에서 폴링 스레드로 동작한다.
# off 면 serve 는 스케줄을 실행하지 않는다 — cron 의 `wccg schedule run` 으로 대체 가능.
SCHEDULER_ENABLED = os.environ.get("WCCG_SCHEDULER", "on") != "off"
SCHEDULER_POLL_SECONDS = 60

# ---- 인증 ----
# WCCG_AUTH=off 는 loopback 바인딩일 때만 허용 (cli.serve 에서 강제)
AUTH_ENABLED = os.environ.get("WCCG_AUTH", "on") != "off"

# 최초 구동(사용자 0명) 시 자동 등록할 관리자. 미설정이면 /setup 페이지로 유도.
ADMIN_EMAIL = os.environ.get("WCCG_ADMIN_EMAIL", "").strip()
ADMIN_PASSWORD = os.environ.get("WCCG_ADMIN_PASSWORD", "")
SESSION_TTL_DAYS = int(os.environ.get("WCCG_SESSION_TTL_DAYS", "14"))
SESSION_COOKIE = "wccg_session"
# API 키 last_used_at 갱신 스로틀 — 읽기 API 폴링(확장 등)이 매 요청 쓰기
# 트랜잭션을 일으키지 않게, 이 간격 이내면 갱신을 생략한다 (표시용 근사값).
API_KEY_TOUCH_THROTTLE_SECONDS = 60
TOTP_ISSUER = "ChunChuGwan"
PENDING_TOTP_TTL_SECONDS = 600          # 패스워드 통과 후 OTP 입력 제한 시간
MIN_PASSWORD_LENGTH = 8

# 이메일 본인 인증 — 코드 만료 시간(분, 시스템 설정으로 변경)과 코드 자릿수.
# 인증 대기(pending_email_verify) 세션 수명은 코드 만료 시간을 따른다.
EMAIL_VERIFICATION_TTL_MINUTES_DEFAULT = 30
EMAIL_VERIFICATION_TTL_MINUTES_MIN = 5
EMAIL_VERIFICATION_TTL_MINUTES_MAX = 1440  # 24시간
EMAIL_VERIFICATION_CODE_LENGTH = 6         # 숫자 코드 자릿수

# 아카이브 휴지통 — 보관 기간(일, 시스템 설정으로 변경). 페이지·사이트 삭제를
# 즉시 지우지 않고 휴지통에 숨겼다가 이 기간이 지나면 스케줄러가 영구 삭제한다.
# 0 = 자동 삭제 비활성(수동 영구삭제 전까지 보관). trash_enabled off 면 즉시 삭제.
TRASH_RETENTION_DAYS_DEFAULT = 30
TRASH_RETENTION_DAYS_MIN = 0               # 0 = 자동 purge 끔
TRASH_RETENTION_DAYS_MAX = 365

# 클러스터(federation) 조정 루프 — 피어별 주기 조정 사이클 간격(초, 시스템 설정으로 변경).
# 한 사이클에서 권한 갱신 → pull 델타 → push 델타를 처리한다. 너무 짧으면 피어에
# 부담이므로 하한을 둔다. 페이싱(건당 간격·배치 상한)은 별도 상수.
CLUSTER_SYNC_INTERVAL_SECONDS_DEFAULT = 300   # 5분
CLUSTER_SYNC_INTERVAL_SECONDS_MIN = 60        # 1분
CLUSTER_SYNC_INTERVAL_SECONDS_MAX = 86400     # 1일
CLUSTER_SYNC_BATCH_MAX = 20                   # 사이클·방향당 처리 스냅샷 상한(델타 배치)
CLUSTER_SEND_MIN_INTERVAL_SECONDS = 2         # 전송 건당 최소 간격(대상 부담 방지)
CLUSTER_HTTP_TIMEOUT_SECONDS = 30             # 피어 HTTP 호출 타임아웃
CLUSTER_PROTOCOL_VERSION = 1                  # 핸드셰이크 프로토콜 버전(호환성 거부 기준)
CLUSTER_BUSY_JOBS_THRESHOLD = 5               # 대기·진행 아카이빙 작업이 이 이상이면 수신 백프레셔(429)
CLUSTER_BUSY_RETRY_AFTER_SECONDS = 60         # 백프레셔 시 Retry-After 안내(초)
CLUSTER_BLOB_MAX_BYTES = 200 * 1024 * 1024    # 단일 CAS 블롭 업로드 상한(수신측 방어)

# 인증 무차별 대입 방어(rate limit) 기본값 — 시스템 설정(settings)으로 오버라이드한다
# (db.auth_throttle_settings 가 [MIN, MAX] 로 클램핑). 고정 윈도우 카운터 방식.
AUTH_LOGIN_LIMIT_DEFAULT = 10              # 이메일별 로그인 실패 허용 횟수/창
AUTH_LOGIN_IP_LIMIT_DEFAULT = 30          # IP별 로그인 시도 허용 횟수/창
AUTH_LOGIN_WINDOW_MINUTES_DEFAULT = 15    # 로그인 카운트 창(분)
AUTH_TOTP_LIMIT_DEFAULT = 10              # 2단계(TOTP·패스키) 시도 허용 횟수/창(=pending 수명)
AUTH_EMAIL_VERIFY_LIMIT_DEFAULT = 5      # 이메일 코드 오답 허용 횟수(초과 시 코드 폐기)
AUTH_EMAIL_RESEND_LIMIT_DEFAULT = 5      # 코드 재발송 시간당 허용 횟수
AUTH_THROTTLE_LIMIT_MIN = 1               # 한도 설정 허용 범위 (시도 횟수)
AUTH_THROTTLE_LIMIT_MAX = 1000
AUTH_THROTTLE_WINDOW_MIN = 1              # 창 설정 허용 범위 (분)
AUTH_THROTTLE_WINDOW_MAX = 1440
# throttle 행 GC 기준 — 가장 긴 창(로그인)의 안전 마진. delete_expired_throttle 호출에 쓴다.
AUTH_THROTTLE_GC_SECONDS = 86400

# 최초 설정(first-run) 보호 토큰. 설정하면 /setup 흐름(관리자 생성·복원·이전)이
# 일치 토큰을 요구한다 — 셋업 완료 전 외부 노출 인스턴스 선점·SSRF 방지. 빈값이면
# 종전대로 토큰 없이 셋업 가능(로컬 단독 사용 편의). 셋업이 끝나면 무의미해진다.
SETUP_TOKEN = os.environ.get("WCCG_SETUP_TOKEN", "").strip()

# 외부 사이트 로그인 자격증명 암호화 키 (대칭 — CLAUDE.md 원칙 6 예외).
# 설정 시에만 자격증명 기능이 활성화된다. DB·저장소엔 암호문만 남고 키는
# 여기(환경변수)에만 둔다. 바꾸면 기존 자격증명을 복호화할 수 없다.
SECRET_KEY = os.environ.get("WCCG_SECRET_KEY", "")

# 외부 노출 시 공개 URL (OIDC redirect_uri 조립, https 면 Secure 쿠키)
PUBLIC_URL = os.environ.get("WCCG_PUBLIC_URL", "").rstrip("/")
COOKIE_SECURE = PUBLIC_URL.startswith("https://")

# ---- 패스키 (WebAuthn) ----
# RP ID 는 도메인이어야 한다. PUBLIC_URL 미설정 시 localhost 로 동작 —
# 이 경우 http://localhost:8765 접속에서만 패스키를 쓸 수 있다 (127.0.0.1 불가).
WEBAUTHN_RP_ID = (urlsplit(PUBLIC_URL).hostname or "localhost") if PUBLIC_URL else "localhost"
WEBAUTHN_RP_NAME = "ChunChuGwan"
WEBAUTHN_ORIGINS = [PUBLIC_URL] if PUBLIC_URL else [f"http://localhost:{DASHBOARD_PORT}"]

# ---- 메일 (초대 발송) ----
# SMTP 설정은 시스템 메뉴(DB settings)에서 등록·변경하거나 아래 환경변수로
# 둔다. 둘 다 있으면 DB 값이 우선하고, 없는 항목만 환경변수로 폴백한다
# (mailer.resolve_config). 발송 가능 여부는 mailer.mail_enabled(conn) — 여기
# 값은 호스트가 어디에도 없을 때의 기본값(빈 문자열)이다.
SMTP_HOST = os.environ.get("WCCG_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("WCCG_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("WCCG_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("WCCG_SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("WCCG_SMTP_FROM", "") or SMTP_USER
SMTP_TLS = os.environ.get("WCCG_SMTP_TLS", "starttls")  # starttls | ssl | off
SMTP_TIMEOUT_SECONDS = 10
INVITE_TTL_DAYS = int(os.environ.get("WCCG_INVITE_TTL_DAYS", "7"))


# ---- OIDC (Authentik) ----
OIDC_PROVIDER = "authentik"
OIDC_ISSUER = os.environ.get("WCCG_OIDC_ISSUER", "").rstrip("/")
OIDC_CLIENT_ID = os.environ.get("WCCG_OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("WCCG_OIDC_CLIENT_SECRET", "")
OIDC_STATE_TTL_SECONDS = 600


def oidc_enabled() -> bool:
    """OIDC 설정이 모두 채워졌는지 (테스트에서 monkeypatch 가능하도록 함수)."""
    return bool(OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET)


def s3_requested() -> bool:
    """WCCG_S3_* 식별 정보 중 하나라도 설정돼 S3 백엔드를 요청했는지."""
    return bool(S3_ENDPOINT_URL or S3_BUCKET or S3_ACCESS_KEY_ID or S3_SECRET_ACCESS_KEY)


def s3_settings() -> dict:
    """S3 백엔드 생성 인자 (필수 세트 검증). 불완전하면 RuntimeError.

    필수: bucket·access key·secret. 누락 시 무엇이 빠졌는지 변수명만 알리고
    값(비밀)은 노출하지 않는다.
    """
    required = {
        "WCCG_S3_BUCKET": S3_BUCKET,
        "WCCG_S3_ACCESS_KEY_ID": S3_ACCESS_KEY_ID,
        "WCCG_S3_SECRET_ACCESS_KEY": S3_SECRET_ACCESS_KEY,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "S3 백엔드 설정이 불완전합니다 — 누락된 환경변수: " + ", ".join(missing)
        )
    return {
        "bucket": S3_BUCKET,
        "archive_root": ARCHIVE_ROOT,
        "cache_dir": BLOB_CACHE_DIR,
        "cache_max_bytes": BLOB_CACHE_MAX_MB * 1024 * 1024,
        "endpoint_url": S3_ENDPOINT_URL,
        "region": S3_REGION,
        "access_key_id": S3_ACCESS_KEY_ID,
        "secret_access_key": S3_SECRET_ACCESS_KEY,
        "force_path_style": S3_FORCE_PATH_STYLE,
        "prefix": S3_PREFIX,
    }


_blob_store = None


def active_backend() -> str:
    """현재 활성 blob 백엔드 ('local'|'s3').

    DB 설정 `storage_backend` 가 정본이고 기본은 'local'. env(WCCG_S3_*)는
    S3 가용성/자격증명일 뿐 활성 백엔드를 바꾸지 않는다 — 전환은 마이그레이션
    0실패 완료(또는 P5 setup)로만 일어난다. DB 파일이 아직 없으면(빈/순수
    단위 테스트) 설정을 읽으려 DB 를 만들지 않고 'local' 로 본다.
    """
    if not DB_PATH.is_file():
        return "local"
    from . import db

    with db.connect() as conn:
        return db.storage_backend(conn)


def blob_store():
    """활성 blob 저장 백엔드 인스턴스 (싱글턴).

    활성 백엔드가 's3'면 WCCG_S3_* 가 완전해야 하고(s3_settings() 가 불완전
    시 RuntimeError 로 실패), 'local'/미설정이면 LocalBlobStore 를 쓴다.
    인스턴스는 캐시되며, 마이그레이션 완료로 활성 백엔드가 바뀌면
    reset_blob_store() 로 무효화한다.
    """
    global _blob_store
    if _blob_store is None:
        if active_backend() == "s3":
            from .blobstore import S3BlobStore

            _blob_store = S3BlobStore(**s3_settings())
        else:
            from .blobstore import LocalBlobStore

            _blob_store = LocalBlobStore()
    return _blob_store


def reset_blob_store() -> None:
    """캐시된 백엔드 인스턴스를 비운다 — 활성 백엔드 전환 후 재생성용."""
    global _blob_store
    _blob_store = None


def ensure_dirs() -> None:
    """아카이브 루트 디렉토리 생성."""
    try:
        SITES_DIR.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        raise PermissionError(
            f"아카이브 디렉토리를 만들 수 없습니다: {ARCHIVE_ROOT} — 쓰기 권한을 "
            "확인하세요 (도커 바인드 마운트라면 호스트 디렉토리 소유자가 "
            "컨테이너 사용자 uid 1000 과 다른 경우)"
        ) from e


def load_domain_rules(domain: str) -> dict:
    """rules.json 에서 도메인별 정규화 룰 로드. 없거나 깨졌으면 빈 dict.

    형식:
        {"example.com": {"remove_selectors": [".ads"],
                         "remove_line_patterns": ["^관련 기사"]}}
    `www.` 접두사가 빠진 키로도 조회한다. 룰 파일 오류가 아카이빙을
    막아서는 안 되므로 경고만 남기고 무시한다.
    """
    if not RULES_PATH.is_file():
        return {}
    try:
        rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("rules.json 로드 실패, 무시: %s", e)
        return {}
    entry = rules.get(domain) or rules.get(domain.removeprefix("www.")) or {}
    return entry if isinstance(entry, dict) else {}
