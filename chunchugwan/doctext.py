"""문서 파일(PDF·워드·한글 등) 본문 텍스트 추출 — 검색 색인용.

searchindex.py 가 첨부 문서(snapshot_documents → 문서 CAS)의 본문을 색인에
넣기 위해 호출한다. 추출은 best-effort 다 — 형식 미지원·라이브러리 부재·
파싱 실패는 모두 None 을 돌려주고, 그 경우 검색 색인에는 파일 메타데이터만
남는다 (아카이빙 자체는 영향받지 않는다).

지원: PDF(pypdf), OOXML(docx/pptx/xlsx), ODF(odt/odp/ods), 한글 HWPX, EPUB —
모두 zip+XML 이거나 pypdf 로 처리한다. 구형 바이너리(.doc/.ppt/.xls/.hwp 등)와
.zip/.pages/.key/.numbers 는 추출하지 않는다 (None — 메타데이터만 색인).
"""

from __future__ import annotations

import html
import logging
import re
import zipfile
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

# XML/HTML 태그 제거 → 텍스트 노드만 (검색용이라 구조는 버리고 단어만 남긴다)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(raw: str) -> str:
    """마크업 텍스트에서 태그 제거 + 엔티티 복원 + 공백 정규화."""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _zip_member_text(path: Path, predicate, limit: int) -> str | None:
    """zip 컨테이너에서 predicate(이름) 이 참인 멤버들의 텍스트를 모아 반환.

    누적 길이가 limit 에 도달하면 더 읽지 않는다 — 색인은 어차피 limit 에서
    잘리므로, 큰 문서의 나머지 멤버를 통째로 메모리에 올리는 낭비를 막는다.
    """
    parts: list[str] = []
    total = 0
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if not predicate(name):
                continue
            try:
                raw = zf.read(name).decode("utf-8", errors="replace")
            except (KeyError, OSError):
                continue
            cleaned = _clean(raw)
            if cleaned:
                parts.append(cleaned)
                total += len(cleaned) + 1
                if total >= limit:
                    break
    return " ".join(parts) if parts else None


def _pdf_text(path: Path, limit: int) -> str | None:
    """PDF 본문 — pypdf. 라이브러리 부재·파싱 실패는 None.

    누적 길이가 limit 에 도달하면 남은 페이지는 읽지 않는다 (조기 중단).
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf 미설치 — PDF 본문 색인이 생략됩니다")
        return None
    reader = PdfReader(str(path))
    pages = []
    total = 0
    for page in reader.pages:
        try:
            extracted = page.extract_text() or ""
        except Exception:  # 개별 페이지 파싱 실패는 건너뛴다
            continue
        pages.append(extracted)
        total += len(extracted) + 1
        if total >= limit:
            break
    text = _WS_RE.sub(" ", "\n".join(pages)).strip()
    return text or None


def _docx_text(path: Path, limit: int) -> str | None:
    return _zip_member_text(
        path,
        lambda n: n == "word/document.xml"
        or n.startswith("word/header")
        or n.startswith("word/footer"),
        limit,
    )


def _pptx_text(path: Path, limit: int) -> str | None:
    return _zip_member_text(
        path, lambda n: n.startswith("ppt/slides/slide") and n.endswith(".xml"), limit
    )


def _xlsx_text(path: Path, limit: int) -> str | None:
    # 공유 문자열(sharedStrings) + 인라인 시트 텍스트
    return _zip_member_text(
        path,
        lambda n: n == "xl/sharedStrings.xml"
        or (n.startswith("xl/worksheets/sheet") and n.endswith(".xml")),
        limit,
    )


def _odf_text(path: Path, limit: int) -> str | None:
    return _zip_member_text(path, lambda n: n == "content.xml", limit)


def _hwpx_text(path: Path, limit: int) -> str | None:
    # 한글 HWPX — 본문은 Contents/section*.xml (대소문자 변형 대비 소문자 비교)
    return _zip_member_text(
        path,
        lambda n: n.lower().startswith("contents/section") and n.endswith(".xml"),
        limit,
    )


def _epub_text(path: Path, limit: int) -> str | None:
    return _zip_member_text(
        path, lambda n: n.lower().endswith((".xhtml", ".html", ".htm")), limit
    )


_EXTRACTORS = {
    ".pdf": _pdf_text,
    ".docx": _docx_text,
    ".pptx": _pptx_text,
    ".xlsx": _xlsx_text,
    ".odt": _odf_text,
    ".odp": _odf_text,
    ".ods": _odf_text,
    ".hwpx": _hwpx_text,
    ".epub": _epub_text,
}


def can_extract(ext: str) -> bool:
    """확장자의 본문 추출을 지원하는지 (구형 바이너리·압축파일은 False)."""
    return ext.lower() in _EXTRACTORS


def extract_text(path: Path, *, ext: str | None = None) -> str | None:
    """문서 파일에서 본문 텍스트 추출 (실패·미지원·크기 초과는 None).

    크기 상한(config.SEARCH_DOC_TEXT_MAX_BYTES)을 넘는 파일은 파싱 비용이
    커서 건너뛰고, 추출 결과는 config.SEARCH_DOC_TEXT_MAX_CHARS 로 자른다.
    """
    if not path.is_file():
        return None
    suffix = (ext or path.suffix).lower()
    extractor = _EXTRACTORS.get(suffix)
    if extractor is None:
        return None
    try:
        if path.stat().st_size > config.SEARCH_DOC_TEXT_MAX_BYTES:
            logger.info("문서가 본문 추출 크기 상한 초과 — 메타데이터만 색인: %s", path.name)
            return None
        # 추출기에 상한을 넘겨 누적이 상한에 닿으면 조기 중단한다 — 어차피 아래에서
        # 자르므로, 큰 문서의 나머지를 통째로 메모리에 올리지 않는다(순위 8).
        text = extractor(path, config.SEARCH_DOC_TEXT_MAX_CHARS)
    except (zipfile.BadZipFile, OSError, ValueError) as e:
        logger.info("문서 본문 추출 실패 (%s): %s", path.name, e)
        return None
    except Exception as e:  # noqa: BLE001 — 서드파티 파서의 예외는 색인을 막지 않는다
        logger.info("문서 본문 추출 중 예기치 못한 오류 (%s): %s", path.name, e)
        return None
    if not text:
        return None
    if len(text) > config.SEARCH_DOC_TEXT_MAX_CHARS:
        text = text[: config.SEARCH_DOC_TEXT_MAX_CHARS]
    return text
