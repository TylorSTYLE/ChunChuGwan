"""스냅샷 파일시스템 레이아웃과 URL 정규화.

보안 노트: slug 생성 시 URL 경로를 그대로 쓰지 말 것 (path traversal).
영숫자/하이픈만 남기고 잘라낸 뒤 URL 해시 8자리를 붙여 유일성을 보장한다.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
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
    d.mkdir(parents=True, exist_ok=False)
    return d


def write_meta(snapshot_dir: Path, meta: SnapshotMeta) -> None:
    (snapshot_dir / "meta.json").write_text(
        json.dumps(asdict(meta), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_meta(snapshot_dir: Path) -> SnapshotMeta:
    data = json.loads((snapshot_dir / "meta.json").read_text(encoding="utf-8"))
    return SnapshotMeta(**data)


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# 캡처 산출물 — 압축 변환(resources.compact_snapshot_dir) 전/후 이름 모두 포함.
# 변환이 일부 실패해도 (예: WebP 한도 초과로 PNG 유지) 산출물이 유실되지 않는다.
CAPTURE_ARTIFACTS = (
    "raw.html", "raw.html.gz", "page.html", "page.html.gz",
    "screenshot.png", "screenshot.webp",
)

# 스냅샷 디렉토리를 구성하는 파일 전체 (표시 순서 고정)
SNAPSHOT_FILES = (
    "page.html", "page.html.gz", "raw.html", "raw.html.gz",
    "content.md", "screenshot.webp", "screenshot.png", "meta.json",
)


def find_screenshot(snapshot_dir: Path) -> Path | None:
    """스냅샷의 스크린샷 경로 — WebP(신규) 우선, PNG(구형) 폴백. 없으면 None."""
    for name in ("screenshot.webp", "screenshot.png"):
        path = snapshot_dir / name
        if path.is_file():
            return path
    return None


def snapshot_files(snapshot_dir: Path) -> list[dict[str, object]]:
    """스냅샷 디렉토리의 파일 목록과 크기.

    SNAPSHOT_FILES 순서로 존재하는 파일만 [{name, bytes}] 로 반환하고,
    함께 저장된 문서가 있으면 'files/{이름}' 항목을 뒤에 붙인다.
    디렉토리가 없으면 빈 목록 (로그는 남아 있는데 파일이 지워진 경우 대비).
    """
    out: list[dict[str, object]] = []
    for name in SNAPSHOT_FILES:
        path = snapshot_dir / name
        if path.is_file():
            out.append({"name": name, "bytes": path.stat().st_size})
    files_dir = snapshot_dir / "files"
    if files_dir.is_dir():
        for path in sorted(files_dir.iterdir()):
            if path.is_file():
                out.append({"name": f"files/{path.name}", "bytes": path.stat().st_size})
    return out


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
    domain_dir = config.SITES_DIR / domain
    if not domain_dir.is_dir():
        return
    for d in domain_dir.iterdir():
        try:
            d.rmdir()
        except OSError:
            pass
    try:
        domain_dir.rmdir()
    except OSError:
        pass


def delete_snapshot_dir(domain: str, slug: str, dir_name: str) -> None:
    """스냅샷 디렉토리 하나 삭제 (이미 없으면 무시). 빈 상위 디렉토리도 정리."""
    _validate_path_parts(domain, slug, dir_name)
    shutil.rmtree(page_dir(domain, slug) / dir_name, ignore_errors=True)
    _prune_empty_dirs(domain)


def delete_page_dir(domain: str, slug: str) -> None:
    """페이지 디렉토리 전체(모든 스냅샷) 삭제. 빈 도메인 디렉토리도 정리."""
    _validate_path_parts(domain, slug)
    shutil.rmtree(page_dir(domain, slug), ignore_errors=True)
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
    snap_dir = new_snapshot_dir(domain, slug, taken_at)
    for name in CAPTURE_ARTIFACTS:
        src = tmp_dir / name
        if src.exists():
            shutil.move(str(src), snap_dir / name)
    files_src = tmp_dir / "files"  # 함께 저장된 문서 파일 (documents.py)
    if files_src.is_dir():
        shutil.move(str(files_src), snap_dir / "files")
    (snap_dir / "content.md").write_text(normalized_text, encoding="utf-8")
    write_meta(snap_dir, meta)
    return snap_dir
