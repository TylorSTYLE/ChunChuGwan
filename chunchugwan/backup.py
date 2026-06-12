"""백업/복원. 전체 백업(tar.gz)과 아카이브 데이터 내보내기/가져오기.

- 전체 백업(kind=full): index.db 일관 복사본 + sites/ + resources/ +
  documents/ + rules.json. 복원은 아카이브 루트를 백업 시점 상태로 통째로
  되돌린다 (인증 데이터 포함).
- 아카이브 내보내기(kind=archive): pages/snapshots/checks/documents(문서
  참조)와 스냅샷 파일, 공유 자원(resources/)·문서 CAS(documents/)만.
  가져오기는 merge(기존 유지 + 중복 스킵) / overwrite(아카이브 데이터 교체).
  인증 테이블(users 등)과 실행 로그(archive_logs)는 건드리지 않는다.

보안 노트: tar 추출은 filter="data" 로 절대경로/상위탈출/심링크를 차단하고,
가져온 데이터의 domain/slug/dir_name 과 공유 자원 파일명은 경로 조립 전에
형식을 검증한다.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, documents, resources, storage

# 2: 압축 저장 형태 도입 — 공유 자원 디렉토리(resources/) 포함,
#    스냅샷 파일이 page.html.gz/raw.html.gz/screenshot.webp 일 수 있음.
# 3: 문서 CAS 도입 — 문서 디렉토리(documents/)와 snapshot_documents 참조
#    (archive.json 의 documents 목록) 포함.
#    구버전 백업/내보내기는 그대로 읽을 수 있다.
FORMAT_VERSION = 3
MANIFEST_NAME = "manifest.json"
ARCHIVE_DATA_NAME = "archive.json"

# DB 값 → 파일시스템 경로 조립 시 path traversal 방지용 형식 검증
_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+(:[0-9]+)?$")
_SLUG_RE = re.compile(r"^[a-z0-9-]+$")
_DIR_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}$")


@dataclass
class ImportResult:
    """import_archive 결과 요약."""

    pages_added: int = 0
    snapshots_added: int = 0
    snapshots_skipped: int = 0
    checks_added: int = 0


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_dest(dest: Path, prefix: str) -> Path:
    """dest 가 디렉토리면 시각 붙은 기본 파일명을 만들고, 아니면 그대로 쓴다."""
    if dest.is_dir():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return dest / f"{prefix}-{stamp}.tar.gz"
    return dest


def _consistent_db_copy(out: Path) -> None:
    """실행 중에도 안전한 index.db 일관 복사본 생성 (sqlite backup API)."""
    with db.connect() as conn:
        dst = sqlite3.connect(out)
        try:
            conn.backup(dst)
        finally:
            dst.close()


def _archive_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """manifest 에 기록할 테이블별 행 수."""
    return {
        t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
        for t in ("pages", "snapshots", "checks")
    }


def read_manifest(src: Path) -> dict:
    """백업 파일의 manifest 를 읽고 형식을 검증. 형식 오류면 ValueError."""
    with tarfile.open(src, "r:gz") as tar:
        try:
            f = tar.extractfile(MANIFEST_NAME)
        except KeyError:
            f = None
        if f is None:
            raise ValueError(f"{MANIFEST_NAME} 이 없습니다 — 춘추관 백업 파일이 아닙니다")
        manifest = json.loads(f.read().decode("utf-8"))
    kind = manifest.get("kind")
    version = manifest.get("format_version")
    if kind not in ("full", "archive"):
        raise ValueError(f"알 수 없는 백업 종류: {kind!r}")
    if not isinstance(version, int) or version > FORMAT_VERSION:
        raise ValueError(f"지원하지 않는 백업 형식 버전: {version!r}")
    return manifest


# ---- 전체 백업/복원 ----


def create_backup(dest: Path) -> Path:
    """전체 백업 tar.gz 생성 후 경로 반환. DB(인증 포함)·sites·rules.json 포함."""
    config.ensure_dirs()
    out = _resolve_dest(dest, "chunchugwan-backup")
    with db.connect() as conn:
        counts = _archive_counts(conn)
    manifest = {
        "kind": "full",
        "format_version": FORMAT_VERSION,
        "created_at": _utcnow(),
        "counts": counts,
    }
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _consistent_db_copy(tmp / "index.db")
        (tmp / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with tarfile.open(out, "w:gz") as tar:
            tar.add(tmp / MANIFEST_NAME, arcname=MANIFEST_NAME)
            tar.add(tmp / "index.db", arcname="index.db")
            if config.RULES_PATH.is_file():
                tar.add(config.RULES_PATH, arcname="rules.json")
            tar.add(config.SITES_DIR, arcname="sites")
            if config.RESOURCES_DIR.is_dir():
                tar.add(config.RESOURCES_DIR, arcname="resources")
            if config.DOCUMENTS_DIR.is_dir():
                tar.add(config.DOCUMENTS_DIR, arcname="documents")
    return out


def _replace_db_file(new_db: Path) -> None:
    """index.db 를 새 파일로 교체 — 이전 DB 의 WAL 잔재(-wal/-shm)도 함께
    지운다 (남으면 새 DB 에 옛 WAL 이 적용돼 손상된다). 같은 프로세스가
    이어서 DB 를 쓸 수 있으므로 스키마 보장 캐시도 무효화한다."""
    config.DB_PATH.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(f"{config.DB_PATH}{suffix}").unlink(missing_ok=True)
    shutil.move(new_db, config.DB_PATH)
    db.invalidate_schema_cache()


def restore_backup(src: Path) -> dict:
    """전체 백업에서 복원 — 아카이브 루트를 백업 시점 상태로 교체.

    현재 DB(인증 데이터 포함)·sites·rules.json 이 모두 백업 내용으로 대체된다.
    파괴적이므로 호출 전 확인은 CLI 가 책임진다. manifest 반환.
    """
    manifest = read_manifest(src)
    if manifest["kind"] != "full":
        raise ValueError(
            "전체 백업 파일이 아닙니다 — 아카이브 내보내기는 wccg import 로 가져오세요"
        )
    config.ensure_dirs()
    # 같은 파일시스템에서 원자적 move 가 되도록 루트 안에 임시 추출
    with tempfile.TemporaryDirectory(dir=config.ARCHIVE_ROOT) as td:
        tmp = Path(td)
        with tarfile.open(src, "r:gz") as tar:
            tar.extractall(tmp, filter="data")
        if not (tmp / "index.db").is_file():
            raise ValueError("백업에 index.db 가 없습니다")

        _replace_db_file(tmp / "index.db")

        shutil.rmtree(config.SITES_DIR, ignore_errors=True)
        if (tmp / "sites").is_dir():
            shutil.move(tmp / "sites", config.SITES_DIR)
        else:
            config.SITES_DIR.mkdir(parents=True, exist_ok=True)

        # 공유 자원·문서 CAS 도 백업 시점 상태로 — 백업에 없으면 비워서 일관성 유지
        shutil.rmtree(config.RESOURCES_DIR, ignore_errors=True)
        if (tmp / "resources").is_dir():
            shutil.move(tmp / "resources", config.RESOURCES_DIR)
        shutil.rmtree(config.DOCUMENTS_DIR, ignore_errors=True)
        if (tmp / "documents").is_dir():
            shutil.move(tmp / "documents", config.DOCUMENTS_DIR)

        config.RULES_PATH.unlink(missing_ok=True)
        if (tmp / "rules.json").is_file():
            shutil.move(tmp / "rules.json", config.RULES_PATH)

        # 파생물 캐시는 복원된 데이터와 어긋날 수 있으므로 비운다
        shutil.rmtree(config.CACHE_DIR, ignore_errors=True)
    return manifest


# ---- 아카이브 데이터 내보내기/가져오기 ----


def export_archive(dest: Path) -> Path:
    """아카이브 데이터(pages/snapshots/checks + 스냅샷 파일)만 tar.gz 로 내보낸다."""
    config.ensure_dirs()
    out = _resolve_dest(dest, "chunchugwan-export")
    with db.connect() as conn:
        pages = [
            {k: r[k] for k in ("url", "domain", "slug", "created_at")}
            for r in conn.execute("SELECT * FROM pages ORDER BY id")
        ]
        snapshots = [
            {
                k: r[k]
                for k in (
                    "page_url", "domain", "slug", "taken_at", "dir_name",
                    "content_hash", "final_url", "http_status", "changed", "note",
                )
            }
            for r in conn.execute(
                """
                SELECT s.*, p.url AS page_url, p.domain, p.slug
                FROM snapshots s JOIN pages p ON p.id = s.page_id
                ORDER BY s.id
                """
            )
        ]
        checks = [
            {k: r[k] for k in ("page_url", "checked_at", "content_hash")}
            for r in conn.execute(
                """
                SELECT c.*, p.url AS page_url
                FROM checks c JOIN pages p ON p.id = c.page_id
                ORDER BY c.id
                """
            )
        ]
        # 문서 참조 — 스냅샷은 (page_url, dir_name)으로 식별한다
        doc_rows = [
            {
                k: r[k]
                for k in (
                    "page_url", "dir_name", "url", "file", "bytes",
                    "sha256", "content_type",
                )
            }
            for r in conn.execute(
                """
                SELECT d.*, s.dir_name, p.url AS page_url
                FROM snapshot_documents d
                JOIN snapshots s ON s.id = d.snapshot_id
                JOIN pages p ON p.id = s.page_id
                ORDER BY d.id
                """
            )
        ]
        counts = _archive_counts(conn)

    manifest = {
        "kind": "archive",
        "format_version": FORMAT_VERSION,
        "created_at": _utcnow(),
        "counts": counts,
    }
    data = {
        "pages": pages, "snapshots": snapshots, "checks": checks,
        "documents": doc_rows,
    }
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (tmp / ARCHIVE_DATA_NAME).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        with tarfile.open(out, "w:gz") as tar:
            tar.add(tmp / MANIFEST_NAME, arcname=MANIFEST_NAME)
            tar.add(tmp / ARCHIVE_DATA_NAME, arcname=ARCHIVE_DATA_NAME)
            for s in snapshots:
                snap_dir = storage.page_dir(s["domain"], s["slug"]) / s["dir_name"]
                if snap_dir.is_dir():
                    tar.add(
                        snap_dir,
                        arcname=f"sites/{s['domain']}/{s['slug']}/{s['dir_name']}",
                    )
            # 모든 스냅샷을 내보내므로 공유 자원·문서 CAS 도 전량 포함한다
            if config.RESOURCES_DIR.is_dir():
                tar.add(config.RESOURCES_DIR, arcname="resources")
            if config.DOCUMENTS_DIR.is_dir():
                tar.add(config.DOCUMENTS_DIR, arcname="documents")
    return out


def _validate_archive_data(data: dict) -> None:
    """가져올 데이터의 필수 키와 경로 구성요소 형식 검증. 위반 시 ValueError."""
    for key in ("pages", "snapshots", "checks"):
        if not isinstance(data.get(key), list):
            raise ValueError(f"{ARCHIVE_DATA_NAME} 에 {key} 목록이 없습니다")
    for p in data["pages"]:
        if not (_DOMAIN_RE.match(str(p.get("domain", ""))) and _SLUG_RE.match(str(p.get("slug", "")))):
            raise ValueError(f"잘못된 페이지 경로 구성요소: {p.get('domain')!r}/{p.get('slug')!r}")
        if not p.get("url"):
            raise ValueError("url 이 빈 페이지가 있습니다")
    for s in data["snapshots"]:
        if not (
            _DOMAIN_RE.match(str(s.get("domain", "")))
            and _SLUG_RE.match(str(s.get("slug", "")))
            and _DIR_NAME_RE.match(str(s.get("dir_name", "")))
        ):
            raise ValueError(
                f"잘못된 스냅샷 경로 구성요소: "
                f"{s.get('domain')!r}/{s.get('slug')!r}/{s.get('dir_name')!r}"
            )
        for key in ("page_url", "taken_at", "content_hash", "final_url"):
            if not s.get(key):
                raise ValueError(f"스냅샷에 {key} 가 없습니다: {s.get('dir_name')!r}")
    # documents 는 v3 부터 — 없으면(구버전) 빈 목록으로 취급
    for d in data.get("documents") or []:
        fname = str(d.get("file") or "")
        sha = str(d.get("sha256") or "")
        if documents.cas_name(sha, fname) is None or Path(fname).name != fname:
            raise ValueError(f"잘못된 문서 참조: {sha!r}/{fname!r}")
        if not (d.get("page_url") and _DIR_NAME_RE.match(str(d.get("dir_name", "")))):
            raise ValueError(f"문서 참조의 스냅샷 식별자가 잘못됐습니다: {fname!r}")


def _wipe_archive_data(conn: sqlite3.Connection) -> None:
    """pages/snapshots/checks 행과 스냅샷 파일을 비운다 (overwrite 모드).

    인증 테이블은 유지한다. archive_logs 는 실행 기록이므로 행은 남기되,
    삭제될 행을 가리키는 FK 만 비워 제약 위반을 막는다.
    schedules 는 페이지에 종속이므로 (id 가 사라진다) 함께 비운다.
    크롤(crawls/crawl_pages)도 사라질 스냅샷을 가리키므로 함께 비운다.
    """
    conn.execute("UPDATE archive_logs SET page_id = NULL, snapshot_id = NULL")
    conn.execute("DELETE FROM crawl_pages")
    conn.execute("DELETE FROM crawls")
    conn.execute("DELETE FROM schedules")
    conn.execute("DELETE FROM snapshot_documents")
    conn.execute("DELETE FROM snapshot_resources")
    conn.execute("DELETE FROM checks")
    conn.execute("DELETE FROM snapshots")
    conn.execute("DELETE FROM pages")
    # 사이트 행은 소속이 남은 것(크롤 스케줄 등)만 남기고 정리
    db.prune_empty_sites(conn)
    if config.SITES_DIR.is_dir():
        for child in config.SITES_DIR.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    shutil.rmtree(config.RESOURCES_DIR, ignore_errors=True)
    shutil.rmtree(config.DOCUMENTS_DIR, ignore_errors=True)
    shutil.rmtree(config.CACHE_DIR, ignore_errors=True)


def _merge_resources(src_root: Path) -> None:
    """가져온 공유 자원을 CAS 로 병합 — 콘텐츠 주소라 같은 이름은 같은 내용.

    이름 형식이 유효한 파일만 받는다 (resources.is_valid_name — path traversal).
    """
    if not src_root.is_dir():
        return
    for f in src_root.glob("*/*"):
        if not (f.is_file() and resources.is_valid_name(f.name)):
            continue
        dst = resources.resource_path(f.name)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(f, dst)


def _merge_documents(src_root: Path) -> None:
    """가져온 문서 CAS 파일 병합 (documents.is_valid_cas_name — path traversal)."""
    if not src_root.is_dir():
        return
    for f in src_root.glob("*/*"):
        if not (f.is_file() and documents.is_valid_cas_name(f.name)):
            continue
        dst = documents.cas_path(f.name)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(f, dst)


def import_archive(src: Path, mode: str = "merge") -> ImportResult:
    """내보낸 아카이브 데이터를 가져온다.

    - merge: 기존 데이터 유지. 페이지는 URL 로 매칭, 스냅샷은 같은 페이지에
      같은 dir_name 이 있으면 스킵 (멱등). checks 는 동일 행만 스킵.
    - overwrite: 기존 아카이브 데이터(행+파일)를 비우고 가져온다.
      두 모드 모두 인증 테이블과 archive_logs 행은 건드리지 않는다.
    """
    if mode not in ("merge", "overwrite"):
        raise ValueError(f"알 수 없는 모드: {mode!r}")
    manifest = read_manifest(src)
    if manifest["kind"] != "archive":
        raise ValueError(
            "아카이브 내보내기 파일이 아닙니다 — 전체 백업은 wccg restore 로 복원하세요"
        )
    config.ensure_dirs()
    result = ImportResult()
    with tempfile.TemporaryDirectory(dir=config.ARCHIVE_ROOT) as td:
        tmp = Path(td)
        with tarfile.open(src, "r:gz") as tar:
            tar.extractall(tmp, filter="data")
        data = json.loads((tmp / ARCHIVE_DATA_NAME).read_text(encoding="utf-8"))
        _validate_archive_data(data)

        with db.connect() as conn:
            if mode == "overwrite":
                _wipe_archive_data(conn)

            _merge_resources(tmp / "resources")
            _merge_documents(tmp / "documents")

            # 페이지: URL 매칭. 기존 페이지가 있으면 그 domain/slug 를 따른다.
            page_ids: dict[str, int] = {}
            page_paths: dict[str, tuple[str, str]] = {}
            for p in data["pages"]:
                row = db.get_page(conn, p["url"])
                if row is None:
                    site_id = db.get_or_create_site(conn, storage.site_key(p["url"]))
                    cur = conn.execute(
                        """
                        INSERT INTO pages (url, domain, slug, site_id, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (p["url"], p["domain"], p["slug"], site_id,
                         p["created_at"] or _utcnow()),
                    )
                    page_ids[p["url"]] = cur.lastrowid
                    page_paths[p["url"]] = (p["domain"], p["slug"])
                    result.pages_added += 1
                else:
                    page_ids[p["url"]] = row["id"]
                    page_paths[p["url"]] = (row["domain"], row["slug"])

            snap_ids: dict[tuple[str, str], int] = {}
            for s in data["snapshots"]:
                page_id = page_ids.get(s["page_url"])
                if page_id is None:
                    raise ValueError(f"pages 에 없는 URL 을 참조하는 스냅샷: {s['page_url']!r}")
                dup = conn.execute(
                    "SELECT id FROM snapshots WHERE page_id = ? AND dir_name = ?",
                    (page_id, s["dir_name"]),
                ).fetchone()
                if dup is not None:
                    snap_ids[(s["page_url"], s["dir_name"])] = dup["id"]
                    result.snapshots_skipped += 1
                    continue
                snap_ids[(s["page_url"], s["dir_name"])] = db.insert_snapshot(
                    conn, page_id,
                    taken_at=s["taken_at"], dir_name=s["dir_name"],
                    content_hash=s["content_hash"], final_url=s["final_url"],
                    http_status=s.get("http_status"),
                    changed=int(s.get("changed", 1)), note=s.get("note"),
                )
                result.snapshots_added += 1

                domain, slug = page_paths[s["page_url"]]
                src_dir = tmp / "sites" / s["domain"] / s["slug"] / s["dir_name"]
                dst_dir = storage.page_dir(domain, slug) / s["dir_name"]
                if src_dir.is_dir() and not dst_dir.exists():
                    dst_dir.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(src_dir, dst_dir)

            for c in data["checks"]:
                page_id = page_ids.get(c["page_url"])
                if page_id is None:
                    continue
                dup = conn.execute(
                    "SELECT 1 FROM checks WHERE page_id = ? AND checked_at = ? AND content_hash = ?",
                    (page_id, c["checked_at"], c["content_hash"]),
                ).fetchone()
                if dup is None:
                    conn.execute(
                        "INSERT INTO checks (page_id, checked_at, content_hash) VALUES (?, ?, ?)",
                        (page_id, c["checked_at"], c["content_hash"]),
                    )
                    result.checks_added += 1

            # 문서 참조 — 대상 스냅샷이 있는 행만, 중복은 무시 (멱등)
            for d in data.get("documents") or []:
                snapshot_id = snap_ids.get((d["page_url"], d["dir_name"]))
                if snapshot_id is None:
                    continue
                db.insert_snapshot_documents(conn, snapshot_id, [{
                    "url": d.get("url") or "",
                    "file": d["file"],
                    "bytes": int(d.get("bytes") or 0),
                    "sha256": d["sha256"],
                    "content_type": d.get("content_type") or "application/octet-stream",
                }])
    return result
