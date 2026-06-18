"""아카이브 전문(full-text) 검색 — SQLite FTS5 trigram 인덱스.

코어 모듈(원칙 1 — 쓰기는 코어). 색인 본문은 스냅샷의 content.md(정규화
텍스트, 원칙 4 의 비교/해시 기준과 동일)에 첨부 문서(snapshot_documents →
문서 CAS)의 본문(doctext.py)을 더한 것이다. 색인 쓰기/조회 SQL 은 db.py 가
소유하고, 이 모듈은 텍스트 조립·문서 추출·쿼리 해석·스니펫 생성을 맡는다.

토크나이저는 trigram — 한국어 조사 결합 때문에 단어 토큰이 검색어와 잘
안 맞으므로, 길이 3+ 임의 부분문자열을 찾는 trigram 이 한국어 검색에
적합하다. trigram 이 못 잡는 1~2글자 쿼리는 LIKE 부분일치로 폴백한다.

신규 스냅샷은 pipeline 이 저장 시 즉시 색인하고(search_indexed=1), 실패·구형·
가져오기로 들어온 스냅샷(search_indexed=0)은 ``wccg search reindex`` 백필이
메운다 (optimize 의 자원 참조 백필과 같은 멱등 패턴).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config, db, doctext, documents, storage

logger = logging.getLogger(__name__)

# trigram 은 길이 3 이상 토큰만 색인한다 — 그보다 짧은 쿼리는 LIKE 폴백.
MIN_TRIGRAM_LENGTH = 3


# ---- 색인 (쓰기) ----


def _read_content_md(snap_dir: Path) -> str:
    """스냅샷의 content.md(정규화 텍스트). 없으면 빈 문자열."""
    path = snap_dir / "content.md"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _read_title(snap_dir: Path) -> str | None:
    """meta.json 의 title (없거나 파싱 실패 시 None)."""
    try:
        return storage.read_meta(snap_dir).title
    except (OSError, ValueError, TypeError):
        return None


def _documents_text(conn: sqlite3.Connection, snapshot_id: int) -> str:
    """스냅샷의 첨부 문서 본문 합본 (문서 CAS 에서 추출). best-effort — 빈 문자열 가능."""
    parts: list[str] = []
    for ref in db.list_snapshot_document_refs(conn, [snapshot_id]):
        try:
            name = documents.cas_name(ref["sha256"], ref["file"])
            if not name:
                continue
            text = doctext.extract_text(
                documents.cas_path(name), ext=Path(ref["file"]).suffix
            )
        except Exception as e:  # noqa: BLE001 — 문서 한 개의 실패가 색인을 막지 않게
            logger.info("문서 본문 추출 건너뜀 (%s): %s", ref["file"], e)
            continue
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _assemble(
    conn: sqlite3.Connection, snapshot_id: int, domain: str, slug: str, dir_name: str
) -> tuple[str, str | None]:
    """색인 본문(content.md + 문서 본문)과 제목을 조립."""
    snap_dir = storage.page_dir(domain, slug) / dir_name
    content_md = _read_content_md(snap_dir)
    title = _read_title(snap_dir)
    doc_text = _documents_text(conn, snapshot_id)
    content = "\n\n".join(p for p in (content_md, doc_text) if p)
    return content, title


def _index_row(conn: sqlite3.Connection, snap: sqlite3.Row) -> None:
    """snap(id, domain, slug, dir_name, page_url)을 색인. FTS row upsert."""
    content, title = _assemble(
        conn, snap["id"], snap["domain"], snap["slug"], snap["dir_name"]
    )
    db.upsert_snapshot_fts(conn, snap["id"], content, title, snap["page_url"])


def index_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> bool:
    """스냅샷 하나를 색인하고 search_indexed=1 표시. 인덱스 비활성/미존재면 False.

    pipeline 이 스냅샷 저장 직후(같은 conn) 호출한다. 호출부는 실패가
    아카이빙을 깨지 않도록 try/except 로 감싸며, 실패 시 search_indexed=0 이
    남아 백필이 나중에 메운다 (best-effort + 안전망).
    """
    if not db.search_index_available(conn):
        return False
    snap = db.get_snapshot(conn, snapshot_id)
    if snap is None:
        return False
    _index_row(conn, snap)
    db.mark_snapshot_search_indexed(conn, snapshot_id)
    return True


def available() -> bool:
    """검색 인덱스(FTS5)를 쓸 수 있는 환경인지."""
    with db.connect() as conn:
        return db.search_index_available(conn)


def pending_count() -> int:
    """아직 색인되지 않은 스냅샷 수 (reindex 대상). 인덱스 비활성이면 0."""
    with db.connect() as conn:
        if not db.search_index_available(conn):
            return 0
        return db.count_unindexed_search_snapshots(conn)


def backfill_all(progress: Callable[[int, int], None] | None = None) -> int:
    """미색인 스냅샷을 전수 색인 (멱등). 색인한 스냅샷 수 반환.

    content.md 가 없거나 추출이 실패해도 완료로 표시한다(빈 본문도 색인).
    중단 후 재실행 안전. progress(done, total) 콜백을 주면 스냅샷마다 호출한다.
    total 은 시작 시점의 미색인 수라 근사치다 — 재색인 도중 새로 들어온
    스냅샷은 다음 백필이 잡는다(라이브 색인이 안 되는 가져오기 등).

    스냅샷마다 트랜잭션을 커밋한다 — 전체를 한 트랜잭션으로 묶으면 재색인
    내내 DB 쓰기 락을 점유해 아카이빙 등 다른 쓰기가 'database is locked'
    로 막힌다(그리고 무거운 문서 본문 추출이 락 안에서 일어난다). 스냅샷
    단위로 끊으면 추출은 락 밖(WAL 읽기)에서 일어나고, 짧은 쓰기만 락을
    잠깐 잡았다 푼다. 처리 직전 스냅샷을 다시 조회해(index_snapshot 과 동일)
    그 사이 삭제된 스냅샷은 건너뛴다 — 없는 스냅샷에 FTS 행을 만들어 orphan
    이 생기는 것을 막는다(잔여 경합은 verify/repair 가 정리).
    """
    if not available():
        return 0
    with db.connect() as conn:
        targets = db.list_unindexed_search_snapshots(conn)
    total = len(targets)
    if progress is not None:
        progress(0, total)
    done = 0
    with db.connect() as conn:
        for snap in targets:
            sid = snap["id"]
            current = db.get_snapshot(conn, sid)
            if current is not None:  # 처리 전 동시 삭제됐으면 건너뜀(orphan 방지)
                try:
                    _index_row(conn, current)
                except Exception as e:  # noqa: BLE001 — 한 스냅샷 실패가 전체를 막지 않게
                    logger.warning("스냅샷 %d 검색 색인 실패: %s", sid, e)
                db.mark_snapshot_search_indexed(conn, sid)
                conn.commit()  # 스냅샷마다 커밋 — 쓰기 락을 오래 점유하지 않게
            done += 1
            if progress is not None:
                progress(done, total)
    if done:
        logger.info("검색 인덱스 백필: 스냅샷 %d개", done)
    return done


def reindex_all(progress: Callable[[int, int], None] | None = None) -> int:
    """인덱스를 비우고 전체 스냅샷을 다시 색인 (정규화 규칙 변경·정합성 교정 등).

    FTS 행을 통째로 지우고 모든 스냅샷을 미색인으로 되돌린 뒤 백필하므로,
    과소 색인·orphan·stale 을 한 번에 모두 바로잡는 완전 교정 수단이다.
    progress(done, total) 콜백은 백필 단계(느린 부분)에 전달된다.
    """
    if not available():
        return 0
    with db.connect() as conn:
        db.clear_search_index(conn)
        db.reset_search_indexed(conn)
    return backfill_all(progress=progress)


# ---- 정합성 점검 / 교정 ----


@dataclass
class VerifyReport:
    """검색 인덱스 정합성 점검 결과."""

    available: bool
    indexed: int = 0   # search_indexed=1 로 표시된 스냅샷 수
    pending: int = 0   # search_indexed=0 (미색인 — 정상적인 백필 대상)
    fts_rows: int = 0  # 실제 FTS 인덱스 행 수
    missing: int = 0   # 플래그=1 인데 FTS 행이 없음 (과소 색인 — '거짓말 플래그')
    orphan: int = 0    # FTS 행이 있는데 스냅샷이 없음 (정상 경로로는 안 생김)

    @property
    def consistent(self) -> bool:
        """어긋남(과소 색인·orphan)이 없는지. pending 은 정상 상태라 제외."""
        return self.missing == 0 and self.orphan == 0


def verify() -> VerifyReport:
    """플래그와 실제 FTS 행이 어긋나는지 점검 (순수 카운트 — 디스크 안 읽음)."""
    with db.connect() as conn:
        if not db.search_index_available(conn):
            return VerifyReport(available=False)
        return VerifyReport(
            available=True,
            indexed=db.count_search_indexed(conn),
            pending=db.count_unindexed_search_snapshots(conn),
            fts_rows=db.count_fts_rows(conn),
            missing=len(db.list_missing_fts_snapshot_ids(conn)),
            orphan=len(db.list_orphan_fts_rowids(conn)),
        )


@dataclass
class RepairResult:
    """정합성 교정 결과."""

    available: bool
    orphans_removed: int = 0  # 삭제한 orphan FTS 행 수
    reindexed: int = 0        # 다시 색인한 스냅샷 수 (과소 색인 + 기존 pending)


def repair() -> RepairResult:
    """발견한 불일치를 교정 — orphan FTS 행 삭제 + 과소 색인 스냅샷 재색인.

    orphan(스냅샷 없는 FTS 행)을 지우고, 과소 색인(플래그=1·FTS 행 없음)
    스냅샷을 미색인으로 되돌린 뒤 백필한다. 백필은 기존 pending 도 함께
    처리하므로, 끝나면 인덱스가 데이터와 일치한다. (전체 비우고 다시 만드는
    reindex_all 보다 가벼움 — 어긋난 것만 손본다.)
    """
    if not available():
        return RepairResult(available=False)
    with db.connect() as conn:
        orphans = db.list_orphan_fts_rowids(conn)
        db.delete_fts_rows(conn, orphans)
        for sid in db.list_missing_fts_snapshot_ids(conn):
            db.mark_snapshot_search_stale(conn, sid)
    reindexed = backfill_all()
    return RepairResult(
        available=True, orphans_removed=len(orphans), reindexed=reindexed
    )


# ---- 검색 (조회) ----


@dataclass
class SearchHit:
    """검색 결과 한 건."""

    snapshot_id: int
    page_id: int
    site_id: int | None
    page_url: str
    domain: str
    taken_at: str
    changed: int
    title: str | None
    snippet: str
    terms: list[str] = field(default_factory=list)


@dataclass
class SearchResults:
    """검색 결과 묶음. mode: fts | like | empty | unavailable."""

    hits: list[SearchHit]
    total: int
    mode: str


def _query_terms(query: str) -> list[str]:
    """쿼리를 공백 단위 토큰으로 (빈 토큰 제외)."""
    return [t for t in query.split() if t]


def _like_pattern(term: str) -> str:
    """LIKE 부분일치 패턴 — % _ \\ 를 이스케이프해 리터럴로 만든다."""
    esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _build_query(terms: list[str]) -> tuple[str | None, object]:
    """쿼리 토큰 → (mode, payload).

    모든 토큰이 길이 3+ 면 FTS(각 토큰을 따옴표로 감싼 phrase 의 AND — 따옴표
    이스케이프로 FTS 특수문자·구문 주입을 무력화). 1~2글자 토큰이 하나라도
    있으면 trigram 이 못 잡으므로 전체를 LIKE 부분일치(AND)로 폴백한다.
    """
    if not terms:
        return None, None
    if any(len(t) < MIN_TRIGRAM_LENGTH for t in terms):
        return "like", [_like_pattern(t) for t in terms]
    match = " ".join('"' + t.replace('"', '""') + '"' for t in terms)
    return "fts", match


def _make_snippet(content: str, terms: list[str], radius: int = 90, max_len: int = 260) -> str:
    """본문에서 첫 매치 주변을 잘라낸 스니펫 (매치가 없으면 앞부분)."""
    if not content:
        return ""
    low = content.lower()
    pos = -1
    for t in terms:
        i = low.find(t.lower())
        if i != -1 and (pos == -1 or i < pos):
            pos = i
    if pos == -1:
        head = content[:max_len].strip()
        return head + ("…" if len(content) > max_len else "")
    start = max(0, pos - radius)
    end = min(len(content), pos + len(terms[0]) + radius)
    snip = content[start:end].strip()
    if start > 0:
        snip = "…" + snip
    if end < len(content):
        snip = snip + "…"
    return snip


def _hit(row: sqlite3.Row, terms: list[str], snippet: str) -> SearchHit:
    return SearchHit(
        snapshot_id=row["snapshot_id"],
        page_id=row["page_id"],
        site_id=row["site_id"],
        page_url=row["page_url"],
        domain=row["domain"],
        taken_at=row["taken_at"],
        changed=row["changed"],
        title=row["title"] or None,
        snippet=snippet,
        terms=terms,
    )


def search(
    query: str,
    *,
    domain: str | None = None,
    latest_only: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> SearchResults:
    """전문 검색. 한국어 부분문자열(trigram) + 1~2글자 LIKE 폴백.

    domain 으로 도메인 한정, latest_only 면 URL 당 최신 스냅샷 1건만.
    """
    terms = _query_terms(query)
    mode, payload = _build_query(terms)
    if mode is None:
        return SearchResults([], 0, "empty")
    with db.connect() as conn:
        if not db.search_index_available(conn):
            return SearchResults([], 0, "unavailable")
        if mode == "fts":
            rows = db.search_snapshots_fts(
                conn, payload, domain=domain, latest_only=latest_only,
                limit=limit, offset=offset,
            )
            total = db.count_search_snapshots_fts(
                conn, payload, domain=domain, latest_only=latest_only
            )
            # 스니펫은 DB(FTS5 snippet())가 매치 주변만 잘라 준다 — 본문 전문을
            # Python 으로 가져오지 않는다.
            hits = [_hit(r, terms, r["snippet"]) for r in rows]
        else:
            rows = db.search_snapshots_like(
                conn, payload, domain=domain, latest_only=latest_only,
                limit=limit, offset=offset,
            )
            total = db.count_search_snapshots_like(
                conn, payload, domain=domain, latest_only=latest_only
            )
            # LIKE 폴백은 MATCH 가 없어 snippet() 을 못 써 content 에서 직접 만든다.
            hits = [_hit(r, terms, _make_snippet(r["content"] or "", terms)) for r in rows]
    return SearchResults(hits, total, mode)
