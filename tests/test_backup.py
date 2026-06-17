"""백업/복원(backup.py) 테스트. 두 아카이브 루트를 전환하며 검증."""
import hashlib
import json
import tarfile

import pytest
from click.testing import CliRunner

from chunchugwan import backup, cli, config, db, documents, storage

URL_A = "https://example.com/post"
URL_B = "https://other.org/page"

DOC_BODY = b"%PDF-1.4 backup fixture"
DOC_SHA = hashlib.sha256(DOC_BODY).hexdigest()
DOC_FILE = "report-12345678.pdf"


def _patch_root(monkeypatch, root):
    """config 의 경로 전역을 임시 루트로 전환."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", root)
    monkeypatch.setattr(config, "SITES_DIR", root / "sites")
    monkeypatch.setattr(config, "DB_PATH", root / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", root / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", root / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", root / "documents")
    monkeypatch.setattr(config, "RULES_PATH", root / "rules.json")


def _seed_page(url: str, dir_names: list[str], with_check: bool = False) -> None:
    """현재 루트에 페이지 1개 + 스냅샷(dir_names 개수만큼) + 파일 구성."""
    domain = url.split("/")[2]
    slug = storage.url_to_slug(url)
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, url, domain, slug)
        for i, dir_name in enumerate(dir_names):
            snap_dir = storage.page_dir(domain, slug) / dir_name
            snap_dir.mkdir(parents=True)
            text = f"{url} 본문 {i}"
            (snap_dir / "content.md").write_text(text, encoding="utf-8")
            db.insert_snapshot(
                conn, page_id,
                taken_at=dir_name[:10] + "T00:00:00+00:00",
                dir_name=dir_name, content_hash=storage.content_sha256(text),
                final_url=url, http_status=200, changed=1,
            )
        if with_check:
            db.insert_check(conn, page_id, "deadbeef")


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """루트 A 에 데이터(페이지 2개·사용자·룰·문서 CAS)를 구성하고 (A, B) 경로 반환."""
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    _patch_root(monkeypatch, root_a)
    _seed_page(URL_A, ["2026-06-01T00-00-00", "2026-06-02T00-00-00"], with_check=True)
    _seed_page(URL_B, ["2026-06-03T00-00-00"])
    with db.connect() as conn:
        db.create_user(conn, "admin@example.com", password_hash="x", role="admin")
        # URL_A 첫 스냅샷(id=1)에 문서 CAS 파일 + 참조 행
        db.insert_snapshot_documents(conn, 1, [{
            "url": "https://example.com/files/report.pdf", "file": DOC_FILE,
            "bytes": len(DOC_BODY), "sha256": DOC_SHA,
            "content_type": "application/pdf",
        }])
    cas = documents.cas_path(DOC_SHA + ".pdf")
    cas.parent.mkdir(parents=True)
    cas.write_bytes(DOC_BODY)
    config.RULES_PATH.write_text('{"example.com": {}}', encoding="utf-8")
    return root_a, root_b


def _counts() -> dict:
    with db.connect() as conn:
        return {
            t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            for t in ("pages", "snapshots", "checks", "users", "archive_logs")
        }


# ---- 전체 백업/복원 ----


def test_backup_restore_roundtrip(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    out = backup.create_backup(tmp_path)
    assert out.name.startswith("chunchugwan-backup-")
    assert out.name.endswith(".ccg.backup")

    _patch_root(monkeypatch, root_b)
    backup.restore_backup(out)

    assert _counts() == {"pages": 2, "snapshots": 3, "checks": 1, "users": 1, "archive_logs": 0}
    slug = storage.url_to_slug(URL_A)
    content = storage.page_dir("example.com", slug) / "2026-06-01T00-00-00" / "content.md"
    assert content.read_text(encoding="utf-8") == f"{URL_A} 본문 0"
    assert json.loads(config.RULES_PATH.read_text(encoding="utf-8")) == {"example.com": {}}


def test_backup_restore_includes_documents(roots, tmp_path, monkeypatch):
    """전체 백업/복원에 문서 CAS 와 snapshot_documents 행이 포함된다."""
    root_a, root_b = roots
    out = backup.create_backup(tmp_path / "full.tar.gz")

    _patch_root(monkeypatch, root_b)
    backup.restore_backup(out)

    assert documents.cas_path(DOC_SHA + ".pdf").read_bytes() == DOC_BODY
    with db.connect() as conn:
        row = db.get_snapshot_document(conn, 1, DOC_FILE)
    assert row is not None and row["sha256"] == DOC_SHA


def test_export_import_includes_documents(roots, tmp_path, monkeypatch):
    """내보내기/가져오기에 문서 CAS 와 참조 행이 따라온다 (merge 멱등)."""
    root_a, root_b = roots
    out = backup.export_archive(tmp_path / "e.tar.gz")

    _patch_root(monkeypatch, root_b)
    backup.import_archive(out, mode="merge")
    assert documents.cas_path(DOC_SHA + ".pdf").read_bytes() == DOC_BODY
    with db.connect() as conn:
        snap = db.get_page(conn, URL_A)
        assert snap is not None
        row = conn.execute("SELECT * FROM snapshot_documents").fetchone()
    assert row is not None and row["sha256"] == DOC_SHA and row["file"] == DOC_FILE

    backup.import_archive(out, mode="merge")  # 멱등 — 행이 늘지 않는다
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM snapshot_documents").fetchone()["c"]
    assert n == 1


def test_export_import_sets_snapshot_bytes(roots, tmp_path, monkeypatch):
    """가져오기가 옮긴 실제 파일 기준으로 snapshots.bytes 를 채운다 (집계 일관성)."""
    root_a, root_b = roots
    out = backup.export_archive(tmp_path / "e.tar.gz")

    _patch_root(monkeypatch, root_b)
    backup.import_archive(out, mode="merge")
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT s.bytes, p.domain, p.slug, s.dir_name "
            "FROM snapshots s JOIN pages p ON p.id = s.page_id"
        ).fetchall()
    assert rows
    for r in rows:
        snap_dir = storage.page_dir(r["domain"], r["slug"]) / r["dir_name"]
        actual = storage.snapshot_dir_bytes(snap_dir)
        assert actual > 0
        assert r["bytes"] == actual


def test_export_import_preserves_title(roots, tmp_path, monkeypatch):
    """비정규화된 스냅샷 title 이 내보내기/가져오기로 라운드트립된다."""
    root_a, root_b = roots
    with db.connect() as conn:
        conn.execute(
            "UPDATE snapshots SET title = ? WHERE dir_name = ?",
            ("내보낸 제목", "2026-06-01T00-00-00"),
        )
        conn.commit()
    out = backup.export_archive(tmp_path / "e.tar.gz")

    _patch_root(monkeypatch, root_b)
    backup.import_archive(out, mode="merge")
    with db.connect() as conn:
        title = conn.execute(
            "SELECT title FROM snapshots WHERE dir_name = ?", ("2026-06-01T00-00-00",)
        ).fetchone()["title"]
    assert title == "내보낸 제목"


def test_export_import_preserves_provenance(roots, tmp_path, monkeypatch):
    """확장 캡처 출처(origin/incomplete)와 client_captured 표식이 라운드트립된다."""
    root_a, root_b = roots
    with db.connect() as conn:
        conn.execute(
            "UPDATE snapshots SET origin = 'extension', incomplete = 1 WHERE dir_name = ?",
            ("2026-06-02T00-00-00",),
        )
        conn.execute("UPDATE pages SET client_captured = 1 WHERE url = ?", (URL_A,))
        conn.commit()
    out = backup.export_archive(tmp_path / "e.tar.gz")

    _patch_root(monkeypatch, root_b)
    backup.import_archive(out, mode="merge")
    with db.connect() as conn:
        assert db.get_page(conn, URL_A)["client_captured"] == 1
        ext = conn.execute(
            "SELECT origin, incomplete FROM snapshots WHERE dir_name = ?",
            ("2026-06-02T00-00-00",),
        ).fetchone()
        assert ext["origin"] == "extension" and ext["incomplete"] == 1
        srv = conn.execute(
            "SELECT origin, incomplete FROM snapshots WHERE dir_name = ?",
            ("2026-06-01T00-00-00",),
        ).fetchone()
        assert srv["origin"] == "server" and srv["incomplete"] == 0


def test_export_site_only(roots, tmp_path, monkeypatch):
    """site_id 한정 내보내기 — 소속 페이지·스냅샷·참조 CAS 만 담긴다."""
    root_a, root_b = roots
    used, unused = "a" * 64 + ".png", "b" * 64 + ".png"
    for name in (used, unused):
        f = config.RESOURCES_DIR / name[:2] / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(name.encode())
    with db.connect() as conn:
        site_id = db.get_site_by_key(conn, storage.site_key(URL_A))["id"]
        db.insert_snapshot_resources(conn, 1, [{"name": used}])  # URL_A 스냅샷
        db.insert_snapshot_resources(conn, 3, [{"name": unused}])  # URL_B 스냅샷

    out = backup.export_archive(tmp_path, site_id=site_id)
    assert out.name.startswith("chunchugwan-export-example.com-")
    assert out.name.endswith(".ccg.export")
    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert any(n.startswith("sites/example.com/") for n in names)
    assert not any("other.org" in n for n in names)
    assert f"resources/{used[:2]}/{used}" in names
    assert f"resources/{unused[:2]}/{unused}" not in names
    assert f"documents/{DOC_SHA[:2]}/{DOC_SHA}.pdf" in names

    # 가져오면 해당 사이트만 복원된다
    _patch_root(monkeypatch, root_b)
    backup.import_archive(out, mode="merge")
    with db.connect() as conn:
        assert db.get_page(conn, URL_A) is not None
        assert db.get_page(conn, URL_B) is None
    assert _counts() == {
        "pages": 1, "snapshots": 2, "checks": 1, "users": 0, "archive_logs": 0,
    }
    assert documents.cas_path(DOC_SHA + ".pdf").read_bytes() == DOC_BODY
    assert (config.RESOURCES_DIR / used[:2] / used).read_bytes() == used.encode()


def test_export_unknown_site_raises(roots, tmp_path):
    with pytest.raises(ValueError, match="사이트 없음"):
        backup.export_archive(tmp_path / "x.tar.gz", site_id=9999)


def _seed_crawl(conn, url: str, site_id: int) -> int:
    """완료된 크롤 회차 1개 삽입 후 id 반환 (테스트 픽스처)."""
    cur = conn.execute(
        """
        INSERT INTO crawls (start_url, scope_host, scope_path, status,
            max_pages, max_depth, delay_seconds, source, site_id,
            created_at, finished_at, next_page_at)
        VALUES (?, ?, '/', 'done', 30, 2, 10, 'web', ?,
                '2026-06-01T01:00:00+00:00', '2026-06-01T02:00:00+00:00',
                '2026-06-01T02:00:00+00:00')
        """,
        (url, url.split("/")[2], site_id),
    )
    return cur.lastrowid


def _seed_certificate(conn, site_id: int, host: str) -> None:
    conn.execute(
        """
        INSERT INTO site_certificates (site_id, host, fingerprint, subject,
            issuer, serial, san, verified, pem, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, 'CN=' || ?, 'CN=ca', '01', '[]', 1, '-----PEM-----',
                '2026-06-01T00:00:00+00:00', '2026-06-02T00:00:00+00:00')
        """,
        (site_id, host, "f" * 64, host),
    )


def test_export_import_carries_crawls_certs_logs(roots, tmp_path, monkeypatch):
    """v4 — 크롤 회차·사이트 인증서·아카이브 로그가 함께 옮겨진다 (사이트 한정 포함)."""
    root_a, root_b = roots
    with db.connect() as conn:
        site_a = db.get_site_by_key(conn, storage.site_key(URL_A))["id"]
        site_b = db.get_site_by_key(conn, storage.site_key(URL_B))["id"]
        crawl_a = _seed_crawl(conn, URL_A, site_a)
        _seed_crawl(conn, URL_B, site_b)
        # 크롤 페이지: 스냅샷 참조가 있는 done + 클레임 중이던 in_progress
        conn.execute(
            "INSERT INTO crawl_pages (crawl_id, url, status, snapshot_id) "
            "VALUES (?, ?, 'done', 1)",
            (crawl_a, URL_A),
        )
        conn.execute(
            "INSERT INTO crawl_pages (crawl_id, url, depth, status, attempts) "
            "VALUES (?, ?, 1, 'in_progress', 2)",
            (crawl_a, URL_A + "/sub"),
        )
        _seed_certificate(conn, site_a, "example.com")
        _seed_certificate(conn, site_b, "other.org")
        db.insert_archive_log(
            conn, url=URL_A, domain="example.com", page_id=1, snapshot_id=1,
            source="cli", status="new", started_at="2026-06-01T00:00:00+00:00",
        )
        db.insert_archive_log(
            conn, url=URL_B, domain="other.org", page_id=2, snapshot_id=3,
            source="cli", status="new", started_at="2026-06-03T00:00:00+00:00",
        )

    out = backup.export_archive(tmp_path / "site.tar.gz", site_id=site_a)
    _patch_root(monkeypatch, root_b)
    result = backup.import_archive(out, mode="merge")
    assert (result.crawls_added, result.certificates_added, result.logs_added) == (1, 1, 1)

    with db.connect() as conn:
        crawls = conn.execute("SELECT * FROM crawls").fetchall()
        assert len(crawls) == 1 and crawls[0]["start_url"] == URL_A
        assert crawls[0]["status"] == "done"
        snap_id = conn.execute(
            "SELECT id FROM snapshots WHERE dir_name = '2026-06-01T00-00-00'"
        ).fetchone()["id"]
        cps = conn.execute("SELECT * FROM crawl_pages ORDER BY id").fetchall()
        assert len(cps) == 2
        assert cps[0]["snapshot_id"] == snap_id  # 새 id 로 다시 연결
        assert cps[1]["status"] == "pending"  # 클레임 상태는 옮기지 않는다
        certs = conn.execute("SELECT * FROM site_certificates").fetchall()
        assert len(certs) == 1 and certs[0]["host"] == "example.com"
        logs = conn.execute("SELECT * FROM archive_logs").fetchall()
        assert len(logs) == 1 and logs[0]["url"] == URL_A
        assert logs[0]["page_id"] == db.get_page(conn, URL_A)["id"]
        assert logs[0]["snapshot_id"] == snap_id

    # 멱등 — 다시 가져와도 늘지 않는다
    again = backup.import_archive(out, mode="merge")
    assert (again.crawls_added, again.certificates_added, again.logs_added) == (0, 0, 0)
    with db.connect() as conn:
        for table, n in (("crawls", 1), ("crawl_pages", 2),
                         ("site_certificates", 1), ("archive_logs", 1)):
            assert conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"] == n


def test_import_overwrite_relinks_kept_logs(roots, tmp_path):
    """overwrite 가 FK 만 비워둔 기존 로그 행이 가져오기에서 다시 연결된다."""
    with db.connect() as conn:
        db.insert_archive_log(
            conn, url=URL_A, domain="example.com", page_id=1, snapshot_id=1,
            source="cli", status="new", started_at="2026-06-01T00:00:00+00:00",
        )
    out = backup.export_archive(tmp_path / "e.tar.gz")
    result = backup.import_archive(out, mode="overwrite")  # 같은 루트에 덮어쓰기
    assert result.logs_added == 0  # 행은 보존돼 있었다
    with db.connect() as conn:
        logs = conn.execute("SELECT * FROM archive_logs").fetchall()
        page = db.get_page(conn, URL_A)
        assert len(logs) == 1
        assert logs[0]["page_id"] == page["id"]
        assert logs[0]["snapshot_id"] is not None


def test_import_rejects_bad_document_refs(roots, tmp_path, monkeypatch):
    """archive.json 의 문서 참조가 형식 위반이면 거부 (path traversal)."""
    root_a, root_b = roots
    evil = tmp_path / "evil-doc.tar.gz"
    manifest = {"kind": "archive", "format_version": 3, "created_at": "x", "counts": {}}
    data = {
        "pages": [], "snapshots": [], "checks": [],
        "documents": [{
            "page_url": URL_A, "dir_name": "2026-06-01T00-00-00",
            "url": "x", "file": "../evil.pdf", "bytes": 1,
            "sha256": DOC_SHA, "content_type": "application/pdf",
        }],
    }
    src = tmp_path / "payload-doc"
    src.mkdir()
    (src / backup.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    (src / backup.ARCHIVE_DATA_NAME).write_text(json.dumps(data), encoding="utf-8")
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(src / backup.MANIFEST_NAME, arcname=backup.MANIFEST_NAME)
        tar.add(src / backup.ARCHIVE_DATA_NAME, arcname=backup.ARCHIVE_DATA_NAME)
    _patch_root(monkeypatch, root_b)
    with pytest.raises(ValueError, match="잘못된 문서 참조"):
        backup.import_archive(evil)


def test_restore_replaces_existing_data(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    out = backup.create_backup(tmp_path / "full.tar.gz")

    _patch_root(monkeypatch, root_b)
    _seed_page("https://gone.example/x", ["2026-06-09T00-00-00"])
    backup.restore_backup(out)

    with db.connect() as conn:
        assert db.get_page(conn, "https://gone.example/x") is None
        assert db.get_page(conn, URL_A) is not None
    assert not (config.SITES_DIR / "gone.example").exists()


def test_restore_rejects_export_file(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    out = backup.export_archive(tmp_path / "export.tar.gz")
    _patch_root(monkeypatch, root_b)
    with pytest.raises(ValueError, match="전체 백업 파일이 아닙니다"):
        backup.restore_backup(out)


def test_read_manifest_rejects_foreign_tar(tmp_path):
    foreign = tmp_path / "foreign.tar.gz"
    payload = tmp_path / "x.txt"
    payload.write_text("x")
    with tarfile.open(foreign, "w:gz") as tar:
        tar.add(payload, arcname="x.txt")
    with pytest.raises(ValueError, match="춘추관 백업 파일이 아닙니다"):
        backup.read_manifest(foreign)


def test_restore_clears_stale_wal_files(roots, tmp_path, monkeypatch):
    """복원이 이전 DB 의 WAL 잔재(-wal/-shm)를 함께 지운다 — 남으면 새 DB 손상."""
    root_a, root_b = roots
    out = backup.create_backup(tmp_path / "bk.tar.gz")

    _patch_root(monkeypatch, root_b)
    config.ensure_dirs()
    (root_b / "index.db-wal").write_bytes(b"stale")
    (root_b / "index.db-shm").write_bytes(b"stale")
    backup.restore_backup(out)

    assert not (root_b / "index.db-wal").exists()
    assert not (root_b / "index.db-shm").exists()
    assert _counts()["pages"] == 2


# ---- 아카이브 내보내기/가져오기 ----


def test_export_import_into_empty_root(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    out = backup.export_archive(tmp_path)
    assert out.name.startswith("chunchugwan-export-")
    assert out.name.endswith(".ccg.export")

    _patch_root(monkeypatch, root_b)
    with db.connect() as conn:
        db.create_user(conn, "b@example.com", password_hash="y")
    result = backup.import_archive(out, mode="merge")

    assert (result.pages_added, result.snapshots_added, result.checks_added) == (2, 3, 1)
    assert result.snapshots_skipped == 0
    # 인증 데이터는 export 에 포함되지 않고, 기존 사용자도 유지
    counts = _counts()
    assert counts["users"] == 1
    assert counts["pages"] == 2 and counts["snapshots"] == 3
    slug = storage.url_to_slug(URL_B)
    content = storage.page_dir("other.org", slug) / "2026-06-03T00-00-00" / "content.md"
    assert content.read_text(encoding="utf-8") == f"{URL_B} 본문 0"


def test_import_merge_is_idempotent(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    out = backup.export_archive(tmp_path / "e.tar.gz")
    _patch_root(monkeypatch, root_b)
    backup.import_archive(out, mode="merge")
    result = backup.import_archive(out, mode="merge")
    assert (result.pages_added, result.snapshots_added, result.checks_added) == (0, 0, 0)
    assert result.snapshots_skipped == 3
    assert _counts()["snapshots"] == 3


def test_import_merge_keeps_existing_pages(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    out = backup.export_archive(tmp_path / "e.tar.gz")
    _patch_root(monkeypatch, root_b)
    _seed_page("https://keep.example/y", ["2026-06-08T00-00-00"])
    backup.import_archive(out, mode="merge")
    with db.connect() as conn:
        assert db.get_page(conn, "https://keep.example/y") is not None
        assert db.get_page(conn, URL_A) is not None
    assert _counts()["pages"] == 3


def test_import_overwrite_replaces_archive_keeps_auth_and_logs(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    out = backup.export_archive(tmp_path / "e.tar.gz")

    _patch_root(monkeypatch, root_b)
    _seed_page("https://gone.example/x", ["2026-06-09T00-00-00"])
    with db.connect() as conn:
        db.create_user(conn, "b@example.com", password_hash="y")
        page = db.get_page(conn, "https://gone.example/x")
        db.insert_archive_log(
            conn, url="https://gone.example/x", domain="gone.example",
            page_id=page["id"], source="cli", status="new",
            started_at="2026-06-09T00:00:00+00:00",
        )
    backup.import_archive(out, mode="overwrite")

    counts = _counts()
    assert counts == {"pages": 2, "snapshots": 3, "checks": 1, "users": 1, "archive_logs": 1}
    with db.connect() as conn:
        assert db.get_page(conn, "https://gone.example/x") is None
        log = conn.execute("SELECT * FROM archive_logs").fetchone()
        assert log["page_id"] is None  # FK 만 비우고 행은 유지
    assert not (config.SITES_DIR / "gone.example").exists()


def test_import_rejects_full_backup(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    out = backup.create_backup(tmp_path / "full.tar.gz")
    _patch_root(monkeypatch, root_b)
    with pytest.raises(ValueError, match="아카이브 내보내기 파일이 아닙니다"):
        backup.import_archive(out)


def test_import_rejects_path_traversal_components(roots, tmp_path, monkeypatch):
    """archive.json 의 slug 에 경로 탈출 문자가 있으면 거부."""
    root_a, root_b = roots
    evil = tmp_path / "evil.tar.gz"
    manifest = {"kind": "archive", "format_version": 1, "created_at": "x", "counts": {}}
    data = {
        "pages": [{"url": URL_A, "domain": "example.com", "slug": "../evil", "created_at": "x"}],
        "snapshots": [], "checks": [],
    }
    src = tmp_path / "payload"
    src.mkdir()
    (src / backup.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    (src / backup.ARCHIVE_DATA_NAME).write_text(json.dumps(data), encoding="utf-8")
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(src / backup.MANIFEST_NAME, arcname=backup.MANIFEST_NAME)
        tar.add(src / backup.ARCHIVE_DATA_NAME, arcname=backup.ARCHIVE_DATA_NAME)

    _patch_root(monkeypatch, root_b)
    with pytest.raises(ValueError, match="잘못된 페이지 경로 구성요소"):
        backup.import_archive(evil)
    assert _counts()["pages"] == 0


# ---- CLI ----


def test_cli_backup_and_restore(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    runner = CliRunner()
    out = tmp_path / "out.ccg.backup"
    result = runner.invoke(cli.main, ["backup", str(out)])
    assert result.exit_code == 0, result.output
    assert "백업 생성" in result.output

    _patch_root(monkeypatch, root_b)
    # 확인 프롬프트에서 거부하면 중단
    result = runner.invoke(cli.main, ["restore", str(out)], input="n\n")
    assert result.exit_code != 0
    assert _counts()["pages"] == 0

    result = runner.invoke(cli.main, ["restore", str(out), "--yes"])
    assert result.exit_code == 0, result.output
    assert "복원 완료" in result.output
    assert _counts()["pages"] == 2


def test_cli_restore_rejects_non_backup_extension(roots, tmp_path):
    """복원은 .ccg.backup 확장자가 아니면 거부한다."""
    out = backup.create_backup(tmp_path)  # 기본 .ccg.backup 파일
    wrong = out.rename(tmp_path / "renamed.tar.gz")
    result = CliRunner().invoke(cli.main, ["restore", str(wrong), "--yes"])
    assert result.exit_code != 0
    assert ".ccg.backup" in result.output


def test_cli_export_and_import(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    runner = CliRunner()
    src = tmp_path / "e.ccg.export"
    result = runner.invoke(cli.main, ["export", str(src)])
    assert result.exit_code == 0, result.output

    _patch_root(monkeypatch, root_b)
    result = runner.invoke(cli.main, ["import", str(src)])
    assert result.exit_code == 0, result.output
    assert "페이지 +2" in result.output

    # overwrite 는 --yes 없이 프롬프트, 거부 시 중단
    result = runner.invoke(
        cli.main, ["import", str(src), "--mode", "overwrite"], input="n\n"
    )
    assert result.exit_code != 0

    result = runner.invoke(
        cli.main, ["import", str(src), "--mode", "overwrite", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "스킵 0" in result.output  # overwrite 후라 전부 새로 들어감


def test_cli_import_rejects_non_export_extension(roots, tmp_path):
    """가져오기는 .ccg.export 확장자가 아니면 거부한다."""
    src = backup.export_archive(tmp_path)  # 기본 .ccg.export 파일
    wrong = src.rename(tmp_path / "renamed.tar.gz")
    result = CliRunner().invoke(cli.main, ["import", str(wrong)])
    assert result.exit_code != 0
    assert ".ccg.export" in result.output


def test_cli_restore_rejects_export_file(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    # 확장자 게이트는 통과시키되(.ccg.backup) 내용이 내보내기(kind=archive)라 거부
    out = backup.export_archive(tmp_path / "e.ccg.backup")
    _patch_root(monkeypatch, root_b)
    result = CliRunner().invoke(cli.main, ["restore", str(out), "--yes"])
    assert result.exit_code != 0
    assert "wccg import" in result.output


# ---- 공유 자원 (resources/) 포함 ----


def test_backup_and_export_include_resources(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    name = "a" * 64 + ".png"
    src = config.RESOURCES_DIR / name[:2] / name
    src.parent.mkdir(parents=True)
    src.write_bytes(b"resource-bytes")

    full = backup.create_backup(tmp_path / "full.tar.gz")
    exported = backup.export_archive(tmp_path / "export.tar.gz")

    # 전체 복원 — 공유 자원이 그대로 돌아온다
    _patch_root(monkeypatch, root_b)
    backup.restore_backup(full)
    assert (config.RESOURCES_DIR / name[:2] / name).read_bytes() == b"resource-bytes"

    # 가져오기(merge) — 빈 루트에 공유 자원 포함 복원
    _patch_root(monkeypatch, tmp_path / "c")
    backup.import_archive(exported, mode="merge")
    assert (config.RESOURCES_DIR / name[:2] / name).read_bytes() == b"resource-bytes"
