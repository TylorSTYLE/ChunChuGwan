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
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from . import config


@dataclass
class SnapshotMeta:
    url: str            # 정규화 URL
    final_url: str      # 리다이렉트 후
    taken_at: str       # ISO 8601 UTC
    content_hash: str   # 정규화 텍스트 SHA-256
    http_status: int | None
    title: str | None


_DEFAULT_PORTS = {"http": 80, "https": 443}

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def normalize_url(raw: str) -> str:
    """비교/저장 기준이 되는 정규화 URL.

    - 스킴 생략 시 https:// 자동 보완 (example.com → https://example.com/)
    - 스킴/호스트 소문자화, fragment 제거
    - 쿼리 파라미터 정렬, 트래킹 파라미터 제거 (config.TRACKING_PARAM_PREFIXES)
    - 기본 포트(:80, :443) 제거
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

    path = parts.path or "/"
    pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(config.TRACKING_PARAM_PREFIXES)
    ]
    query = urlencode(sorted(pairs))
    return urlunsplit((scheme, netloc, path, query, ""))


def url_to_slug(normalized_url: str) -> str:
    """디렉토리명용 slug: '{경로요약}-{sha256(url)[:8]}'.

    경로요약은 [a-z0-9-]만 허용, 최대 40자. 루트 경로면 'root'.
    """
    url_hash = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:8]
    path = urlsplit(normalized_url).path.lower()
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


CAPTURE_ARTIFACTS = ("raw.html", "page.html", "screenshot.png")

# 스냅샷 디렉토리를 구성하는 파일 전체 (표시 순서 고정)
SNAPSHOT_FILES = ("page.html", "raw.html", "content.md", "screenshot.png", "meta.json")


def snapshot_files(snapshot_dir: Path) -> list[dict[str, object]]:
    """스냅샷 디렉토리의 파일 목록과 크기.

    SNAPSHOT_FILES 순서로 존재하는 파일만 [{name, bytes}] 로 반환한다.
    디렉토리가 없으면 빈 목록 (로그는 남아 있는데 파일이 지워진 경우 대비).
    """
    out: list[dict[str, object]] = []
    for name in SNAPSHOT_FILES:
        path = snapshot_dir / name
        if path.is_file():
            out.append({"name": name, "bytes": path.stat().st_size})
    return out


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
    (snap_dir / "content.md").write_text(normalized_text, encoding="utf-8")
    write_meta(snap_dir, meta)
    return snap_dir
