"""스냅샷 자원 콘텐츠 주소 저장소(CAS)와 저장 압축.

page.html 은 자원이 base64 data URI 로 인라인된 단일 HTML 이라 스냅샷 용량의
대부분을 차지하고, 같은 페이지를 재아카이빙하면 변하지 않은 이미지·폰트가
스냅샷마다 통째로 중복 저장된다. 이 모듈은 저장 공간을 줄이기 위해:

- 큰 data URI 자원을 sha256 콘텐츠 주소 저장소
  (``resources/{해시 앞 2자}/{sha256}{확장자}``)로 추출해 스냅샷 간 공유하고
  (base64 → 바이너리 변환으로 추가 ~25% 절감),
- 큰 인라인 ``<style>`` 블록(캡처가 인라인한 사이트 공통 CSS)도 같은 CAS 로
  추출해 ``<link href="/resource/...">`` 참조로 바꾸고,
- 자원이 빠진 HTML 텍스트는 gzip 으로 저장하며 (page.html.gz / raw.html.gz),
- 스크린샷 PNG 는 WebP 로 변환한다 (한도 초과로 실패하거나 결과가 더 크면
  PNG 유지 + 마커를 남겨 재시도하지 않는다).

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
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from . import config, documents, storage

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


def _write_cas(name: str, payload: bytes) -> str:
    """CAS 에 payload 를 기록하고 이름 반환. 이미 있으면 그대로 쓴다.

    이미 있는 파일은 mtime 만 갱신한다 — 고아 자원 정리(sweep)의 유예 창이
    "방금 다시 쓰이기 시작한 파일"을 진행 중 캡처의 커밋 전에 지우지 않게.
    """
    store = config.blob_store()
    path = resource_path(name)
    if store.is_file(path):
        store.touch_mtime(path)
        return name
    store.write_atomic(path, payload)
    return name


def _store(data: bytes, ext: str) -> str:
    """자원 바이트를 CAS 에 저장하고 이름 반환 (이름 = 내용 sha256 + 확장자)."""
    if ext == ".css":
        return _store_css(data)
    return _write_cas(hashlib.sha256(data).hexdigest() + ext, data)


def _store_css(data: bytes) -> str:
    """CSS 텍스트를 CAS 에 gzip 으로 저장 (이름은 원문 sha256 + .css).

    CSS 는 텍스트라 압축률이 높고 추출 전에는 page.html.gz 안에 압축돼
    있었으므로, 원문 그대로 저장하면 추출이 오히려 총용량을 늘린다.
    /resource/ 라우트가 gzip 매직 바이트를 감지해 Content-Encoding: gzip
    으로 서빙한다 (구형 아카이브의 비압축 .css 와 공존 가능).

    이름(원문 sha256)으로 존재를 먼저 확인하고, 신규일 때만 gzip 압축한다 —
    재아카이빙에서 안 바뀐 CSS 를 매번 level=9 로 재압축하는 낭비를 막는다.
    """
    name = hashlib.sha256(data).hexdigest() + ".css"
    path = resource_path(name)
    if config.blob_store().is_file(path):
        config.blob_store().touch_mtime(path)  # _write_cas 와 동일 — sweep 유예 창 갱신
        return name
    return _write_cas(name, gzip.compress(data, compresslevel=9, mtime=0))


def is_gzipped(path: Path) -> bool:
    """CAS 파일이 gzip 으로 저장됐는지 (매직 바이트) — /resource/ 서빙용.

    서빙 경로가 항상 로컬(또는 백엔드가 materialize 한) 파일 경로를 넘기므로
    매직 바이트는 그 로컬 파일에서 직접 읽는다.
    """
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"
    except OSError:
        return False


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
    store = config.blob_store()
    if not store.is_dir(config.RESOURCES_DIR):
        return []
    return sorted(
        f for f in store.glob(config.RESOURCES_DIR, "*/*")
        if store.is_file(f) and is_valid_name(f.name)
    )


def delete_cas(names: list[str]) -> None:
    """참조가 사라진 자원 CAS 파일들 삭제 (빈 버킷 디렉토리도 정리).

    잔존 참조 판정(snapshot_resources)은 호출부(deletion.py)가 한다.
    """
    store = config.blob_store()
    for name in names:
        if not is_valid_name(name):
            continue
        path = resource_path(name)
        store.delete(path)
        store.rmdir(path.parent)


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


_STYLE_BLOCK_RE = re.compile(
    r"<style\b([^>]*)>(.*?)</style\s*>", re.DOTALL | re.IGNORECASE
)
_MEDIA_ATTR_RE = re.compile(r"""\bmedia\s*=\s*("[^"]*"|'[^']*')""", re.IGNORECASE)
_BASE_HREF_RE = re.compile(
    r"""<base\b[^>]*\bhref\s*=\s*["']?([^"'>\s]+)""", re.IGNORECASE
)
# CSS 안의 외부 참조 — url(...) 과 @import "..." (@import url(...) 은 앞에서 잡힌다)
_CSS_URL_REF_RE = re.compile(r"""url\(\s*(['"]?)([^'")\s]+)\1\s*\)""", re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(r"""(@import\s+)(['"])([^'"]+)\2""", re.IGNORECASE)
# <style> → 외부 .css 이동이 참조 해석을 바꾸지 않는 안전한 접두사.
# data:·절대 URL·프래그먼트는 기준 무관, /resource/ 는 루트 상대라 동일 해석
# (이 형태는 externalize_data_uris 가 만들고, 그때 <base> 는 이미 제거된다).
_SAFE_CSS_REF_PREFIXES = ("data:", "http://", "https://", "//", "#", "/resource/")


def _css_refs_relocatable(css: str) -> bool:
    """CSS 텍스트의 url()/@import 참조가 전부 위치 무관 형태인지.

    상대 경로 참조는 <style>(문서 기준)과 외부 .css(/resource/ 기준)에서
    다르게 해석되므로, 남아 있으면 추출하지 않고 인라인을 유지한다.
    """
    for m in _CSS_URL_REF_RE.finditer(css):
        ref = m.group(2).strip()
        if ref and not ref.lower().startswith(_SAFE_CSS_REF_PREFIXES):
            return False
    for m in _CSS_IMPORT_RE.finditer(css):
        ref = m.group(3).strip()
        if ref and not ref.lower().startswith(_SAFE_CSS_REF_PREFIXES):
            return False
    return True


def _absolutize_css_refs(css: str, base_url: str) -> str:
    """CSS 의 상대 url()/@import 참조를 base_url 기준 절대 URL 로 재작성.

    원본 페이지의 인라인 <style> 안에서 상대 참조는 페이지 URL(또는 <base>)
    기준으로 해석됐다 — 절대화는 그 원래 해석을 복원하는 변환으로, 캡처가
    <link> 스타일시트를 인라인할 때 하는 절대화(capture._absolutize_css_urls)
    와 같은 의미다. 절대화된 블록은 외부 .css 로 옮겨도 해석이 같다.
    """

    def _url_repl(m: re.Match[str]) -> str:
        ref = m.group(2).strip()
        if ref.lower().startswith(_SAFE_CSS_REF_PREFIXES):
            return m.group(0)
        return f"url({urljoin(base_url, ref)})"

    def _import_repl(m: re.Match[str]) -> str:
        ref = m.group(3).strip()
        if ref.lower().startswith(_SAFE_CSS_REF_PREFIXES):
            return m.group(0)
        return f'{m.group(1)}{m.group(2)}{urljoin(base_url, ref)}{m.group(2)}'

    return _CSS_IMPORT_RE.sub(_import_repl, _CSS_URL_REF_RE.sub(_url_repl, css))


def externalize_style_blocks(
    html: str, base_url: str | None = None
) -> tuple[str, list[str]]:
    """RESOURCE_MIN_BYTES 이상인 인라인 ``<style>`` 블록을 CAS(.css)로 추출.

    캡처가 외부 스타일시트를 ``<style>`` 텍스트로 인라인하므로, 사이트 공통
    CSS 가 스냅샷마다 통째로 중복 저장된다. 추출분은
    ``<link rel="stylesheet" href="/resource/{이름}">`` 으로 치환해 같은
    내용은 아카이브 전체에서 한 번만 저장되게 한다 (media 속성은 유지,
    파일 본체는 gzip — _store_css).

    base_url(페이지의 final_url)이 주어지면 상대 url()/@import 참조를 먼저
    절대화한다(_absolutize_css_refs — 문서에 <base href> 가 있으면 그 기준).
    의미가 바뀔 수 있는 블록은 그대로 둔다 — 절대화 후에도 상대 참조가 남는
    블록(_css_refs_relocatable)과 ``<svg>`` 안의 블록(<link> 는 SVG
    컨텍스트에서 동작하지 않음). 치환이 있으면 externalize_data_uris 와
    같은 이유로 <base> 태그를 제거한다.
    (치환된 HTML, 추출 CAS 이름 목록 — 중복 제거·등장 순) 반환.
    """
    names: list[str] = []
    lowered = html.lower()
    effective_base = base_url
    if base_url:
        base_tag = _BASE_HREF_RE.search(html)
        if base_tag:
            effective_base = urljoin(base_url, base_tag.group(1))

    def _repl(m: re.Match[str]) -> str:
        body = m.group(2)
        if len(body.encode("utf-8")) < config.RESOURCE_MIN_BYTES:
            return m.group(0)
        head = lowered[: m.start()]
        if head.count("<svg") > head.count("</svg"):
            return m.group(0)
        if effective_base:
            body = _absolutize_css_refs(body, effective_base)
        if not _css_refs_relocatable(body):
            return m.group(0)
        name = _store(body.encode("utf-8"), ".css")
        if name not in names:
            names.append(name)
        media = _MEDIA_ATTR_RE.search(m.group(1))
        media_attr = f" media={media.group(1)}" if media else ""
        return f'<link rel="stylesheet" href="/resource/{name}"{media_attr}>'

    out = _STYLE_BLOCK_RE.sub(_repl, html)
    if names:
        out = _BASE_TAG_RE.sub("", out)
    return out, names


def _gzip_replace(src: Path) -> Path:
    """src 를 gzip 압축한 ``{src}.gz`` 를 만들고 원본을 지운다."""
    store = config.blob_store()
    dst = src.with_name(src.name + ".gz")
    store.write_bytes(dst, gzip.compress(store.read_bytes(src), compresslevel=9))
    store.delete(src)
    return dst


def _screenshot_to_webp(png: Path) -> Path | None:
    """스크린샷 PNG → 같은 이름의 WebP 변환 후 원본 삭제. 변환 안 하면 None.

    screenshot.png → screenshot.webp, screenshot-mobile.png →
    screenshot-mobile.webp 처럼 베이스 이름은 입력 PNG 에서 파생한다.

    WebP 는 한 변 16383px 제한이 있어 아주 긴 전체 페이지 스크린샷은 변환에
    실패할 수 있고, 변환 결과가 원본보다 커질 수도 있다 — 두 경우 모두 원본
    PNG 를 유지하고 마커(<png 이름>.keep)를 남겨 이후 재시도와 압축 대상
    카운트(needs_compaction)에서 제외한다.
    """
    from PIL import Image

    store = config.blob_store()
    dst = png.with_suffix(".webp")
    marker = png.with_name(png.name + ".keep")
    try:
        with Image.open(store.local_path(png)) as im:
            im.save(
                store.local_path(dst), "WEBP",
                quality=config.SCREENSHOT_WEBP_QUALITY, method=4,
            )
    except Exception as e:
        logger.warning("스크린샷 WebP 변환 실패, PNG 유지: %s (%s)", png, e)
        store.delete(dst)
        store.touch_create(marker)
        return None
    if store.size(dst) >= store.size(png):
        logger.info("WebP 가 PNG 보다 작지 않아 변환 생략, PNG 유지: %s", png)
        store.delete(dst)
        store.touch_create(marker)
        return None
    store.delete(png)
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


def compact_snapshot_dir(
    snap_dir: Path, base_url: str | None = None
) -> CompactStats:
    """스냅샷 디렉토리 산출물을 압축 저장 형태로 변환 (멱등).

    - page.html → data URI·인라인 <style> 추출 후 gzip → page.html.gz
    - raw.html → gzip → raw.html.gz
    - screenshot.png → screenshot.webp (변환 실패 시 PNG 유지)
    - screenshot-mobile.png → screenshot-mobile.webp (있을 때만, 같은 규칙)

    파이프라인이 임시 캡처 디렉토리에, ``wccg compact`` 가 기존 스냅샷에
    같은 변환을 적용한다 (스냅샷 불변 원칙의 유일한 예외 — 내용 보존 변환).
    이미 변환된 디렉토리는 건드릴 것이 없어 그대로 통과한다.
    base_url 은 페이지의 final_url — 인라인 <style> 의 상대 참조 절대화 기준
    (externalize_style_blocks 참조).
    """
    stats = CompactStats()
    store = config.blob_store()

    page = snap_dir / "page.html"
    if store.is_file(page):
        stats.before_bytes += store.size(page)
        html, stats.resource_names = externalize_data_uris(
            store.read_text(page, encoding="utf-8")
        )
        html, css_names = externalize_style_blocks(html, base_url)
        stats.resource_names += [
            n for n in css_names if n not in stats.resource_names
        ]
        stats.externalized = len(stats.resource_names)
        dst = snap_dir / "page.html.gz"
        store.write_bytes(dst, gzip.compress(html.encode("utf-8"), compresslevel=9))
        store.delete(page)
        stats.after_bytes += store.size(dst)

    raw = snap_dir / "raw.html"
    if store.is_file(raw):
        stats.before_bytes += store.size(raw)
        stats.after_bytes += store.size(_gzip_replace(raw))

    # 데스크탑·모바일 스크린샷 모두 같은 규칙으로 WebP 변환 (마커가 있으면 생략)
    for png_name, marker_name in (
        ("screenshot.png", storage.WEBP_SKIP_MARKER),
        ("screenshot-mobile.png", storage.MOBILE_WEBP_SKIP_MARKER),
    ):
        png = snap_dir / png_name
        if store.is_file(png) and not store.is_file(snap_dir / marker_name):
            before = store.size(png)
            webp = _screenshot_to_webp(png)
            if webp is not None:
                stats.before_bytes += before
                stats.after_bytes += store.size(webp)

    return stats


def snapshot_dirs() -> list[Path]:
    """확정된(meta.json 이 있는) 스냅샷 디렉토리 전체 — compact 대상 스캔.

    finalize_snapshot 은 meta.json 을 마지막에 쓰므로, 저장 진행 중인
    디렉토리는 자연스럽게 제외된다.
    """
    store = config.blob_store()
    if not store.is_dir(config.SITES_DIR):
        return []
    return sorted(
        p for p in store.glob(config.SITES_DIR, "*/*/*")
        if store.is_file(p / "meta.json")
    )


# compact_snapshot_dir 가 변환하는 구형 산출물 이름
_LEGACY_NAMES = ("page.html", "raw.html", "screenshot.png", "screenshot-mobile.png")


def needs_compaction(snap_dir: Path) -> bool:
    """압축 변환이 필요한 구형 산출물(또는 files/ 의 구형 문서)이 남아 있는지.

    WebP 변환을 하지 않기로 확정한 PNG(<png 이름>.keep 마커 보유 — 한도
    초과·용량 역효과)는 변환할 것이 없으므로 대상으로 세지 않는다. 데스크탑·
    모바일 스크린샷 각각의 마커를 따로 본다.
    """
    names: tuple[str, ...] = _LEGACY_NAMES
    skip = []
    if (snap_dir / storage.WEBP_SKIP_MARKER).is_file():
        skip.append("screenshot.png")
    if (snap_dir / storage.MOBILE_WEBP_SKIP_MARKER).is_file():
        skip.append("screenshot-mobile.png")
    if skip:
        names = tuple(n for n in names if n not in skip)
    return any(
        (snap_dir / name).is_file() for name in names
    ) or documents.has_legacy_documents(snap_dir)


# 구형 스크린샷 산출물 ↔ WebP 변환 보류 마커 (마커가 있으면 변환 대상 아님)
_LEGACY_SCREENSHOT_MARKERS = {
    "screenshot.png": storage.WEBP_SKIP_MARKER,
    "screenshot-mobile.png": storage.MOBILE_WEBP_SKIP_MARKER,
}


def compactable_count() -> int:
    """압축 변환 대상(구형 산출물이 남은) 스냅샷 수.

    CLI 와 대시보드가 압축 기능의 노출/실행 여부를 판정하는 기준 —
    0 이면 compact 를 실행할 필요가 없다.

    sites/ 전체를 **한 번만 나열(단일 LIST)** 해 메모리에서 판정한다 — 스냅샷마다
    meta.json HEAD + files/ LIST 를 하던 N+1 라운드트립(S3 모드에서 시스템 화면이
    수십 초~타임아웃)을 없앤다. 판정 기준은 needs_compaction 과 같다(구형 산출물·
    보류 마커·구형 files/ 문서). 직속 파일 이름만 보므로 디스크 내용을 읽지 않는다.
    """
    store = config.blob_store()
    if not store.is_dir(config.SITES_DIR):
        return 0
    # 스냅샷 디렉토리({domain}/{slug}/{ts}) → 그 안의 직속 이름 집합
    names_by_dir: dict[Path, set[str]] = {}
    for f in store.rglob(config.SITES_DIR, "*"):
        rel = f.relative_to(config.SITES_DIR).parts
        if len(rel) < 4:
            continue  # 스냅샷 디렉토리 자체·상위 — 직속 파일/하위만 센다
        snap_dir = config.SITES_DIR / rel[0] / rel[1] / rel[2]
        # rel[3] = 직속 파일명, 또는 하위 디렉토리(files/) 이름
        names_by_dir.setdefault(snap_dir, set()).add(rel[3])
    count = 0
    for names in names_by_dir.values():
        if "meta.json" not in names:
            continue  # 저장 진행 중 등 — 확정 스냅샷이 아니다
        legacy = "page.html" in names or "raw.html" in names or "files" in names
        for art, marker in _LEGACY_SCREENSHOT_MARKERS.items():
            if art in names and marker not in names:
                legacy = True
        if legacy:
            count += 1
    return count


def _meta_final_url(snap_dir: Path) -> str | None:
    """스냅샷 meta.json 의 final_url — 상대 CSS 참조 절대화 기준 (없으면 None)."""
    try:
        meta = json.loads(
            config.blob_store().read_text(snap_dir / "meta.json", encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    url = meta.get("final_url") or meta.get("url")
    return url if isinstance(url, str) and url else None


def _tree_bytes(root: Path) -> int:
    """디렉토리 전체 파일 용량 (없으면 0)."""
    store = config.blob_store()
    if not store.is_dir(root):
        return 0
    return sum(store.size(f) for f in store.rglob(root, "*") if store.is_file(f))


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
        stats = compact_snapshot_dir(d, _meta_final_url(d))
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
