"""스냅샷 파일시스템 레이아웃과 URL 정규화.

보안 노트: slug 생성 시 URL 경로를 그대로 쓰지 말 것 (path traversal).
영숫자/하이픈만 남기고 잘라낸 뒤 URL 해시 8자리를 붙여 유일성을 보장한다.
"""

from __future__ import annotations

import hashlib
import json
import re
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


def normalize_url(raw: str) -> str:
    """비교/저장 기준이 되는 정규화 URL.

    - 스킴/호스트 소문자화, fragment 제거
    - 쿼리 파라미터 정렬, 트래킹 파라미터 제거 (config.TRACKING_PARAM_PREFIXES)
    - 기본 포트(:80, :443) 제거
    """
    parts = urlsplit(raw.strip())
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
