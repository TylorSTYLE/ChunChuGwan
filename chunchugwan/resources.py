"""스냅샷 자원 콘텐츠 주소 저장소(CAS)와 저장 압축.

page.html 은 자원이 base64 data URI 로 인라인된 단일 HTML 이라 스냅샷 용량의
대부분을 차지하고, 같은 페이지를 재아카이빙하면 변하지 않은 이미지·폰트가
스냅샷마다 통째로 중복 저장된다. 이 모듈은 저장 공간을 줄이기 위해:

- 큰 data URI 자원을 sha256 콘텐츠 주소 저장소
  (``resources/{해시 앞 2자}/{sha256}{확장자}``)로 추출해 스냅샷 간 공유하고
  (base64 → 바이너리 변환으로 추가 ~25% 절감),
- 자원이 빠진 HTML 텍스트는 gzip 으로 저장하며 (page.html.gz / raw.html.gz),
- 스크린샷 PNG 는 WebP 로 변환한다.

자원 파일은 콘텐츠 주소라 불변이고, 추출된 page.html 은 대시보드의
``/resource/{이름}`` 라우트로 자원을 참조한다.

보안 노트:
- 추출은 _MIME_EXT 화이트리스트(이미지·폰트·CSS)에 한정한다. text/html 같은
  문서 타입을 추출하면 /resource/ 가 same-origin 문서를 서빙하게 되어
  아카이빙된 스크립트가 대시보드 컨텍스트에서 실행될 수 있으므로
  (CLAUDE.md 원칙 5) 절대 추가하지 말 것.
- 자원 이름은 ``sha256 hex + 화이트리스트 확장자`` 형식만 유효하다 —
  경로 조립 전에 반드시 is_valid_name 으로 검증한다 (path traversal).
"""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config, documents

logger = logging.getLogger(__name__)

# 추출 허용 미디어 타입 → 저장 확장자 (문서 타입 금지 — 모듈 docstring 참조)
_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "font/woff2": ".woff2",
    "font/woff": ".woff",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
    "application/font-woff": ".woff",
    "application/font-woff2": ".woff2",
    "application/x-font-ttf": ".ttf",
    "application/x-font-opentype": ".otf",
    "application/vnd.ms-fontobject": ".eot",
    "text/css": ".css",
}

# 저장 확장자 → 서빙 미디어 타입 (/resource/ 라우트용)
EXT_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".eot": "application/vnd.ms-fontobject",
    ".css": "text/css; charset=utf-8",
}

_DATA_URI_RE = re.compile(
    r"data:([a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)"
)
# base href 가 있으면 /resource/ 루트 상대 참조가 원본 사이트로 해석된다
_BASE_TAG_RE = re.compile(r"<base\b[^>]*>", re.IGNORECASE)

_NAME_RE = re.compile(r"([0-9a-f]{64})(\.[a-z0-9]{2,6})")


def is_valid_name(name: str) -> bool:
    """CAS 자원 이름 형식 검증 — sha256 hex + 화이트리스트 확장자."""
    m = _NAME_RE.fullmatch(name)
    return bool(m and m.group(2) in EXT_MEDIA_TYPES)


def resource_path(name: str) -> Path:
    """자원 이름 → CAS 경로. 호출 전 is_valid_name 검증 필수."""
    return config.RESOURCES_DIR / name[:2] / name


def _store(data: bytes, ext: str) -> str:
    """자원 바이트를 CAS 에 저장하고 이름 반환. 이미 있으면 그대로 쓴다.

    이미 있는 파일은 mtime 만 갱신한다 — 고아 자원 정리(sweep)의 유예 창이
    "방금 다시 쓰이기 시작한 파일"을 진행 중 캡처의 커밋 전에 지우지 않게.
    """
    name = hashlib.sha256(data).hexdigest() + ext
    path = resource_path(name)
    if path.is_file():
        os.utime(path)
        return name
    path.parent.mkdir(parents=True, exist_ok=True)
    # 동시 아카이빙(스케줄러 + 수동)에 안전하도록 임시 파일 후 원자적 교체
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return name


_RESOURCE_REF_RE = re.compile(r"/resource/([0-9a-f]{64}\.[a-z0-9]{2,6})")


def referenced_names_in_html(html: str) -> list[str]:
    """page.html(.gz 해제 후) 텍스트가 참조하는 자원 CAS 이름 목록.

    저장공간 최적화의 참조 백필이 구형 스냅샷을 스캔하는 데 쓴다.
    형식이 유효한 이름만 (중복 제거·등장 순).
    """
    names: list[str] = []
    for m in _RESOURCE_REF_RE.finditer(html):
        name = m.group(1)
        if is_valid_name(name) and name not in names:
            names.append(name)
    return names


def cas_files() -> list[Path]:
    """자원 CAS 의 파일 전체 (이름 형식이 유효한 것만) — sweep 대상 스캔."""
    if not config.RESOURCES_DIR.is_dir():
        return []
    return sorted(
        f for f in config.RESOURCES_DIR.glob("*/*")
        if f.is_file() and is_valid_name(f.name)
    )


def delete_cas(names: list[str]) -> None:
    """참조가 사라진 자원 CAS 파일들 삭제 (빈 버킷 디렉토리도 정리).

    잔존 참조 판정(snapshot_resources)은 호출부(deletion.py)가 한다.
    """
    for name in names:
        if not is_valid_name(name):
            continue
        path = resource_path(name)
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass


def externalize_data_uris(html: str) -> tuple[str, list[str]]:
    """RESOURCE_MIN_BYTES 이상인 화이트리스트 data URI 를 CAS 로 추출.

    추출분은 ``/resource/{이름}`` 참조로 치환한다. 치환이 한 건이라도 있으면
    <base> 태그를 제거한다 — 원본 DOM 은 raw.html.gz 가 보존하므로 파생
    산출물인 page.html 에서는 제거해도 된다.
    (치환된 HTML, 추출된 CAS 이름 목록 — 중복 제거·등장 순) 반환.
    이름 목록은 snapshot_resources 참조 기록(GC 의 근거)이 된다.
    """
    names: list[str] = []

    def _repl(m: re.Match[str]) -> str:
        ext = _MIME_EXT.get(m.group(1).lower())
        if ext is None:
            return m.group(0)
        try:
            data = base64.b64decode(m.group(2))
        except (binascii.Error, ValueError):
            return m.group(0)
        if len(data) < config.RESOURCE_MIN_BYTES:
            return m.group(0)
        name = _store(data, ext)
        if name not in names:
            names.append(name)
        return "/resource/" + name

    out = _DATA_URI_RE.sub(_repl, html)
    if names:
        out = _BASE_TAG_RE.sub("", out)
    return out, names


def _gzip_replace(src: Path) -> Path:
    """src 를 gzip 압축한 ``{src}.gz`` 를 만들고 원본을 지운다."""
    dst = src.with_name(src.name + ".gz")
    dst.write_bytes(gzip.compress(src.read_bytes(), compresslevel=9))
    src.unlink()
    return dst


def _screenshot_to_webp(png: Path) -> Path | None:
    """screenshot.png → screenshot.webp 변환 후 원본 삭제. 실패 시 PNG 유지.

    WebP 는 한 변 16383px 제한이 있어 아주 긴 전체 페이지 스크린샷은
    변환에 실패할 수 있다 — 이 경우 None 을 반환하고 원본을 그대로 둔다.
    """
    from PIL import Image

    dst = png.with_name("screenshot.webp")
    try:
        with Image.open(png) as im:
            im.save(dst, "WEBP", quality=config.SCREENSHOT_WEBP_QUALITY, method=4)
    except Exception as e:
        logger.warning("스크린샷 WebP 변환 실패, PNG 유지: %s (%s)", png, e)
        dst.unlink(missing_ok=True)
        return None
    png.unlink()
    return dst


@dataclass
class CompactStats:
    """compact_snapshot_dir 결과 — 변환 전/후 바이트와 추출 자원 이름."""

    externalized: int = 0
    before_bytes: int = 0
    after_bytes: int = 0
    # 이번 변환에서 CAS 로 추출된 자원 이름 (snapshot_resources 기록용)
    resource_names: list[str] = field(default_factory=list)

    @property
    def saved_bytes(self) -> int:
        return self.before_bytes - self.after_bytes


def compact_snapshot_dir(snap_dir: Path) -> CompactStats:
    """스냅샷 디렉토리 산출물을 압축 저장 형태로 변환 (멱등).

    - page.html → data URI 추출 후 gzip → page.html.gz
    - raw.html → gzip → raw.html.gz
    - screenshot.png → screenshot.webp (변환 실패 시 PNG 유지)

    파이프라인이 임시 캡처 디렉토리에, ``wccg compact`` 가 기존 스냅샷에
    같은 변환을 적용한다 (스냅샷 불변 원칙의 유일한 예외 — 내용 보존 변환).
    이미 변환된 디렉토리는 건드릴 것이 없어 그대로 통과한다.
    """
    stats = CompactStats()

    page = snap_dir / "page.html"
    if page.is_file():
        stats.before_bytes += page.stat().st_size
        html, stats.resource_names = externalize_data_uris(
            page.read_text(encoding="utf-8")
        )
        stats.externalized = len(stats.resource_names)
        dst = snap_dir / "page.html.gz"
        dst.write_bytes(gzip.compress(html.encode("utf-8"), compresslevel=9))
        page.unlink()
        stats.after_bytes += dst.stat().st_size

    raw = snap_dir / "raw.html"
    if raw.is_file():
        stats.before_bytes += raw.stat().st_size
        stats.after_bytes += _gzip_replace(raw).stat().st_size

    png = snap_dir / "screenshot.png"
    if png.is_file():
        before = png.stat().st_size
        webp = _screenshot_to_webp(png)
        if webp is not None:
            stats.before_bytes += before
            stats.after_bytes += webp.stat().st_size

    return stats


def snapshot_dirs() -> list[Path]:
    """확정된(meta.json 이 있는) 스냅샷 디렉토리 전체 — compact 대상 스캔.

    finalize_snapshot 은 meta.json 을 마지막에 쓰므로, 저장 진행 중인
    디렉토리는 자연스럽게 제외된다.
    """
    if not config.SITES_DIR.is_dir():
        return []
    return sorted(
        p for p in config.SITES_DIR.glob("*/*/*") if (p / "meta.json").is_file()
    )


# compact_snapshot_dir 가 변환하는 구형 산출물 이름
_LEGACY_NAMES = ("page.html", "raw.html", "screenshot.png")


def needs_compaction(snap_dir: Path) -> bool:
    """압축 변환이 필요한 구형 산출물(또는 files/ 의 구형 문서)이 남아 있는지."""
    return any(
        (snap_dir / name).is_file() for name in _LEGACY_NAMES
    ) or documents.has_legacy_documents(snap_dir)


def compactable_count() -> int:
    """압축 변환 대상(구형 산출물이 남은) 스냅샷 수.

    CLI 와 대시보드가 압축 기능의 노출/실행 여부를 판정하는 기준 —
    0 이면 compact 를 실행할 필요가 없다.
    """
    return sum(1 for d in snapshot_dirs() if needs_compaction(d))


def _tree_bytes(root: Path) -> int:
    """디렉토리 전체 파일 용량 (없으면 0)."""
    if not root.is_dir():
        return 0
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


@dataclass
class CompactRunResult:
    """compact_all 결과 — 전체 대상 수와 변환 합계."""

    total: int = 0          # 대상 스냅샷 수
    converted: int = 0      # 이번 실행에서 변환된 스냅샷 수
    externalized: int = 0   # CAS 로 추출한 자원 수
    documents: int = 0      # 문서 CAS 로 이전한 구형 files/ 문서 수
    before_bytes: int = 0
    after_bytes: int = 0    # CAS 에 새로 추가된 자원 용량 포함

    @property
    def saved_bytes(self) -> int:
        return self.before_bytes - self.after_bytes


def compact_all() -> CompactRunResult:
    """모든 확정 스냅샷에 압축 변환을 적용 (멱등).

    CLI(``wccg compact``)와 대시보드 시스템 메뉴가 공유하는 단일 진입점.
    스냅샷 산출물 변환에 더해 구형 files/ 문서를 문서 CAS 로 이전한다
    (documents.compact_legacy_documents — 중복 내용은 한 번만 남는다).
    after_bytes 에는 추출 자원·이전 문서가 CAS 에 차지하는 증가분을 포함해
    절약량이 과장되지 않게 한다. 변환이 있었으면 픽셀 diff 캐시를 비운다 —
    스크린샷 형식이 바뀌어 어긋날 수 있고, 재생성 가능하다.
    """
    dirs = snapshot_dirs()
    result = CompactRunResult(total=len(dirs))
    cas_before = _tree_bytes(config.RESOURCES_DIR)
    for d in dirs:
        stats = compact_snapshot_dir(d)
        if stats.before_bytes == 0:
            continue  # 이미 변환된 스냅샷
        result.converted += 1
        result.externalized += stats.externalized
        result.before_bytes += stats.before_bytes
        result.after_bytes += stats.after_bytes
    result.after_bytes += _tree_bytes(config.RESOURCES_DIR) - cas_before
    doc_stats = documents.compact_legacy_documents()
    result.documents = doc_stats.moved
    result.before_bytes += doc_stats.before_bytes
    result.after_bytes += doc_stats.after_bytes
    if result.converted:
        shutil.rmtree(config.CACHE_DIR, ignore_errors=True)
    return result
