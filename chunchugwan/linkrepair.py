"""아카이브 링크 교정 — 구형 단일 페이지 스냅샷의 page.html 앵커 재작성.

신규 스냅샷은 캡처가 page.html 앵커를 리졸버(/goto·/crawl/{id}/goto)로
재작성하지만(capture.generic_link_rewriter·crawler.link_rewriter), 그 기능
도입 전 단일 페이지 스냅샷은 앵커가 재작성되지 않았다. compact 가 <base> 를
제거하면 상대/루트상대 앵커가 대시보드 오리진으로 풀려 깨진다.

이 모듈은 ``snapshots.links_rewritten=0`` 인 스냅샷의 page.html(.gz) 을 스캔해
앵커를 ``/goto?url=...`` 리졸버로 재작성한다 (searchindex 의 reindex 백필과
같은 구조 — CLI ``wccg links repair``·시스템 설정 "아카이브 링크 교정"이 공유).
page.html 재작성은 내용 보존 변환(원본 DOM 은 raw.html(.gz) 가 보존)이라
스냅샷 불변 원칙의 compact 류 예외다. 멱등 — 이미 리졸버로 가는 앵커
(/goto·/crawl/{id}/goto)·#·mailto·javascript·data 는 건드리지 않는다.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urljoin

from . import db, storage

logger = logging.getLogger(__name__)

_ANCHOR_RE = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
_HREF_RE = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_TARGET_RE = re.compile(
    r"""\btarget\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE
)
# <base href> 는 effective base 계산용, <base> 전체는 재작성 후 제거용
# (resources._BASE_HREF_RE/_BASE_TAG_RE 와 같은 정규식).
_BASE_HREF_RE = re.compile(
    r"""<base\b[^>]*\bhref\s*=\s*["']?([^"'>\s]+)""", re.IGNORECASE
)
_BASE_TAG_RE = re.compile(r"<base\b[^>]*>", re.IGNORECASE)

# 재작성하지 않는 href 접두사 — 이미 리졸버로 가거나(/goto·/crawl/), 문서 내
# 앵커·비웹 스킴. 루트상대 /crawl/ 는 크롤 스냅샷의 /crawl/{id}/goto 라
# 크롤셋 우선 의미를 보존한다(멱등).
_SKIP_HREF_PREFIXES = (
    "#", "mailto:", "javascript:", "tel:", "data:", "/goto", "/crawl/",
)


def rewrite_anchors(html: str, base_url: str | None) -> tuple[str, int]:
    """page.html 의 <a href> 를 ``/goto?url=...`` 리졸버로 재작성 (멱등).

    상대 href 는 effective base(문서의 <base href> 가 있으면 그 기준,
    없으면 base_url=final_url)로 절대화한 뒤 정규화해 리졸버로 보낸다.
    http(s) 로 풀리지 않는 링크·이미 리졸버로 가는 링크·비웹 스킴은 그대로
    둔다. 재작성한 앵커에는 ``target="_top"`` 을 붙인다(샌드박스 iframe 에서
    사용자 클릭 시 뷰어 전체 이동). 한 건이라도 재작성하면 <base> 를 제거한다
    (루트상대 /goto·/resource/ 가 원본 사이트로 해석되는 것을 막기 위해 —
    resources.externalize_data_uris 와 같은 이유). (재작성된 HTML, 건수) 반환.
    """
    effective_base = base_url
    if base_url:
        bt = _BASE_HREF_RE.search(html)
        if bt:
            effective_base = urljoin(base_url, bt.group(1))

    count = 0

    def _rewrite_tag(m: re.Match[str]) -> str:
        nonlocal count
        tag = m.group(0)
        hm = _HREF_RE.search(tag)
        if hm is None:
            return tag
        raw = hm.group(1) if hm.group(1) is not None else hm.group(2)
        val = (raw or "").strip()
        if not val or val.lower().startswith(_SKIP_HREF_PREFIXES):
            return tag
        abs_url = urljoin(effective_base or "", val)
        if not abs_url.lower().startswith(("http://", "https://")):
            return tag
        try:
            norm = storage.normalize_url(abs_url)
        except ValueError:
            return tag
        new_href = "/goto?url=" + quote(norm, safe="")
        new_tag = tag[: hm.start()] + f'href="{new_href}"' + tag[hm.end():]
        if _TARGET_RE.search(new_tag):
            new_tag = _TARGET_RE.sub('target="_top"', new_tag, count=1)
        else:
            new_tag = new_tag[:2] + ' target="_top"' + new_tag[2:]
        count += 1
        return new_tag

    out = _ANCHOR_RE.sub(_rewrite_tag, html)
    if count:
        out = _BASE_TAG_RE.sub("", out)
    return out, count


def _page_html_path(snap_dir: Path) -> tuple[Path, bool] | None:
    """(page.html 경로, gz 여부) — 없으면 None (파일이 지워진 로그 등)."""
    gz = snap_dir / "page.html.gz"
    if gz.is_file():
        return gz, True
    plain = snap_dir / "page.html"
    if plain.is_file():
        return plain, False
    return None


def _read_html(path: Path, is_gz: bool) -> str:
    raw = path.read_bytes()
    if is_gz:
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def _write_html(path: Path, is_gz: bool, html: str) -> None:
    """page.html(.gz) 을 새 내용으로 원자적으로 교체 (저장 형태는 보존)."""
    data = html.encode("utf-8")
    if is_gz:
        data = gzip.compress(data, compresslevel=9)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def repair_snapshot(snap) -> bool:
    """스냅샷 하나의 page.html 앵커를 재작성 (멱등). 실제로 바꿨으면 True.

    snap 은 domain·slug·dir_name·final_url 을 가진 row (db.get_snapshot 또는
    list_links_pending_snapshots). links_rewritten 플래그 갱신은 호출부가 한다.
    """
    snap_dir = storage.page_dir(snap["domain"], snap["slug"]) / snap["dir_name"]
    found = _page_html_path(snap_dir)
    if found is None:
        return False
    path, is_gz = found
    out, n = rewrite_anchors(_read_html(path, is_gz), snap["final_url"])
    if n == 0:
        return False
    _write_html(path, is_gz, out)
    return True


def pending_count() -> int:
    """앵커 재작성이 아직 안 된 스냅샷 수 (링크 교정 대상)."""
    with db.connect() as conn:
        return db.count_links_pending_snapshots(conn)


def backfill_all(progress: Callable[[int, int], None] | None = None) -> int:
    """미교정 스냅샷의 page.html 앵커를 전수 재작성 (멱등). 재작성한 스냅샷 수 반환.

    스냅샷마다 트랜잭션을 커밋한다 — 전체를 한 트랜잭션으로 묶으면 교정 내내
    DB 쓰기 락을 점유한다(searchindex.backfill_all 과 같은 이유). page.html 이
    없거나 재작성할 앵커가 없는 스냅샷도 완료로 표시한다(크롤 스냅샷은 이미
    /crawl/{id}/goto 라 변경 없이 완료 처리). 중단 후 재실행 안전.
    progress(done, total) 콜백을 주면 스냅샷마다 호출한다.
    """
    with db.connect() as conn:
        targets = db.list_links_pending_snapshots(conn)
    total = len(targets)
    if progress is not None:
        progress(0, total)
    done = 0
    rewritten = 0
    with db.connect() as conn:
        for snap in targets:
            sid = snap["id"]
            current = db.get_snapshot(conn, sid, include_trashed=True)
            if current is not None:  # 처리 전 동시 삭제됐으면 건너뜀
                try:
                    if repair_snapshot(current):
                        rewritten += 1
                except Exception as e:  # noqa: BLE001 — 한 건 실패가 전체를 막지 않게
                    logger.warning("스냅샷 %d 링크 교정 실패: %s", sid, e)
                db.mark_snapshot_links_rewritten(conn, sid)
                conn.commit()  # 스냅샷마다 커밋 — 쓰기 락을 오래 점유하지 않게
            done += 1
            if progress is not None:
                progress(done, total)
    if done:
        logger.info("링크 교정 백필: 스냅샷 %d개 처리(%d개 재작성)", done, rewritten)
    return rewritten
