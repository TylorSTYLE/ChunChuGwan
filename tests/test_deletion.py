"""아카이브 삭제 — DB 정합성(changed 재계산·로그 보존), 파일/캐시 정리,
문서 CAS GC, 라우트 가드."""
import hashlib

import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, deletion, differ, documents, storage
from chunchugwan.web import app as web_app

URL = "https://example.com/post"
DOMAIN = "example.com"

# 콘텐츠 시퀀스 A → B → A: 가운데(B)를 지우면 마지막 스냅샷은 '동일'이 돼야 한다
CONTENTS = ["내용 A", "내용 B", "내용 A"]


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """페이지 1개 + 스냅샷 3개(A,B,A) + check/schedule/log 가 있는 임시 아카이브."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")

    slug = storage.url_to_slug(URL)
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, URL, DOMAIN, slug)
        prev_hash = None
        for i, text in enumerate(CONTENTS):
            dir_name = f"2026-06-0{i + 1}T00-00-00"
            snap_dir = storage.page_dir(DOMAIN, slug) / dir_name
            snap_dir.mkdir(parents=True)
            (snap_dir / "content.md").write_text(text, encoding="utf-8")
            content_hash = storage.content_sha256(text)
            snap_id = db.insert_snapshot(
                conn, page_id,
                taken_at=f"2026-06-0{i + 1}T00:00:00+00:00", dir_name=dir_name,
                content_hash=content_hash, final_url=URL, http_status=200,
                changed=1 if prev_hash != content_hash else 0,
            )
            db.insert_archive_log(
                conn, url=URL, domain=DOMAIN, page_id=page_id, snapshot_id=snap_id,
                source="cli", status="new" if i == 0 else "changed",
                started_at=f"2026-06-0{i + 1}T00:00:00+00:00",
            )
            prev_hash = content_hash
        db.insert_check(conn, page_id, prev_hash)
        db.upsert_schedule(conn, page_id, 3600, "2026-06-10T00:00:00+00:00")
    return {"page_id": page_id, "slug": slug, "tmp_path": tmp_path}


def _snaps(page_id: int):
    with db.connect() as conn:
        return db.list_snapshots(conn, page_id)


# ---- 단일 스냅샷 삭제 ----


def test_delete_middle_snapshot_recomputes_changed(archive):
    """A→B→A 에서 B 삭제: 마지막 스냅샷은 새 직전(A)과 같으므로 '동일'로 보정."""
    snaps = _snaps(archive["page_id"])
    result = deletion.delete_snapshot(snaps[1]["id"])
    assert result is not None and result.snapshots_deleted == 1

    remaining = _snaps(archive["page_id"])
    assert [s["dir_name"] for s in remaining] == [
        "2026-06-01T00-00-00", "2026-06-03T00-00-00"
    ]
    assert remaining[1]["changed"] == 0  # 변경 → 동일로 재계산됨

    page_path = storage.page_dir(DOMAIN, archive["slug"])
    assert not (page_path / "2026-06-02T00-00-00").exists()
    assert (page_path / "2026-06-01T00-00-00" / "content.md").is_file()


def test_delete_first_snapshot_marks_next_changed(archive):
    """첫 스냅샷 삭제: 다음이 첫 스냅샷이 되고 changed=1."""
    snaps = _snaps(archive["page_id"])
    # 두 번째를 '동일'로 만들어 두고 (강제 저장 시나리오) 첫 번째를 지운다
    with db.connect() as conn:
        conn.execute(
            "UPDATE snapshots SET changed = 0, content_hash = ? WHERE id = ?",
            (snaps[0]["content_hash"], snaps[1]["id"]),
        )
    deletion.delete_snapshot(snaps[0]["id"])
    remaining = _snaps(archive["page_id"])
    assert remaining[0]["changed"] == 1


def test_delete_snapshot_detaches_log_but_keeps_history(archive):
    snaps = _snaps(archive["page_id"])
    deletion.delete_snapshot(snaps[1]["id"])
    with db.connect() as conn:
        logs = db.list_archive_logs(conn)
    assert len(logs) == 3  # 실행 이력은 그대로
    assert sum(1 for log in logs if log["snapshot_id"] is None) == 1


def test_delete_snapshot_purges_shotdiff_cache(archive):
    snaps = _snaps(archive["page_id"])
    sid = snaps[1]["id"]
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    keep = config.CACHE_DIR / f"shotdiff-{snaps[0]['id']}-{snaps[2]['id']}.json"
    for name in (f"shotdiff-{snaps[0]['id']}-{sid}.png",
                 f"shotdiff-{snaps[0]['id']}-{sid}.json",
                 f"shotdiff-{sid}-{snaps[2]['id']}.json"):
        (config.CACHE_DIR / name).write_text("{}")
    keep.write_text("{}")

    deletion.delete_snapshot(sid)
    assert keep.is_file()
    assert list(config.CACHE_DIR.glob(f"shotdiff-*-{sid}.*")) == []
    assert list(config.CACHE_DIR.glob(f"shotdiff-{sid}-*.*")) == []


def test_delete_snapshot_missing_id(archive):
    assert deletion.delete_snapshot(9999) is None


def test_diff_works_after_middle_deletion(archive):
    """가운데 스냅샷 삭제 후에도 남은 두 개의 diff 가 정상 동작(번호 재배열)."""
    snaps = _snaps(archive["page_id"])
    deletion.delete_snapshot(snaps[1]["id"])
    d = differ.diff_text("내용 A", "내용 A")
    assert d.identical  # 남은 1↔2 비교는 동일 — 위 재계산과 일치하는 결과


# ---- 페이지 전체 삭제 ----


def test_delete_page_removes_all_data(archive):
    page_id = archive["page_id"]
    result = deletion.delete_page(page_id, hard=True)
    assert result is not None
    assert result.url == URL and result.snapshots_deleted == 3
    assert result.trashed is False

    with db.connect() as conn:
        assert db.get_page_by_id(conn, page_id) is None
        assert db.list_snapshots(conn, page_id) == []
        assert db.list_checks(conn, page_id) == []
        assert db.get_schedule(conn, page_id) is None
        logs = db.list_archive_logs(conn)
    # 실행 로그는 이력으로 남고 참조만 해제된다
    assert len(logs) == 3
    assert all(log["page_id"] is None and log["snapshot_id"] is None for log in logs)
    # 파일도 도메인 디렉토리까지 정리
    assert not (config.SITES_DIR / DOMAIN).exists()


def test_delete_page_missing_id(archive):
    assert deletion.delete_page(9999) is None


def test_delete_path_parts_validated(archive):
    with pytest.raises(ValueError):
        storage.delete_snapshot_dir(DOMAIN, archive["slug"], "../evil")
    with pytest.raises(ValueError):
        storage.delete_page_dir("..", archive["slug"])


# ---- 문서 CAS GC ----


def _attach_document(snapshot_id: int, body: bytes, file: str) -> str:
    """스냅샷에 문서 참조 행 + CAS 파일을 붙이고 sha256 반환."""
    sha = hashlib.sha256(body).hexdigest()
    name = documents.cas_name(sha, file)
    path = documents.cas_path(name)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    with db.connect() as conn:
        db.insert_snapshot_documents(conn, snapshot_id, [{
            "url": f"https://example.com/{file}", "file": file,
            "bytes": len(body), "sha256": sha,
            "content_type": "application/pdf",
        }])
    return sha


def test_delete_snapshot_keeps_shared_document(archive):
    """다른 스냅샷이 같은 문서를 참조하면 CAS 파일은 남는다."""
    snaps = _snaps(archive["page_id"])
    body = b"%PDF shared"
    sha = _attach_document(snaps[0]["id"], body, "report-11111111.pdf")
    _attach_document(snaps[1]["id"], body, "report-11111111.pdf")

    deletion.delete_snapshot(snaps[0]["id"])
    assert documents.cas_path(sha + ".pdf").is_file()
    with db.connect() as conn:
        assert db.get_snapshot_document(conn, snaps[0]["id"], "report-11111111.pdf") is None
        assert db.get_snapshot_document(conn, snaps[1]["id"], "report-11111111.pdf") is not None

    # 마지막 참조 스냅샷까지 지우면 CAS 파일도 삭제된다
    deletion.delete_snapshot(snaps[1]["id"])
    assert not documents.cas_path(sha + ".pdf").exists()


def test_delete_page_garbage_collects_documents(archive):
    """페이지 삭제 시 그 페이지만 참조하던 문서 CAS 파일이 정리된다."""
    snaps = _snaps(archive["page_id"])
    sha_a = _attach_document(snaps[0]["id"], b"%PDF only-here", "a-22222222.pdf")
    sha_b = _attach_document(snaps[1]["id"], b"%PDF elsewhere", "b-33333333.pdf")

    # 다른 페이지의 스냅샷이 b 문서를 함께 참조한다
    with db.connect() as conn:
        other_page = db.get_or_create_page(
            conn, "https://other.com/", "other.com", "root-deadbeef"
        )
        other_snap = db.insert_snapshot(
            conn, other_page, taken_at="2026-06-05T00:00:00+00:00",
            dir_name="2026-06-05T00-00-00", content_hash="x",
            final_url="https://other.com/", http_status=200, changed=1,
        )
    _attach_document(other_snap, b"%PDF elsewhere", "b-33333333.pdf")

    deletion.delete_page(archive["page_id"], hard=True)
    assert not documents.cas_path(sha_a + ".pdf").exists()   # 참조 0 → 삭제
    assert documents.cas_path(sha_b + ".pdf").is_file()       # 잔존 참조 → 유지
    with db.connect() as conn:
        assert db.get_snapshot_document(conn, other_snap, "b-33333333.pdf") is not None


# ---- 웹 라우트 (인증 off — loopback 전부 허용) ----


@pytest.fixture
def client(archive, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    web_app._active_jobs.clear()
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def _site_url() -> str:
    """아카이브 픽스처 페이지가 속한 사이트 상세 경로."""
    with db.connect() as conn:
        site = db.get_site_by_key(conn, storage.site_key(URL))
    return f"/sites/{site['id']}"


# ---- 권한 (인증 on — 삭제는 admin/archive_manager 전용, archiver 는 삭제 불가) ----


@pytest.fixture
def role_client(archive):
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(
            conn, "manager@test.co", auth.hash_password("password1234"),
            role="archive_manager",
        )
        db.create_user(
            conn, "archiver@test.co", auth.hash_password("password1234"), role="archiver"
        )
        db.create_user(
            conn, "viewer@test.co", auth.hash_password("password1234"), role="viewer"
        )
    web_app._active_jobs.clear()
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def _login(client, email: str, password: str):
    return client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )


