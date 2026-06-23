"""스냅샷 파일시스템 레이아웃과 URL 정규화.

보안 노트: slug 생성 시 URL 경로를 그대로 쓰지 말 것 (path traversal).
영숫자/하이픈만 남기고 잘라낸 뒤 URL 해시 8자리를 붙여 유일성을 보장한다.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote, unquote

from . import config


@dataclass
class SnapshotMeta:
    url: str            # 정규화 URL
    final_url: str      # 리다이렉트 후
    taken_at: str       # ISO 8601 UTC
    content_hash: str   # 정규화 텍스트 SHA-256
    http_status: int | None
    title: str | None
    # 함께 저장된 문서 파일 목록 [{url, file, bytes, sha256, content_type}].
    # files/ 하위 파일은 이 목록에 있는 이름만 서빙된다 (구형 meta 는 None).
    documents: list[dict] | None = None
    # 캡처 출처 — 'server'(서버 캡처, 기본) | 'extension'(브라우저 확장 클라이언트).
    origin: str = "server"
    # 불완전 캡처 여부 (일부 자원·프레임·스크린샷 수집 실패).
    incomplete: bool = False
    # 확장 캡처의 브라우저 환경 {viewport_w, viewport_h, dpr, zoom, ua} — 뷰어가
    # 해상도 차이를 라벨로 보여준다. 서버 캡처는 None.
    capture_env: dict | None = None


_DEFAULT_PORTS = {"http": 80, "https": 443}

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


# RFC 3986 pchar 에서 unreserved 외에 literal 로 허용되는 문자 ('/' 제외)
_PCHAR_SAFE = "!$&'()*+,;=:@"


def _requote_segment(seg: str, safe: str) -> str:
    """한 segment 의 퍼센트 인코딩 표기 통일 (decode 후 재인코딩).

    UTF-8 로 디코딩되지 않는 시퀀스(EUC-KR 인코딩 URL 등)는 원형 유지.
    """
    try:
        decoded = unquote(seg, errors="strict")
    except UnicodeDecodeError:
        return seg
    return quote(decoded, safe=safe)


def _requote(component: str, safe: str) -> str:
    """path/fragment 의 퍼센트 인코딩 표기 통일.

    '경기 파주시'(원형)와 '%EA%B2%BD%EA%B8%B0%20...'(인코딩)가 같은 URL 로
    취급되도록 정규화한다. '/' 는 segment 단위로 처리해 인코딩된 %2F 와의
    구분을 유지한다. safe 에는 '/' 를 넣지 말 것.
    """
    return "/".join(_requote_segment(seg, safe) for seg in component.split("/"))


def _is_route_fragment(fragment: str) -> bool:
    """SPA 라우팅 fragment 인지 — 경로 구분자(/)가 있으면 라우트로 본다.

    weather.go.kr 의 `#dong/4148051000/...` 처럼 fragment 가 화면을 결정하는
    사이트가 있다. 이런 fragment 를 제거하면 기본 화면이 아카이브되므로
    보존해야 한다. 단순 문서 내 앵커(`#section-2`)는 콘텐츠에 영향이 없어
    제거 대상이다.
    """
    return "/" in fragment


def normalize_url(raw: str) -> str:
    """비교/저장 기준이 되는 정규화 URL.

    - 스킴 생략 시 https:// 자동 보완 (example.com → https://example.com/)
    - 스킴/호스트 소문자화, fragment 제거 (단, SPA 라우팅 fragment 는 보존)
    - 쿼리 파라미터 정렬, 트래킹 파라미터 제거 (config.TRACKING_PARAM_PREFIXES)
    - 기본 포트(:80, :443) 제거
    - path/fragment 퍼센트 인코딩 표기 통일 (한글 원형 ↔ %XX 인코딩 동일 취급)
    """
    raw = raw.strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw and not _SCHEME_RE.match(raw):
        raw = "https://" + raw
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"지원하지 않는 스킴: {raw!r}")
    if not parts.hostname:
        raise ValueError(f"호스트가 없는 URL: {raw!r}")

    host = parts.hostname.lower()
    netloc = host
    if parts.port is not None and parts.port != _DEFAULT_PORTS[scheme]:
        netloc = f"{host}:{parts.port}"

    path = _requote(parts.path or "/", _PCHAR_SAFE)
    pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(config.TRACKING_PARAM_PREFIXES)
    ]
    query = urlencode(sorted(pairs))
    fragment = parts.fragment if _is_route_fragment(parts.fragment) else ""
    fragment = _requote(fragment, _PCHAR_SAFE + "?")
    return urlunsplit((scheme, netloc, path, query, fragment))


def strip_www(host: str) -> str:
    """호스트의 `www.` 접두 제거 — www 와 apex 는 같은 사이트로 취급.

    남는 부분이 도메인 형태(점 포함)일 때만 제거한다 — `www.com` 처럼
    www 자체가 등록 도메인인 호스트나 IP 호스트는 그대로 둔다.
    """
    if host.startswith("www.") and "." in host[4:]:
        return host[4:]
    return host


def netloc_site_key(netloc: str) -> str:
    """netloc(`host[:port]`)의 사이트 키 — www 접두만 제거, 포트는 유지."""
    host, sep, port = netloc.rpartition(":")
    if sep and port.isdigit():
        return f"{strip_www(host)}:{port}"
    return strip_www(netloc)


def site_key(normalized_url: str) -> str:
    """정규화 URL 의 사이트 키 — 서브도메인 단위 그룹핑 식별자.

    www 와 apex 는 같은 사이트, 다른 서브도메인은 다른 사이트다.
    기본 포트는 normalize_url 이 이미 제거했으므로 남은 포트는 다른
    사이트를 뜻한다 (예: localhost 게이트와 무관한 192.168.x.x:8080).
    """
    return netloc_site_key(urlsplit(normalized_url).netloc)


def url_origin(url: str) -> str:
    """URL 의 origin (`scheme://host[:비기본포트]`) — 소문자, 기본 포트 생략.

    로그인 자격증명을 대상 origin 으로만 스코프할 때 쓴다 (캡처 연동) —
    Basic 인증·Bearer 토큰이 페이지의 서드파티 하위 자원으로 새지 않게
    한다. 같은 origin 판정은 scheme·host·유효 포트가 모두 같을 때다.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    default_port = {"https": 443, "http": 80}.get(scheme)
    if port is not None and port != default_port:
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def scheme_inferred(raw: str) -> bool:
    """입력에 스킴이 없어 normalize_url 이 https:// 를 추정 보완하는지.

    HTTP 전용 사이트에서 https 추정이 빗나갈 수 있으므로, 캡처 실패 시
    http 폴백 여부를 판단하는 데 쓴다 (pipeline 참조).
    """
    return not _SCHEME_RE.match(raw.strip())


def url_to_slug(normalized_url: str) -> str:
    """디렉토리명용 slug: '{경로요약}-{sha256(url)[:8]}'.

    경로요약은 [a-z0-9-]만 허용, 최대 40자. 루트 경로면 'root'.
    라우팅 fragment 가 보존된 URL 은 fragment 도 요약에 포함한다.
    """
    url_hash = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:8]
    parts = urlsplit(normalized_url)
    path = parts.path.lower()
    if parts.fragment:
        path += "-" + parts.fragment.lower()
    summary = re.sub(r"[^a-z0-9]+", "-", path).strip("-")[:40].rstrip("-")
    if not summary:
        summary = "root"
    return f"{summary}-{url_hash}"


def page_dir(domain: str, slug: str) -> Path:
    return config.SITES_DIR / domain / slug


def new_snapshot_dir(domain: str, slug: str, taken_at: datetime | None = None) -> Path:
    """새 스냅샷 디렉토리 생성 후 경로 반환. 디렉토리명은 ISO 시각(콜론→하이픈)."""
    ts = (taken_at or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H-%M-%S")
    d = page_dir(domain, slug) / ts
    config.blob_store().mkdir(d, parents=True, exist_ok=False)
    return d


def write_meta(snapshot_dir: Path, meta: SnapshotMeta) -> None:
    config.blob_store().write_text(
        snapshot_dir / "meta.json",
        json.dumps(asdict(meta), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_meta(snapshot_dir: Path) -> SnapshotMeta:
    data = json.loads(
        config.blob_store().read_text(snapshot_dir / "meta.json", encoding="utf-8")
    )
    return SnapshotMeta(**data)


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# WebP 변환을 하지 않기로 확정한 스크린샷 PNG 의 마커 (빈 파일) — WebP 한 변
# 16383px 한도 초과로 변환에 실패했거나, 변환 결과가 원본보다 커서 생략한 경우.
# resources._screenshot_to_webp 가 남기고, needs_compaction(압축 대상 카운트)이
# 이 마커가 있는 PNG 를 변환 대상에서 제외한다.
WEBP_SKIP_MARKER = "screenshot.png.keep"
# 모바일 해상도 스크린샷(capture._capture_mobile_screenshot)의 WebP 변환 생략 마커.
# 데스크탑과 같은 규칙(한도 초과·용량 역효과)을 모바일 PNG 에 따로 적용한다.
MOBILE_WEBP_SKIP_MARKER = "screenshot-mobile.png.keep"

# 캡처 산출물 — 압축 변환(resources.compact_snapshot_dir) 전/후 이름 모두 포함.
# 변환이 일부 실패해도 (예: WebP 한도 초과로 PNG 유지) 산출물이 유실되지 않는다.
CAPTURE_ARTIFACTS = (
    "raw.html", "raw.html.gz", "page.html", "page.html.gz",
    "screenshot.png", "screenshot.webp", WEBP_SKIP_MARKER,
    "screenshot-mobile.png", "screenshot-mobile.webp", MOBILE_WEBP_SKIP_MARKER,
)

# 스냅샷 디렉토리를 구성하는 파일 전체 (표시 순서 고정)
SNAPSHOT_FILES = (
    "page.html", "page.html.gz", "raw.html", "raw.html.gz",
    "content.md", "screenshot.webp", "screenshot.png",
    "screenshot-mobile.webp", "screenshot-mobile.png", "meta.json",
)


def find_screenshot(snapshot_dir: Path) -> Path | None:
    """스냅샷의 데스크탑 스크린샷 경로 — WebP(신규) 우선, PNG(구형) 폴백. 없으면 None."""
    store = config.blob_store()
    for name in ("screenshot.webp", "screenshot.png"):
        path = snapshot_dir / name
        if store.is_file(path):
            return path
    return None


def find_mobile_screenshot(snapshot_dir: Path) -> Path | None:
    """스냅샷의 모바일 해상도 스크린샷 경로 — WebP 우선, PNG 폴백. 없으면 None.

    모바일 스크린샷은 시스템 설정이 켜져 있을 때만 생성되므로, 없을 수 있다.
    """
    store = config.blob_store()
    for name in ("screenshot-mobile.webp", "screenshot-mobile.png"):
        path = snapshot_dir / name
        if store.is_file(path):
            return path
    return None


def snapshot_files(snapshot_dir: Path) -> list[dict[str, object]]:
    """스냅샷 디렉토리의 파일 목록과 크기.

    SNAPSHOT_FILES 순서로 존재하는 파일만 [{name, bytes}] 로 반환하고,
    함께 저장된 문서가 있으면 'files/{이름}' 항목을 뒤에 붙인다.
    디렉토리가 없으면 빈 목록 (로그는 남아 있는데 파일이 지워진 경우 대비).
    """
    store = config.blob_store()
    out: list[dict[str, object]] = []
    for name in SNAPSHOT_FILES:
        path = snapshot_dir / name
        if store.is_file(path):
            out.append({"name": name, "bytes": store.size(path)})
    files_dir = snapshot_dir / "files"
    if store.is_dir(files_dir):
        for path in sorted(store.iterdir(files_dir)):
            if store.is_file(path):
                out.append({"name": f"files/{path.name}", "bytes": store.size(path)})
    return out


def snapshot_dir_bytes(snapshot_dir: Path) -> int:
    """스냅샷 디렉토리의 파일 용량 합 (바이트). 없으면 0.

    snapshot_files 와 같은 기준(스냅샷 산출물 + files/ 문서)으로 합산한다 —
    snapshots.bytes 비정규화 값의 단일 계산 지점이다.
    """
    return sum(f["bytes"] for f in snapshot_files(snapshot_dir))


def dir_bytes(root: Path) -> int:
    """디렉토리 전체 파일 용량 합 (바이트). 없으면 0."""
    store = config.blob_store()
    if not store.is_dir(root):
        return 0
    return sum(store.size(p) for p in store.rglob(root, "*") if store.is_file(p))


def _local_dir_bytes(root: Path) -> int:
    """로컬 파일시스템 디렉토리 용량 합 (백엔드 무관) — cache/blobcache 등 항상 로컬."""
    if not root.is_dir():
        return 0
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def local_usage() -> dict[str, int]:
    """S3 모드의 로컬 디스크 사용량 — index.db + cache/ + read-through 캐시(blobcache).

    S3 모드에선 sites/resources/documents 가 로컬에 없고 S3 에 있으므로(별도
    온디맨드 스캔), 로컬 사용량은 이 셋만 합산한다. S3 를 호출하지 않는다.
    """
    return {
        "db": config.DB_PATH.stat().st_size if config.DB_PATH.is_file() else 0,
        "cache": _local_dir_bytes(config.CACHE_DIR),
        "blobcache": _local_dir_bytes(config.BLOB_CACHE_DIR),
    }


# 아카이브 디스크 사용량 캐시 — sites/resources/documents 트리를 전부 rglob 하는
# 비싼 계산이라 짧은 TTL 로 캐시한다. 현황·시스템 화면의 표시 전용 파생값이라
# 약간의 부정확(최대 TTL)은 허용된다. 아카이브 루트별로 키를 둬 테스트·다중 루트
# 에서 다른 데이터가 섞이지 않게 한다.
_DISK_USAGE_TTL_SECONDS = 30
_disk_usage_cache: "tuple[float, str, dict[str, int]] | None" = None


def archive_disk_usage(*, fresh: bool = False) -> dict[str, int]:
    """아카이브 실제 저장공간 사용량 (바이트).

    로컬 모드: db/sites/resources/documents (DB·공유 자원 CAS·문서 CAS 포함).
    S3 모드: db/cache/blobcache — sites/resources/documents 는 S3 에 있어 로컬엔
    없으므로 요청 경로에서 S3 를 호출하지 않고 로컬 분해만 돌려준다(S3 카테고리
    사용량은 온디맨드 스캔 — storage_usage). fresh=True 면 캐시 무시.
    """
    global _disk_usage_cache
    root = str(config.ARCHIVE_ROOT)
    now = time.monotonic()
    cached = _disk_usage_cache
    if (
        not fresh and cached is not None and cached[1] == root
        and now - cached[0] < _DISK_USAGE_TTL_SECONDS
    ):
        return dict(cached[2])
    if config.active_backend() == "s3":
        usage = local_usage()  # S3 미호출 — 로컬 분해만
    else:
        usage = {
            "db": config.DB_PATH.stat().st_size if config.DB_PATH.is_file() else 0,
            "sites": dir_bytes(config.SITES_DIR),
            "resources": dir_bytes(config.RESOURCES_DIR),
            "documents": dir_bytes(config.DOCUMENTS_DIR),
        }
    _disk_usage_cache = (now, root, usage)
    return dict(usage)


def _validate_path_parts(*parts: str) -> None:
    """디렉토리 경로 조각 검증 — 구분자/상위 참조가 섞이면 ValueError.

    값은 DB 에 기록된 domain/slug/dir_name 만 쓰지만(app.py 보안 노트),
    SITES_DIR 밖을 지우는 사고가 없도록 삭제 직전에 한 번 더 막는다.
    """
    for part in parts:
        if not part or "/" in part or "\\" in part or part in (".", ".."):
            raise ValueError(f"잘못된 경로 조각: {part!r}")


def _prune_empty_dirs(domain: str) -> None:
    """빈 페이지/도메인 디렉토리 정리 (비어 있지 않으면 그대로 둔다)."""
    store = config.blob_store()
    domain_dir = config.SITES_DIR / domain
    if not store.is_dir(domain_dir):
        return
    for d in store.iterdir(domain_dir):
        store.rmdir(d)
    store.rmdir(domain_dir)


def delete_snapshot_dir(domain: str, slug: str, dir_name: str) -> None:
    """스냅샷 디렉토리 하나 삭제 (이미 없으면 무시). 빈 상위 디렉토리도 정리."""
    _validate_path_parts(domain, slug, dir_name)
    config.blob_store().rmtree(page_dir(domain, slug) / dir_name)
    _prune_empty_dirs(domain)


def delete_page_dir(domain: str, slug: str) -> None:
    """페이지 디렉토리 전체(모든 스냅샷) 삭제. 빈 도메인 디렉토리도 정리."""
    _validate_path_parts(domain, slug)
    config.blob_store().rmtree(page_dir(domain, slug))
    _prune_empty_dirs(domain)


def finalize_snapshot(
    tmp_dir: Path,
    domain: str,
    slug: str,
    meta: SnapshotMeta,
    normalized_text: str,
    taken_at: datetime,
) -> Path:
    """임시 캡처 산출물을 새 스냅샷 디렉토리로 확정.

    캡처 산출물 이동 + content.md / meta.json 기록. 확정된 디렉토리는
    이후 수정하지 않는다(불변).
    """
    store = config.blob_store()
    snap_dir = new_snapshot_dir(domain, slug, taken_at)
    for name in CAPTURE_ARTIFACTS:
        src = tmp_dir / name
        if src.exists():
            store.move(src, snap_dir / name)
    files_src = tmp_dir / "files"  # 함께 저장된 문서 파일 (documents.py)
    if files_src.is_dir():
        store.move(files_src, snap_dir / "files")
    store.write_text(snap_dir / "content.md", normalized_text, encoding="utf-8")
    write_meta(snap_dir, meta)
    return snap_dir
