"""백업/복원. 전체 백업(tar.gz)과 아카이브 데이터 내보내기/가져오기.

- 전체 백업(kind=full): index.db 일관 복사본 + sites/ + resources/ +
  documents/ + rules.json. 복원은 아카이브 루트를 백업 시점 상태로 통째로
  되돌린다 (인증 데이터 포함).
- 아카이브 내보내기(kind=archive): pages/snapshots/checks/documents(문서
  참조)·크롤 회차(crawls/crawl_pages)·사이트 인증서(site_certificates)·
  아카이브 로그(archive_logs)와 스냅샷 파일, 공유 자원(resources/)·문서
  CAS(documents/). 가져오기는 merge(기존 유지 + 중복 스킵) /
  overwrite(아카이브 데이터 교체). 인증 테이블(users 등)은 건드리지 않는다.

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
# 4: 크롤 회차(crawls — crawl_pages 내장)·사이트 인증서(certificates)·
#    아카이브 로그(logs) 포함 — 사이트 단위 내보내기가 기록을 온전히 옮긴다.
#    구버전 백업/내보내기는 그대로 읽을 수 있다 (없는 목록은 빈 것으로 취급).
FORMAT_VERSION = 4
MANIFEST_NAME = "manifest.json"
ARCHIVE_DATA_NAME = "archive.json"

# 백업·내보내기 tar.gz 의 gzip 압축 레벨. 용량 대부분이 이미 압축된 자원
# (page.html.gz·screenshot.webp·CAS gzip/webp·PDF/zip)이라 높은 레벨로 재압축해도
# 거의 안 줄고 CPU 만 크게 든다 — 레벨 1 로 낮춰 시간을 아낀다(출력은 여전히
# 표준 .tar.gz 라 restore/import 의 r:gz 와 호환). 잘 압축되는 DB·JSON 도 레벨 1
# 로 충분히 작아진다.
_BACKUP_COMPRESSLEVEL = 1

# 아카이브 내보내기 파일 확장자 — 내용은 tar.gz 지만 가져오기는 이 확장자만
# 인식한다(전체 백업 .ccg.backup 와 구분). 강제는 사용자 경계(CLI import·웹 업로드).
EXPORT_SUFFIX = ".ccg.export"
# 전체 백업 파일 확장자 — 내용은 tar.gz 지만 복원은 이 확장자만 인식한다.
# 강제는 사용자 경계(CLI restore·웹 업로드).
BACKUP_SUFFIX = ".ccg.backup"

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
    crawls_added: int = 0
    certificates_added: int = 0
    logs_added: int = 0


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_dest(dest: Path, prefix: str, ext: str = ".tar.gz") -> Path:
    """dest 가 디렉토리면 시각 붙은 기본 파일명을 만들고, 아니면 그대로 쓴다."""
    if dest.is_dir():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return dest / f"{prefix}-{stamp}{ext}"
    return dest


def is_export_filename(name: str) -> bool:
    """가져오기 입력 파일명이 내보내기 확장자(.ccg.export)인지 — 대소문자 무시."""
    return name.lower().endswith(EXPORT_SUFFIX)


def is_backup_filename(name: str) -> bool:
    """복원 입력 파일명이 전체 백업 확장자(.ccg.backup)인지 — 대소문자 무시."""
    return name.lower().endswith(BACKUP_SUFFIX)


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
    out = _resolve_dest(dest, "chunchugwan-backup", ext=BACKUP_SUFFIX)
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
        with tarfile.open(out, "w:gz", compresslevel=_BACKUP_COMPRESSLEVEL) as tar:
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


def finalize_migration(staging: Path) -> None:
    """춘추관 간 네트워크 이전(파일 단위 Pull)의 마무리 — 스테이징 디렉토리를
    아카이브 루트로 합쳐 데이터를 교체한다.

    `restore_backup` 과 같은 '아카이브 루트를 통째로 교체' 의미이나, 입력이
    tar.gz 가 아니라 받는 쪽이 파일 단위로 받아 쌓은 스테이징 디렉토리다
    (index.db + sites/resources/documents/rules.json, 일부 파일이 빠질 수 있음 —
    빠진 스냅샷 파일은 뷰어에서 graceful 404). DB 는 항상 완전 전송된 전제.
    staging 은 CACHE_DIR 밖(ARCHIVE_ROOT 직속)이어야 한다 — 캐시를 비울 때
    스테이징이 함께 지워지지 않도록.
    """
    db_file = staging / "index.db"
    if not db_file.is_file():
        raise ValueError("이전 스테이징에 index.db 가 없습니다")
    config.ensure_dirs()
    _replace_db_file(db_file)

    for name, target in (
        ("sites", config.SITES_DIR),
        ("resources", config.RESOURCES_DIR),
        ("documents", config.DOCUMENTS_DIR),
    ):
        shutil.rmtree(target, ignore_errors=True)
        src = staging / name
        if src.is_dir():
            shutil.move(str(src), str(target))
        elif name == "sites":
            target.mkdir(parents=True, exist_ok=True)

    config.RULES_PATH.unlink(missing_ok=True)
    rules_src = staging / "rules.json"
    if rules_src.is_file():
        shutil.move(str(rules_src), str(config.RULES_PATH))

    # 소스 DB 는 이전 모드(migration_mode=on)·소스 토큰 해시를 담고 있다 —
    # 받는 쪽이 그대로 켜진 채 시작하지 않도록 끈다 (정상 서비스로 시작).
    with db.connect() as conn:
        db.set_migration_mode(conn, False)

    # 파생물 캐시는 새 데이터와 어긋날 수 있으므로 비운다 (staging 은 캐시 밖이라 안전)
    shutil.rmtree(config.CACHE_DIR, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)


# ---- 아카이브 데이터 내보내기/가져오기 ----


def export_archive(dest: Path, site_id: int | None = None) -> Path:
    """아카이브 데이터(pages/snapshots/checks + 스냅샷 파일)만 tar.gz 로 내보낸다.

    site_id 를 주면 그 사이트 소속 페이지로 한정한다 — 공유 자원·문서 CAS 도
    내보내는 스냅샷이 참조하는 파일만 담는다. 파일 형식은 전체 내보내기와
    같아 가져오기(import_archive)로 똑같이 읽힌다. 사이트가 없으면 ValueError.
    """
    config.ensure_dirs()
    args: tuple = () if site_id is None else (site_id,)
    prefix = "chunchugwan-export"

    def _site_where(alias: str) -> str:
        return "" if site_id is None else f" WHERE {alias}.site_id = ?"

    where = _site_where("p")
    with db.connect() as conn:
        if site_id is not None:
            site = db.get_site(conn, site_id)
            if site is None:
                raise ValueError(f"사이트 없음: {site_id}")
            prefix += "-" + site["site_key"].replace(":", "_")
        pages = [
            {k: r[k] for k in ("url", "domain", "slug", "client_captured", "created_at")}
            for r in conn.execute(f"SELECT * FROM pages p{where} ORDER BY p.id", args)
        ]
        snapshots = [
            {
                k: r[k]
                for k in (
                    "page_url", "domain", "slug", "taken_at", "dir_name",
                    "content_hash", "final_url", "http_status", "changed", "note",
                    "origin", "incomplete", "bytes", "title",
                )
            }
            for r in conn.execute(
                f"""
                SELECT s.*, p.url AS page_url, p.domain, p.slug
                FROM snapshots s JOIN pages p ON p.id = s.page_id
                {where} ORDER BY s.id
                """,
                args,
            )
        ]
        checks = [
            {k: r[k] for k in ("page_url", "checked_at", "content_hash")}
            for r in conn.execute(
                f"""
                SELECT c.*, p.url AS page_url
                FROM checks c JOIN pages p ON p.id = c.page_id
                {where} ORDER BY c.id
                """,
                args,
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
                f"""
                SELECT d.*, s.dir_name, p.url AS page_url
                FROM snapshot_documents d
                JOIN snapshots s ON s.id = d.snapshot_id
                JOIN pages p ON p.id = s.page_id
                {where} ORDER BY d.id
                """,
                args,
            )
        ]
        # 크롤 회차 — 페이지 큐(crawl_pages)를 내장. 스냅샷 참조는
        # (page_url, dir_name)으로 직렬화해 가져올 때 새 id 로 잇는다.
        # 로컬 네트워크 태그·클레임 상태는 인스턴스 로컬이라 내보내지 않는다.
        crawls = []
        for c in conn.execute(
            f"SELECT * FROM crawls c{_site_where('c')} ORDER BY c.id", args
        ).fetchall():
            crawl_pages = [
                {
                    "url": r["url"], "depth": r["depth"], "status": r["status"],
                    "attempts": r["attempts"], "error": r["error"],
                    "page_url": r["snap_page_url"], "dir_name": r["dir_name"],
                }
                for r in conn.execute(
                    """
                    SELECT cp.*, s.dir_name, p.url AS snap_page_url
                    FROM crawl_pages cp
                    LEFT JOIN snapshots s ON s.id = cp.snapshot_id
                    LEFT JOIN pages p ON p.id = s.page_id
                    WHERE cp.crawl_id = ? ORDER BY cp.id
                    """,
                    (c["id"],),
                )
            ]
            crawls.append({
                **{
                    k: c[k]
                    for k in (
                        "start_url", "scope_host", "scope_path", "status",
                        "max_pages", "max_depth", "delay_seconds", "source",
                        "created_at", "finished_at",
                    )
                },
                "pages": crawl_pages,
            })
        # 사이트 인증서 — 소속 사이트는 site_key 로 직렬화한다
        certificates = [
            {
                k: r[k]
                for k in (
                    "site_key", "host", "fingerprint", "subject", "issuer",
                    "serial", "san", "not_before", "not_after",
                    "signature_algorithm", "verified", "pem",
                    "first_seen_at", "last_seen_at",
                )
            }
            for r in conn.execute(
                f"""
                SELECT sc.*, st.site_key
                FROM site_certificates sc JOIN sites st ON st.id = sc.site_id
                {_site_where("sc")} ORDER BY sc.id
                """,
                args,
            )
        ]
        # 아카이브 로그 — page_id/snapshot_id FK 는 (page_url, dir_name)으로
        # 직렬화. 사이트 한정이면 소속 페이지의 로그만 — 페이지 행이 생기기
        # 전에 실패한 로그(page_id NULL)는 소속을 알 수 없어 빠진다
        # (list_site_failed_logs 와 같은 기준).
        log_rows = [
            {
                **{
                    k: r[k]
                    for k in (
                        "url", "domain", "source", "status", "started_at",
                        "duration_ms", "http_status", "content_hash",
                        "error", "steps",
                    )
                },
                "page_url": r["page_url"], "dir_name": r["dir_name"],
            }
            for r in conn.execute(
                f"""
                SELECT al.*, p.url AS page_url, s.dir_name
                FROM archive_logs al
                {"JOIN" if site_id is not None else "LEFT JOIN"} pages p
                    ON p.id = al.page_id
                LEFT JOIN snapshots s ON s.id = al.snapshot_id
                {_site_where("p")} ORDER BY al.id
                """,
                args,
            )
        ]
        # 사이트 한정이면 내보내는 스냅샷이 참조하는 공유 자원만 추린다
        resource_names = None if site_id is None else [
            r["name"]
            for r in conn.execute(
                """
                SELECT DISTINCT sr.name
                FROM snapshot_resources sr
                JOIN snapshots s ON s.id = sr.snapshot_id
                JOIN pages p ON p.id = s.page_id
                WHERE p.site_id = ? ORDER BY sr.name
                """,
                (site_id,),
            )
        ]

    out = _resolve_dest(dest, prefix, ext=EXPORT_SUFFIX)
    counts = {"pages": len(pages), "snapshots": len(snapshots), "checks": len(checks)}
    manifest = {
        "kind": "archive",
        "format_version": FORMAT_VERSION,
        "created_at": _utcnow(),
        "counts": counts,
    }
    data = {
        "pages": pages, "snapshots": snapshots, "checks": checks,
        "documents": doc_rows,
        "crawls": crawls, "certificates": certificates, "logs": log_rows,
    }
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (tmp / ARCHIVE_DATA_NAME).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        with tarfile.open(out, "w:gz", compresslevel=_BACKUP_COMPRESSLEVEL) as tar:
            tar.add(tmp / MANIFEST_NAME, arcname=MANIFEST_NAME)
            tar.add(tmp / ARCHIVE_DATA_NAME, arcname=ARCHIVE_DATA_NAME)
            for s in snapshots:
                snap_dir = storage.page_dir(s["domain"], s["slug"]) / s["dir_name"]
                if snap_dir.is_dir():
                    tar.add(
                        snap_dir,
                        arcname=f"sites/{s['domain']}/{s['slug']}/{s['dir_name']}",
                    )
            if resource_names is None:
                # 모든 스냅샷을 내보내므로 공유 자원·문서 CAS 도 전량 포함한다
                if config.RESOURCES_DIR.is_dir():
                    tar.add(config.RESOURCES_DIR, arcname="resources")
                if config.DOCUMENTS_DIR.is_dir():
                    tar.add(config.DOCUMENTS_DIR, arcname="documents")
            else:
                # 사이트 한정 — 소속 스냅샷이 참조하는 CAS 파일만 담는다
                for name in resource_names:
                    if not resources.is_valid_name(name):
                        continue
                    f = resources.resource_path(name)
                    if f.is_file():
                        tar.add(f, arcname=f"resources/{name[:2]}/{name}")
                doc_names = sorted({
                    n
                    for n in (
                        documents.cas_name(d["sha256"], d["file"]) for d in doc_rows
                    )
                    if n is not None
                })
                for name in doc_names:
                    f = documents.cas_path(name)
                    if f.is_file():
                        tar.add(f, arcname=f"documents/{name[:2]}/{name}")
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
    # crawls/certificates/logs 는 v4 부터 — 파일 경로를 만들지 않는 DB 전용
    # 데이터라 필수 키만 확인한다 (없으면 구버전, 빈 목록 취급)
    for c in data.get("crawls") or []:
        if not (c.get("start_url") and c.get("created_at") and c.get("status")):
            raise ValueError(f"크롤 회차에 필수 값이 없습니다: {c.get('start_url')!r}")
        if not isinstance(c.get("pages") or [], list):
            raise ValueError(f"크롤 페이지 목록이 잘못됐습니다: {c['start_url']!r}")
    for cert in data.get("certificates") or []:
        if not (
            _DOMAIN_RE.match(str(cert.get("site_key", "")))
            and cert.get("host") and cert.get("fingerprint") and cert.get("pem")
        ):
            raise ValueError(f"인증서 항목이 잘못됐습니다: {cert.get('host')!r}")
    for lg in data.get("logs") or []:
        if not (lg.get("url") and lg.get("started_at") and lg.get("status")):
            raise ValueError(f"아카이브 로그 항목에 필수 값이 없습니다: {lg.get('url')!r}")


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
    db.clear_search_index(conn)
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
      두 모드 모두 인증 테이블은 건드리지 않는다.

    크롤 회차·사이트 인증서·아카이브 로그(v4)도 각자의 자연키로 중복을
    스킵하며 가져온다 — 기존 archive_logs 행은 지우지 않는다.
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
                        INSERT INTO pages
                            (url, domain, slug, site_id, client_captured, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (p["url"], p["domain"], p["slug"], site_id,
                         int(p.get("client_captured", 0)), p["created_at"] or _utcnow()),
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
                snap_id = db.insert_snapshot(
                    conn, page_id,
                    taken_at=s["taken_at"], dir_name=s["dir_name"],
                    content_hash=s["content_hash"], final_url=s["final_url"],
                    http_status=s.get("http_status"),
                    changed=int(s.get("changed", 1)), note=s.get("note"),
                    origin=s.get("origin", "server"),
                    incomplete=int(s.get("incomplete", 0)),
                    bytes=int(s.get("bytes", 0)),
                    title=s.get("title"),
                )
                snap_ids[(s["page_url"], s["dir_name"])] = snap_id
                result.snapshots_added += 1

                domain, slug = page_paths[s["page_url"]]
                src_dir = tmp / "sites" / s["domain"] / s["slug"] / s["dir_name"]
                dst_dir = storage.page_dir(domain, slug) / s["dir_name"]
                if src_dir.is_dir() and not dst_dir.exists():
                    dst_dir.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(src_dir, dst_dir)
                # 옮긴 실제 파일 기준으로 bytes 를 권위적으로 다시 맞춘다 — 구버전
                # 내보내기(bytes 없음)나 직렬화 값과 파일의 불일치를 모두 흡수한다.
                db.update_snapshot_bytes(
                    conn, snap_id, storage.snapshot_dir_bytes(dst_dir)
                )

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

            # 크롤 회차 — (start_url, created_at)이 같으면 스킵 (멱등).
            # 클레임 상태(in_progress)는 인스턴스 로컬이라 pending 으로 되돌리고,
            # 진행 중(running)이던 회차는 가져온 쪽 크롤러가 이어서 처리한다.
            # 로컬 네트워크 태그는 인스턴스 설정이라 옮기지 않는다 — 사설 대역
            # 페이지는 게이트가 거부하므로 태그를 다시 지정해야 이어진다.
            for cr in data.get("crawls") or []:
                dup = conn.execute(
                    "SELECT 1 FROM crawls WHERE start_url = ? AND created_at = ?",
                    (cr["start_url"], cr["created_at"]),
                ).fetchone()
                if dup is not None:
                    continue
                crawl_site = db.get_or_create_site(
                    conn, storage.site_key(cr["start_url"])
                )
                cur = conn.execute(
                    """
                    INSERT INTO crawls (start_url, scope_host, scope_path,
                        status, max_pages, max_depth, delay_seconds, source,
                        site_id, created_at, finished_at, next_page_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (cr["start_url"], cr.get("scope_host") or "",
                     cr.get("scope_path") or "/", cr["status"],
                     int(cr.get("max_pages") or 0), int(cr.get("max_depth") or 0),
                     int(cr.get("delay_seconds") or 0), cr.get("source") or "web",
                     crawl_site, cr["created_at"], cr.get("finished_at"),
                     _utcnow()),
                )
                crawl_id = cur.lastrowid
                for cp in cr.get("pages") or []:
                    if not cp.get("url"):
                        continue
                    status = cp.get("status") or "pending"
                    if status == "in_progress":
                        status = "pending"
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO crawl_pages
                            (crawl_id, url, depth, status, attempts,
                             snapshot_id, error)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (crawl_id, cp["url"], int(cp.get("depth") or 0), status,
                         int(cp.get("attempts") or 0),
                         snap_ids.get((cp.get("page_url"), cp.get("dir_name"))),
                         cp.get("error")),
                    )
                result.crawls_added += 1

            # 사이트 인증서 — (site, host, 지문)이 같은 버전은 스킵 (멱등)
            for cert in data.get("certificates") or []:
                cert_site = db.get_or_create_site(conn, cert["site_key"])
                dup = conn.execute(
                    """
                    SELECT 1 FROM site_certificates
                    WHERE site_id = ? AND host = ? AND fingerprint = ?
                    """,
                    (cert_site, cert["host"], cert["fingerprint"]),
                ).fetchone()
                if dup is not None:
                    continue
                conn.execute(
                    """
                    INSERT INTO site_certificates (site_id, host, fingerprint,
                        subject, issuer, serial, san, not_before, not_after,
                        signature_algorithm, verified, pem,
                        first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (cert_site, cert["host"], cert["fingerprint"],
                     cert.get("subject") or "", cert.get("issuer") or "",
                     cert.get("serial") or "", cert.get("san") or "[]",
                     cert.get("not_before"), cert.get("not_after"),
                     cert.get("signature_algorithm"),
                     int(cert.get("verified", 1)), cert["pem"],
                     cert.get("first_seen_at") or _utcnow(),
                     cert.get("last_seen_at") or _utcnow()),
                )
                result.certificates_added += 1

            # 아카이브 로그 — (url, started_at)이 같은 행은 스킵하되, overwrite
            # 가 FK 만 비워둔 기존 행이면 가져온 참조로 되살린다 (멱등)
            for lg in data.get("logs") or []:
                page_id = page_ids.get(lg.get("page_url"))
                snapshot_id = snap_ids.get((lg.get("page_url"), lg.get("dir_name")))
                dup = conn.execute(
                    "SELECT id FROM archive_logs WHERE url = ? AND started_at = ?",
                    (lg["url"], lg["started_at"]),
                ).fetchone()
                if dup is not None:
                    conn.execute(
                        """
                        UPDATE archive_logs
                        SET page_id = COALESCE(page_id, ?),
                            snapshot_id = COALESCE(snapshot_id, ?)
                        WHERE id = ?
                        """,
                        (page_id, snapshot_id, dup["id"]),
                    )
                    continue
                db.insert_archive_log(
                    conn,
                    url=lg["url"], domain=lg.get("domain") or "",
                    page_id=page_id, snapshot_id=snapshot_id,
                    source=lg.get("source") or "cli", status=lg["status"],
                    started_at=lg["started_at"],
                    duration_ms=int(lg.get("duration_ms") or 0),
                    http_status=lg.get("http_status"),
                    content_hash=lg.get("content_hash"),
                    error=lg.get("error"), steps=lg.get("steps"),
                )
                result.logs_added += 1
    return result
