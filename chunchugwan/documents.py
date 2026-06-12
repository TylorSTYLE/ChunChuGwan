"""아카이빙된 페이지가 링크한 문서 파일(PDF·워드·한글 등) 다운로드와 문서 CAS.

캡처 시 수집된 문서 링크를 내려받아 문서 콘텐츠 주소 저장소
(``documents/{sha256 앞 2자}/{sha256}{확장자}``)에 저장하고, 스냅샷은
snapshot_documents 행(db.py)과 meta.json 의 documents 목록으로 참조한다.
같은 내용의 문서는 스냅샷·페이지가 달라도 한 번만 저장되며, 참조하는
스냅샷이 모두 삭제되면 CAS 파일도 함께 삭제된다 (deletion.py).

보안 노트: 공유 자원 CAS(/resource/)는 인증 예외 경로라 문서 타입 서빙이
금지돼 있으므로(resources.py 보안 노트), 문서 CAS 는 별도 디렉토리에 두고
인증이 걸린 라우트에서 DB/meta 에 기록된 이름만 항상 첨부파일 다운로드로
내려준다 — 절대 /resource/ 로 합치지 말 것.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import httpx

from . import config, db, storage

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
    links: list[str], dest_dir: Path, referer: str | None = None,
    verify: bool = True,
) -> tuple[list[dict[str, object]], list[str]]:
    """문서 링크들을 dest_dir 에 내려받아 (성공 manifest, 실패 URL) 반환.

    manifest 항목: {url, file, bytes, sha256, content_type}.
    개수(config.DOCUMENT_MAX_COUNT)·크기(config.DOCUMENT_MAX_BYTES) 한도를
    넘거나 응답이 HTML 인 항목은 건너뛴다. 실패가 아카이빙을 막지 않는다.
    verify=False 는 TLS 인증서 검증을 끈다 — 자체 서명 사이트를 검증 무시로
    캡처한 경우(pipeline insecure_tls)에만 쓴다.
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
            manifest.append(_download_one(url, dest_dir, headers, verify))
        except Exception as e:
            logger.warning("문서 다운로드 실패(건너뜀): %s — %s", url, e)
            failed.append(url)
    return manifest, failed


def _download_one(
    url: str, dest_dir: Path, headers: dict[str, str], verify: bool = True
) -> dict[str, object]:
    """문서 1개를 스트리밍 다운로드 (크기 한도 검사 + sha256 계산)."""
    name = document_filename(url)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / name
    hasher = hashlib.sha256()
    size = 0
    try:
        with httpx.stream(
            "GET", url, headers=headers, follow_redirects=True,
            timeout=config.DOCUMENT_FETCH_TIMEOUT_SECONDS, verify=verify,
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


# ---- 문서 CAS (콘텐츠 주소 저장소) ----

# CAS 파일 이름: sha256 hex + 문서 화이트리스트 확장자
_CAS_NAME_RE = re.compile(r"([0-9a-f]{64})(\.[a-z0-9]{2,8})")


def is_valid_cas_name(name: str) -> bool:
    """문서 CAS 이름 형식 검증 — sha256 hex + 문서 화이트리스트 확장자."""
    m = _CAS_NAME_RE.fullmatch(name)
    return bool(m and m.group(2) in config.DOCUMENT_EXTENSIONS)


def cas_name(sha256: str, file: str) -> str | None:
    """(해시, 정제된 파일명) → CAS 이름. 형식이 유효하지 않으면 None."""
    name = sha256 + Path(file).suffix.lower()
    return name if is_valid_cas_name(name) else None


def cas_path(name: str) -> Path:
    """CAS 이름 → 저장 경로. 호출 전 is_valid_cas_name 검증 필수."""
    return config.DOCUMENTS_DIR / name[:2] / name


def _move_into_cas(src: Path, name: str) -> bool:
    """파일을 CAS 로 이동. 같은 내용이 이미 있으면 src 만 지운다.

    새로 추가됐으면 True (중복 제거로 저장이 생략됐으면 False).
    os.replace 는 원자적이라 동시 아카이빙(스케줄러 + 수동)에 안전하다.
    스테이징(/tmp)과 아카이브가 다른 파일시스템이면(도커 볼륨 등 — EXDEV)
    목적지 디렉토리의 임시 파일로 복사한 뒤 교체해 원자성을 유지한다.
    """
    dst = cas_path(name)
    if dst.is_file():
        src.unlink()
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        fd, tmp = tempfile.mkstemp(dir=dst.parent, suffix=".tmp")
        os.close(fd)
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
        except BaseException:
            os.unlink(tmp)
            raise
        src.unlink()
    return True


def ingest_into_cas(files_dir: Path, manifest: list[dict[str, object]]) -> None:
    """내려받은 문서들을 files_dir 에서 문서 CAS 로 이동.

    manifest 의 sha256 은 다운로드 스트리밍 중 직접 계산한 값이라 신뢰한다.
    CAS 이름을 만들 수 없는 항목(비정상 확장자)은 manifest 에서 제거해
    스냅샷이 존재하지 않는 문서를 참조하지 않게 한다.
    """
    kept: list[dict[str, object]] = []
    for entry in manifest:
        name = cas_name(str(entry["sha256"]), str(entry["file"]))
        src = files_dir / str(entry["file"])
        if name is None or not src.is_file():
            logger.warning("문서 CAS 이전 불가(건너뜀): %s", entry.get("file"))
            continue
        _move_into_cas(src, name)
        kept.append(entry)
    manifest[:] = kept
    try:
        files_dir.rmdir()
    except OSError:
        pass


def delete_cas(names: list[str]) -> None:
    """참조가 사라진 문서 CAS 파일들 삭제 (빈 버킷 디렉토리도 정리)."""
    for name in names:
        if not is_valid_cas_name(name):
            continue
        path = cas_path(name)
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass


# ---- 구형 스냅샷(files/) → 문서 CAS 이전 (wccg compact) ----


def _legacy_entries(snap_dir: Path) -> list[tuple[dict, Path]]:
    """스냅샷 files/ 에 실제로 남아 있는 meta documents 항목들.

    파일명은 meta 에 기록된 값이지만 경로 조립 전에 한 번 더 검증한다.
    """
    files_dir = snap_dir / "files"
    if not files_dir.is_dir():
        return []
    try:
        meta = storage.read_meta(snap_dir)
    except (OSError, ValueError, TypeError):
        return []
    out: list[tuple[dict, Path]] = []
    for entry in meta.documents or []:
        fname = str(entry.get("file") or "")
        if not fname or Path(fname).name != fname:
            continue
        src = files_dir / fname
        if src.is_file():
            out.append((entry, src))
    return out


def has_legacy_documents(snap_dir: Path) -> bool:
    """CAS 로 이전할 문서가 files/ 에 남아 있는지 (compact 대상 판정)."""
    return bool(_legacy_entries(snap_dir))


def _file_sha256(path: Path) -> str:
    """파일 내용의 sha256 (스트리밍 — 문서는 최대 50MB)."""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            hasher.update(chunk)
    return hasher.hexdigest()


@dataclass
class DocCompactStats:
    """compact_legacy_documents 결과 — 이전 수와 전/후 바이트."""

    moved: int = 0
    before_bytes: int = 0
    after_bytes: int = 0   # CAS 에 새로 추가된 용량 (중복은 0)


def _compact_snapshot_documents(
    conn: sqlite3.Connection, snapshot_id: int, snap_dir: Path, stats: DocCompactStats
) -> None:
    """스냅샷 하나의 files/ 문서를 CAS 로 이전하고 참조 행을 기록 (멱등).

    해시는 meta 값을 믿지 않고 다시 계산한다 — CAS 이름은 반드시 실제
    내용과 일치해야 한다 (콘텐츠 주소 불변 원칙).
    """
    manifest: list[dict[str, object]] = []
    for entry, src in _legacy_entries(snap_dir):
        sha = _file_sha256(src)
        name = cas_name(sha, src.name)
        if name is None:
            continue
        size = src.stat().st_size
        stats.before_bytes += size
        if _move_into_cas(src, name):
            stats.after_bytes += size
        stats.moved += 1
        manifest.append({
            "url": str(entry.get("url") or ""),
            "file": src.name,
            "bytes": size,
            "sha256": sha,
            "content_type": str(entry.get("content_type") or "application/octet-stream"),
        })
    if manifest:
        db.insert_snapshot_documents(conn, snapshot_id, manifest)
    try:
        (snap_dir / "files").rmdir()
    except OSError:
        pass


def compact_legacy_documents() -> DocCompactStats:
    """모든 확정 스냅샷의 구형 files/ 문서를 CAS 로 이전 (멱등).

    resources.compact_all 이 호출한다 — 저장 형태만 바꾸는 내용 보존 변환.
    """
    stats = DocCompactStats()
    with db.connect() as conn:
        for snap in db.list_snapshot_dirs(conn):
            snap_dir = (
                storage.page_dir(snap["domain"], snap["slug"]) / snap["dir_name"]
            )
            if (snap_dir / "files").is_dir():
                _compact_snapshot_documents(conn, snap["id"], snap_dir, stats)
    return stats
