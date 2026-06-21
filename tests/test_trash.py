"""아카이브 휴지통 — 소프트 삭제·복원·영구삭제·자동 purge·설정 토글·재아카이브
자동 복원·스냅샷 즉시삭제·CLI 그룹. 숨김 표면(뷰어·검색 등)은 test_trash_access.py."""
import pytest
from click.testing import CliRunner

from chunchugwan import cli, config, db, deletion, storage

URL = "https://example.com/post"
DOMAIN = "example.com"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    return tmp_path


def _seed_page(url=URL, n=2):
    dom = url.split("/")[2]
    slug = storage.url_to_slug(url)
    with db.connect() as conn:
        pid = db.get_or_create_page(conn, url, dom, slug)
        for i in range(n):
            dn = f"2026-06-0{i + 1}T00-00-00"
            d = storage.page_dir(dom, slug) / dn
            d.mkdir(parents=True)
            (d / "content.md").write_text(f"c{i}")
            db.insert_snapshot(
                conn, pid, taken_at=f"2026-06-0{i + 1}T00:00:00+00:00",
                dir_name=dn, content_hash=f"h{url}{i}", final_url=url,
                http_status=200, changed=1,
            )
    return pid


def _counts():
    """(보이는 페이지, 최근 스냅샷, 보이는 사이트, 휴지통 항목) 수."""
    with db.connect() as conn:
        return (
            db.count_pages(conn),
            len(db.list_recent_snapshots(conn, 50)),
            db.count_sites(conn),
            db.count_trash_entries(conn),
        )


# ---- 소프트 삭제 · 복원 · 영구삭제 ----


def test_delete_page_soft_then_restore(env):
    pid = _seed_page()
    res = deletion.delete_page(pid)
    assert res.trashed is True and res.snapshots_deleted == 2
    assert _counts() == (0, 0, 0, 1)  # 모든 목록에서 숨김 + 휴지통 1
    with db.connect() as conn:
        assert db.get_page_by_id(conn, pid) is None  # 기본 숨김
        assert db.get_page_by_id(conn, pid, include_trashed=True) is not None
        entry = db.list_trash_entries(conn)[0]
        assert entry["kind"] == "page" and entry["label"] == URL
    assert (config.SITES_DIR / DOMAIN).exists()  # 파일 보존
    deletion.restore(entry["id"])
    assert _counts() == (1, 2, 1, 0)


def test_delete_page_soft_then_purge(env):
    pid = _seed_page()
    deletion.delete_page(pid)
    with db.connect() as conn:
        tid = db.list_trash_entries(conn)[0]["id"]
    assert deletion.purge(tid) is not None
    assert _counts() == (0, 0, 0, 0)
    with db.connect() as conn:
        assert db.get_page_by_id(conn, pid, include_trashed=True) is None
    assert not (config.SITES_DIR / DOMAIN).exists()  # 파일까지 정리


def test_trash_disabled_deletes_immediately(env):
    pid = _seed_page()
    with db.connect() as conn:
        db.set_setting(conn, db.TRASH_ENABLED_KEY, "off")
    res = deletion.delete_page(pid)
    assert res.trashed is False
    assert _counts() == (0, 0, 0, 0)  # 휴지통 거치지 않음
    with db.connect() as conn:
        assert db.get_page_by_id(conn, pid, include_trashed=True) is None


def test_hard_flag_bypasses_trash(env):
    pid = _seed_page()
    res = deletion.delete_page(pid, hard=True)
    assert res.trashed is False
    assert _counts() == (0, 0, 0, 0)


def test_snapshot_delete_is_always_immediate(env):
    pid = _seed_page(n=2)
    with db.connect() as conn:
        snaps = db.list_snapshots(conn, pid)
    deletion.delete_snapshot(snaps[0]["id"])
    assert _counts()[3] == 0  # 휴지통 항목 안 만든다(범위 밖)
    with db.connect() as conn:
        assert len(db.list_snapshots(conn, pid)) == 1


# ---- 사이트 단위 · 크롤 스케줄 정지 ----


def test_delete_site_soft_pauses_and_restores(env):
    pid = _seed_page()
    with db.connect() as conn:
        site_id = db.get_page_by_id(conn, pid, include_trashed=True)["site_id"]
        db.upsert_crawl_schedule(
            conn, "https://example.com/", max_pages=10, max_depth=2,
            delay_seconds=5, interval_seconds=3600,
            next_run_at="2020-01-01T00:00:00+00:00",
        )
    res = deletion.delete_site(site_id)
    assert res.trashed is True and res.pages_deleted == 1
    with db.connect() as conn:
        assert db.count_sites(conn) == 0
        # 휴지통 사이트의 크롤 스케줄은 due 에서 제외 → 재크롤로 부활하지 않음
        assert db.list_due_crawl_schedules(conn, "2099-01-01T00:00:00+00:00") == []
        tid = db.list_trash_entries(conn)[0]["id"]
    deletion.restore(tid)
    with db.connect() as conn:
        assert db.count_sites(conn) == 1
        assert len(db.list_due_crawl_schedules(conn, "2099-01-01T00:00:00+00:00")) == 1


def test_purge_site_entry_removes_site_row(env):
    pid = _seed_page()
    with db.connect() as conn:
        site_id = db.get_page_by_id(conn, pid, include_trashed=True)["site_id"]
    deletion.delete_site(site_id)
    with db.connect() as conn:
        tid = db.list_trash_entries(conn)[0]["id"]
    deletion.purge(tid)
    with db.connect() as conn:
        assert db.get_site(conn, site_id) is None
        assert db.count_trash_entries(conn) == 0


# ---- 자동 purge (보관 기간) ----


def test_purge_expired_respects_retention(env):
    pid = _seed_page()
    deletion.delete_page(pid)
    with db.connect() as conn:
        db.set_setting(conn, db.TRASH_RETENTION_DAYS_KEY, "30")
        conn.execute(
            "UPDATE trash_entries SET deleted_at = '2020-01-01T00:00:00+00:00'"
        )
    assert deletion.purge_expired() == 1
    assert _counts()[3] == 0


def test_purge_expired_disabled_when_zero(env):
    pid = _seed_page()
    deletion.delete_page(pid)
    with db.connect() as conn:
        db.set_setting(conn, db.TRASH_RETENTION_DAYS_KEY, "0")
        conn.execute(
            "UPDATE trash_entries SET deleted_at = '2020-01-01T00:00:00+00:00'"
        )
    assert deletion.purge_expired() == 0
    assert _counts()[3] == 1


def test_purge_expired_keeps_recent(env):
    pid = _seed_page()
    deletion.delete_page(pid)  # 방금 삭제 — deleted_at=now
    with db.connect() as conn:
        db.set_setting(conn, db.TRASH_RETENTION_DAYS_KEY, "30")
    assert deletion.purge_expired() == 0
    assert _counts()[3] == 1


# ---- 재아카이브 자동 복원 ----


def test_rearchive_autorestores_trashed_page(env):
    pid = _seed_page()
    deletion.delete_page(pid)
    assert _counts()[3] == 1
    with db.connect() as conn:
        pid2 = db.get_or_create_page(conn, URL, DOMAIN, storage.url_to_slug(URL))
    assert pid2 == pid
    assert _counts()[3] == 0  # 항목 제거
    with db.connect() as conn:
        assert db.get_page_by_id(conn, pid) is not None  # 다시 보임


# ---- CLI 휴지통 그룹 ----


def test_cli_trash_list_restore_purge(env):
    _seed_page()
    r = CliRunner()
    out = r.invoke(cli.main, ["delete", URL, "--yes"])
    assert "휴지통으로 이동" in out.output
    lst = r.invoke(cli.main, ["trash", "list"])
    assert URL in lst.output
    rr = r.invoke(cli.main, ["trash", "restore", URL])
    assert "복원됨" in rr.output
    with db.connect() as conn:
        assert db.count_trash_entries(conn) == 0
    # 다시 삭제 후 URL 로 영구삭제
    r.invoke(cli.main, ["delete", URL, "--yes"])
    pp = r.invoke(cli.main, ["trash", "purge", URL, "--yes"])
    assert "영구 삭제됨" in pp.output
    with db.connect() as conn:
        assert db.count_trash_entries(conn) == 0
        assert db.get_page(conn, URL) is None


def test_documents_list_excludes_trashed(env):
    pid = _seed_page()
    with db.connect() as conn:
        sid = db.list_snapshots(conn, pid)[0]["id"]
        db.insert_snapshot_documents(conn, sid, [{
            "url": "https://example.com/f.pdf", "file": "f.pdf",
            "bytes": 10, "sha256": "abc123", "content_type": "application/pdf",
        }])
        assert len(db.list_document_groups(conn)) == 1
        assert db.document_totals(conn)["groups"] == 1
    deletion.delete_page(pid)
    with db.connect() as conn:
        assert db.list_document_groups(conn) == []
        assert db.document_totals(conn)["groups"] == 0


def test_export_excludes_trashed_backup_preserves(env, tmp_path):
    import json
    import tarfile

    from chunchugwan import backup

    config.ensure_dirs()
    _seed_page("https://keep.com/a")
    deletion.delete_page(_seed_page("https://gone.com/b"))
    out = backup.export_archive(tmp_path / "exp")
    with tarfile.open(out) as tar:
        data = json.loads(tar.extractfile("archive.json").read())
    urls = [p["url"] for p in data["pages"]]
    assert "https://keep.com/a" in urls
    assert not any("gone.com" in u for u in urls)  # 휴지통은 내보내기 제외
    # 전체 백업은 DB 파일째 복사 — 휴지통 보존
    assert backup.create_backup(tmp_path / "bk").exists()
    with db.connect() as conn:
        assert db.count_trash_entries(conn) == 1


def test_cli_trash_purge_all_and_expired(env):
    _seed_page("https://a.com/x")
    _seed_page("https://b.com/y")
    r = CliRunner()
    r.invoke(cli.main, ["delete", "https://a.com/x", "--yes"])
    r.invoke(cli.main, ["delete", "https://b.com/y", "--yes"])
    with db.connect() as conn:
        assert db.count_trash_entries(conn) == 2
    out = r.invoke(cli.main, ["trash", "purge", "--all", "--yes"])
    assert "2개 항목" in out.output
    with db.connect() as conn:
        assert db.count_trash_entries(conn) == 0


def test_list_trash_entries_pagination(env):
    """list_trash_entries 의 limit/offset 슬라이싱 — 휴지통 화면 페이징의 DB 계층."""
    for i in range(5):
        pid = _seed_page(url=f"https://example.com/p{i}", n=1)
        deletion.delete_page(pid)  # 휴지통으로 (trash_enabled 기본 on)
    with db.connect() as conn:
        assert db.count_trash_entries(conn) == 5
        first2 = db.list_trash_entries(conn, limit=2, offset=0)
        next2 = db.list_trash_entries(conn, limit=2, offset=2)
        assert len(first2) == 2 and len(next2) == 2
        # 최근 삭제 순으로 슬라이스 — 1·2페이지가 겹치지 않음
        assert {e["id"] for e in first2}.isdisjoint({e["id"] for e in next2})
        # limit 없으면 전체
        assert len(db.list_trash_entries(conn)) == 5
