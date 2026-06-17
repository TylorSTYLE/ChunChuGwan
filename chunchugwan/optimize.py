"""저장공간 최적화 — 압축 변환 + 인라인 스타일 추출 + 자원 참조 백필 + 고아 자원 정리.

CLI(``wccg compact``)와 대시보드 시스템 메뉴("저장공간 최적화")가 공유하는
단일 진입점. 네 단계를 순서대로 실행한다:

1. 압축 변환 — 구형 스냅샷을 압축 저장 형태로 변환 (resources.compact_all,
   내용 보존 — 스냅샷 불변 원칙의 유일한 예외).
2. 인라인 스타일 추출 — 추출이 안 된 스냅샷(snapshots.css_externalized=0,
   이 기능 도입 전 데이터)의 page.html.gz 에서 큰 인라인 <style> 블록을
   CAS(.css)로 추출해 스냅샷 간 공유한다
   (resources.externalize_style_blocks — 신규 스냅샷은 저장 시점에 적용).
3. 참조 백필 — 자원 참조가 기록되지 않은 스냅샷(snapshots.resources_indexed=0,
   이 기능 도입 전 데이터·가져오기로 들어온 데이터)의 page.html.gz 를 스캔해
   snapshot_resources 를 채운다 (원본 URL 은 알 수 없어 NULL).
4. 고아 정리(sweep) — 어떤 스냅샷도 참조하지 않는 resources/ CAS 파일 삭제.
   백필이 100% 끝난 경우에만 실행한다 — 참조 미기록 스냅샷이 남아 있으면
   그 스냅샷의 자원을 고아로 오인할 수 있다. 진행 중 캡처와의 경합은
   유예 창(최근 생성·갱신 파일 제외, config.RESOURCE_ORPHAN_GRACE_SECONDS)과
   삭제 직전 참조 재확인으로 막는다.

참조 테이블이 자리 잡은 뒤의 고아는 스냅샷 삭제 시점에 deletion.py 가 즉시
정리하므로, sweep 은 사실상 1회성 정리 + 안전망이다.
"""

from __future__ import annotations

import gzip
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, db, resources, storage

logger = logging.getLogger(__name__)


@dataclass
class OptimizeResult:
    """run() 결과 — 단계별 합계."""

    compact: resources.CompactRunResult = field(
        default_factory=resources.CompactRunResult
    )
    styles_snapshots: int = 0    # 인라인 <style> 추출로 다시 쓴 스냅샷 수
    styles_extracted: int = 0    # CAS 로 추출한 스타일 자원 수 (고유)
    styles_saved_bytes: int = 0  # 추출로 줄어든 용량 (CAS 증가분 차감)
    indexed: int = 0         # 참조를 백필한 스냅샷 수
    swept: int = 0           # 삭제한 고아 자원 수
    swept_bytes: int = 0     # 삭제한 고아 자원 용량
    sweep_skipped: bool = False  # 백필 미완료로 sweep 을 건너뛰었는지


def pending_counts() -> tuple[int, int, int]:
    """(압축 변환, 인라인 스타일 추출, 참조 백필) 대상 스냅샷 수.

    시스템 화면이 최적화 버튼의 노출/대상 표시를 판정하는 기준 — 모두 0 이면
    실행할 것이 없다 (이후의 고아는 삭제 시 GC 가 즉시 정리한다).
    """
    with db.connect() as conn:
        css_pending = db.count_css_pending_snapshots(conn)
        unindexed = db.count_unindexed_snapshots(conn)
    return resources.compactable_count(), css_pending, unindexed


def run() -> OptimizeResult:
    """저장공간 최적화 전체 실행 (멱등)."""
    result = OptimizeResult(compact=resources.compact_all())
    (
        result.styles_snapshots,
        result.styles_extracted,
        result.styles_saved_bytes,
    ) = _externalize_styles()
    result.indexed = _backfill_refs()
    result.swept, result.swept_bytes, result.sweep_skipped = _sweep_orphans()
    # 압축 변환·스타일 추출이 page.html.gz·스크린샷 형태를 바꿔 디렉토리 용량이
    # 달라졌으면 비정규화 bytes 를 파일시스템에서 다시 맞춘다 (집계 일관성).
    if result.compact.converted or result.styles_snapshots:
        with db.connect() as conn:
            db.backfill_snapshot_bytes(conn)
    return result


def _snapshot_page_html(domain: str, slug: str, dir_name: str) -> str | None:
    """스냅샷의 page.html(.gz) 텍스트 — 없으면 None (파일이 지워진 로그 등)."""
    snap_dir = storage.page_dir(domain, slug) / dir_name
    gz = snap_dir / "page.html.gz"
    if gz.is_file():
        return gzip.decompress(gz.read_bytes()).decode("utf-8", errors="replace")
    plain = snap_dir / "page.html"
    if plain.is_file():
        return plain.read_text(encoding="utf-8", errors="replace")
    return None


def _rewrite_gz(path: Path, html: str) -> int:
    """page.html.gz 를 새 내용으로 원자적으로 교체. 새 파일 크기 반환."""
    data = gzip.compress(html.encode("utf-8"), compresslevel=9)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return len(data)


def _externalize_styles() -> tuple[int, int, int]:
    """추출 미완료 스냅샷의 page.html.gz 에서 인라인 <style> 을 CAS 로 추출.

    사이트 공통 CSS 가 스냅샷마다 중복 저장된 것을 /resource/{sha256}.css
    공유로 바꾼다 (resources.externalize_style_blocks — 내용 보존 변환,
    스냅샷 불변 원칙의 예외인 compact 의 일부). 추출분의 참조는
    snapshot_resources 에 기록한다 (원본 URL 은 알 수 없어 NULL).
    디렉토리/파일이 없거나 추출할 블록이 없는 스냅샷도 완료로 표시한다.
    반환은 (다시 쓴 스냅샷 수, 추출한 고유 자원 수, 절약 바이트 — CAS
    증가분 차감).
    """
    rewritten = 0
    before = after = 0
    extracted: set[str] = set()
    with db.connect() as conn:
        targets = db.list_css_pending_snapshots(conn)
        if not targets:
            return 0, 0, 0
        cas_before = sum(f.stat().st_size for f in resources.cas_files())
        for snap in targets:
            gz = (
                storage.page_dir(snap["domain"], snap["slug"])
                / snap["dir_name"] / "page.html.gz"
            )
            if gz.is_file():
                html = gzip.decompress(gz.read_bytes()).decode(
                    "utf-8", errors="replace"
                )
                out, names = resources.externalize_style_blocks(
                    html, snap["final_url"]
                )
                if names:
                    before += gz.stat().st_size
                    after += _rewrite_gz(gz, out)
                    db.insert_snapshot_resources(
                        conn, snap["id"], [{"name": n, "url": None} for n in names]
                    )
                    rewritten += 1
                    extracted.update(names)
            db.mark_snapshot_css_externalized(conn, snap["id"])
    after += sum(f.stat().st_size for f in resources.cas_files()) - cas_before
    if rewritten:
        logger.info(
            "인라인 스타일 추출: 스냅샷 %d개에서 자원 %d개", rewritten, len(extracted)
        )
    return rewritten, len(extracted), before - after


def _backfill_refs() -> int:
    """참조 미기록 스냅샷의 page.html 을 스캔해 snapshot_resources 백필.

    압축 변환 직후라 보통 page.html.gz 를 읽는다. 디렉토리/파일이 없는
    스냅샷도 인덱스 완료로 표시한다 — 참조할 자원이 없다는 뜻이다.
    반환은 이번에 인덱스한 스냅샷 수.
    """
    done = 0
    with db.connect() as conn:
        targets = db.list_unindexed_snapshots(conn)
        for snap in targets:
            html = _snapshot_page_html(
                snap["domain"], snap["slug"], snap["dir_name"]
            )
            if html:
                names = resources.referenced_names_in_html(html)
                if names:
                    db.insert_snapshot_resources(
                        conn, snap["id"], [{"name": n, "url": None} for n in names]
                    )
            db.mark_snapshot_resources_indexed(conn, snap["id"])
            done += 1
    if done:
        logger.info("자원 참조 백필: 스냅샷 %d개 인덱스", done)
    return done


def _sweep_orphans() -> tuple[int, int, bool]:
    """어떤 스냅샷도 참조하지 않는 자원 CAS 파일 삭제.

    반환은 (삭제 수, 삭제 바이트, 건너뜀 여부). 백필이 끝나지 않았으면
    아무것도 지우지 않고 건너뛴다.
    """
    with db.connect() as conn:
        if db.count_unindexed_snapshots(conn) > 0:
            return 0, 0, True
        referenced = db.list_all_resource_names(conn)
    cutoff = time.time() - config.RESOURCE_ORPHAN_GRACE_SECONDS
    candidates = [
        f for f in resources.cas_files()
        if f.name not in referenced and f.stat().st_mtime <= cutoff
    ]
    if not candidates:
        return 0, 0, False
    # 후보 수집 후 새로 생긴 참조(동시 아카이빙의 기존 파일 재사용) 재확인.
    # _store 의 mtime 갱신 + 유예 창이 1차 방어, 이 재확인이 2차 방어다.
    with db.connect() as conn:
        alive = set(db.list_resource_refs_by_names(conn, [f.name for f in candidates]))
    swept = swept_bytes = 0
    for f in candidates:
        if f.name in alive:
            continue
        swept_bytes += f.stat().st_size
        swept += 1
        f.unlink(missing_ok=True)
        try:
            f.parent.rmdir()
        except OSError:
            pass
    if swept:
        logger.info("고아 자원 정리: %d개 · %dKB 삭제", swept, swept_bytes // 1024)
    return swept, swept_bytes, False
