"""아카이빙된 페이지가 링크한 문서 파일(PDF·워드·한글 등) 다운로드.

캡처 시 수집된 문서 링크를 스냅샷의 files/ 하위 디렉토리에 저장한다.
공유 자원 CAS(/resource/)는 인증 예외 경로라 문서 타입 서빙이 금지돼
있으므로(resources.py 보안 노트), 문서는 스냅샷 디렉토리에 두고
meta.json 의 documents 목록으로 검증해 인증이 걸린 라우트로만 내려준다.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import httpx

from . import config

logger = logging.getLogger(__name__)

# 파일명에서 허용하는 문자 외 시퀀스 (유니코드 \w 라 한글 파일명은 보존된다)
_UNSAFE_RE = re.compile(r"[^\w.-]+", re.UNICODE)

# 응답이 이 타입이면 문서가 아니라 로그인/오류 페이지로 본다
_HTML_TYPES = ("text/html", "application/xhtml+xml")


def is_document_url(url: str) -> bool:
    """URL 경로의 확장자가 문서 화이트리스트에 있는 http(s) URL 인지."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    suffix = PurePosixPath(unquote(parts.path)).suffix.lower()
    return suffix in config.DOCUMENT_EXTENSIONS


def document_filename(url: str) -> str:
    """문서 저장 파일명: '{정제된 원본 이름}-{sha256(url)[:8]}{확장자}'.

    경로 구분자·상위 참조가 남지 않도록 정제하고(path traversal 방지),
    URL 해시로 이름 충돌 없이 유일성을 보장한다. 확장자는 화이트리스트
    검증을 통과한 값이라 안전한 문자만 포함한다.
    """
    path = PurePosixPath(unquote(urlsplit(url).path))
    ext = path.suffix.lower()
    stem = _UNSAFE_RE.sub("-", path.stem).strip("-.")[:60].rstrip("-.") or "document"
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{stem}-{url_hash}{ext}"


def download_documents(
    links: list[str], dest_dir: Path, referer: str | None = None
) -> tuple[list[dict[str, object]], list[str]]:
    """문서 링크들을 dest_dir 에 내려받아 (성공 manifest, 실패 URL) 반환.

    manifest 항목: {url, file, bytes, sha256, content_type}.
    개수(config.DOCUMENT_MAX_COUNT)·크기(config.DOCUMENT_MAX_BYTES) 한도를
    넘거나 응답이 HTML 인 항목은 건너뛴다. 실패가 아카이빙을 막지 않는다.
    """
    links = list(dict.fromkeys(links))
    if len(links) > config.DOCUMENT_MAX_COUNT:
        logger.warning(
            "문서 링크 %d개 중 앞 %d개만 저장 (개수 한도)",
            len(links), config.DOCUMENT_MAX_COUNT,
        )
        links = links[: config.DOCUMENT_MAX_COUNT]

    headers = {"User-Agent": config.USER_AGENT}
    if referer:
        headers["Referer"] = referer

    manifest: list[dict[str, object]] = []
    failed: list[str] = []
    for url in links:
        try:
            manifest.append(_download_one(url, dest_dir, headers))
        except Exception as e:
            logger.warning("문서 다운로드 실패(건너뜀): %s — %s", url, e)
            failed.append(url)
    return manifest, failed


def _download_one(url: str, dest_dir: Path, headers: dict[str, str]) -> dict[str, object]:
    """문서 1개를 스트리밍 다운로드 (크기 한도 검사 + sha256 계산)."""
    name = document_filename(url)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / name
    hasher = hashlib.sha256()
    size = 0
    try:
        with httpx.stream(
            "GET", url, headers=headers, follow_redirects=True,
            timeout=config.DOCUMENT_FETCH_TIMEOUT_SECONDS,
        ) as resp:
            resp.raise_for_status()
            content_type = (
                resp.headers.get("content-type", "").split(";")[0].strip().lower()
            )
            if content_type in _HTML_TYPES:
                raise ValueError(f"HTML 응답 — 문서 아님 ({content_type})")
            with path.open("wb") as f:
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > config.DOCUMENT_MAX_BYTES:
                        raise ValueError(
                            f"크기 한도 초과 (> {config.DOCUMENT_MAX_BYTES} bytes)"
                        )
                    hasher.update(chunk)
                    f.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)  # 부분 다운로드 잔재 제거
        raise
    return {
        "url": url,
        "file": name,
        "bytes": size,
        "sha256": hasher.hexdigest(),
        "content_type": content_type or "application/octet-stream",
    }
