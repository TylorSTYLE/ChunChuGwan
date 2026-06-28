"""SvelteKit SPA read API(/api/web) 테스트 — 현황·목록·타임라인·스냅샷·diff·검색·문서·스케줄·로그.

Phase C2 컷오버로 제거되는 SSR read 화면(app.dashboard/index/timeline/snapshot_view/
diff_view/search_view/documents_view/schedules_view/logs_view)의 검증 로직을 JSON API
기준으로 대체한다. SSR test_web.py·test_auth_snapshot_access.py 의 read 단정에 대응한다.
"""
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from chunchugwan import auth, config, db, storage
from chunchugwan.web import app as web_app

URL = "https://example.com/post"
AUTH_URL = "https://example.com/secret"


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """인증이 켜진 임시 아카이브 DB."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    web_app._active_jobs.clear()
    yield
    web_app._active_jobs.clear()


def make_user(email="u@test.co", password="userpass123", role="archiver"):
    """사용자 + active 세션을 만들고 (user_id, token) 반환."""
    with db.connect() as conn:
        pw = auth.hash_password(password) if password else None
        uid = db.create_user(conn, email, pw, role=role)
        token = auth.issue_session(conn, uid)
    return uid, token


def client_for(token=None):
    c = TestClient(web_app.app)
    if token:
        c.cookies.set(config.SESSION_COOKIE, token)
    return c


def seed_page(contents=("첫 줄\n둘째 줄", "첫 줄\n둘째 줄 수정됨\n셋째 줄")):
    """페이지 1 + 스냅샷 N(content.md·page.html·screenshot) + check 1 시드 → page_id."""
    domain, slug = "example.com", storage.url_to_slug(URL)
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, URL, domain, slug)
        for i, text in enumerate(contents):
            dn = f"2026-06-0{i + 1}T00-00-00"
            d = storage.page_dir(domain, slug) / dn
            d.mkdir(parents=True)
            (d / "content.md").write_text(text, encoding="utf-8")
            (d / "page.html").write_text(
                "<html><body>본문</body></html>", encoding="utf-8"
            )
            Image.new("RGB", (8, 8), (255 - i * 255,) * 3).save(d / "screenshot.png")
            db.insert_snapshot(
                conn, page_id, taken_at=f"2026-06-0{i + 1}T00:00:00+00:00",
                dir_name=dn, content_hash=storage.content_sha256(text),
                final_url=URL, http_status=200, changed=1,
            )
        db.insert_check(conn, page_id, storage.content_sha256(contents[-1]))
        db.backfill_snapshot_bytes(conn)
        db.backfill_snapshot_titles(conn)
    return page_id


def seed_auth_page(owner_id):
    """공개 스냅샷 1 + 인증 스냅샷 1(소유자=owner_id) + check 시드 → page_id."""
    domain, slug = "example.com", storage.url_to_slug(AUTH_URL)
    specs = [("publichash01", "공개", 0, None), ("secrethash99", "비밀", 1, owner_id)]
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, AUTH_URL, domain, slug)
        for i, (chash, body, authed, by) in enumerate(specs):
            dn = f"2026-06-0{i + 1}T00-00-00"
            d = storage.page_dir(domain, slug) / dn
            d.mkdir(parents=True)
            (d / "content.md").write_text(body, encoding="utf-8")
            db.insert_snapshot(
                conn, page_id, taken_at=f"2026-06-0{i + 1}T00:00:00+00:00",
                dir_name=dn, content_hash=chash, final_url=AUTH_URL,
                http_status=200, changed=1, authenticated=authed, authenticated_by=by,
            )
        db.insert_check(conn, page_id, "checkhashCC9")
    return page_id


def insert_log(status="new", *, domain="example.com", started_at="2026-06-01T00:00:00+00:00",
               error=None):
    with db.connect() as conn:
        return db.insert_archive_log(
            conn, url=f"https://{domain}/x", domain=domain, status=status,
            started_at=started_at, error=error,
        )


# ---- 인증 가드 (read 라우트 공통) ----


def test_read_routes_require_session(tmp_db):
    seed_page()
    c = client_for()  # 쿠키 없음
    for path in ("/api/web/dashboard", "/api/web/sites", "/api/web/pages/1",
                 "/api/web/documents", "/api/web/schedules"):
        assert c.get(path).status_code == 401, path


# ---- dashboard ----


def test_dashboard_totals(tmp_db):
    seed_page()
    insert_log("new")
    _, token = make_user()
    body = client_for(token).get("/api/web/dashboard").json()
    assert body["total_pages"] == 1
    assert body["total_sites"] == 1
    assert body["total_snapshots"] == 2
    assert body["total_bytes"] > 0
    assert len(body["recent_snaps"]) == 2
    assert len(body["recent_logs"]) == 1
    assert body["version"]
    assert body["trend"] and all("pct" in t for t in body["trend"])


def test_dashboard_empty(tmp_db):
    _, token = make_user()
    body = client_for(token).get("/api/web/dashboard").json()
    assert body["total_pages"] == 0 and body["total_snapshots"] == 0


# ---- sites (목록) ----


def test_sites_list(tmp_db):
    seed_page()
    _, token = make_user()
    body = client_for(token).get("/api/web/sites").json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["site_key"] == "example.com"
    assert item["page_count"] == 1
    assert item["snapshot_count"] == 2
    assert item["bytes"] > 0
    assert "title" in item and "network_tags" in item


# ---- pages/{id} (타임라인) ----


def seed_n_snapshots(n):
    """n개의 스냅샷을 가진 페이지 → page_id (페이지네이션 검증용)."""
    domain, slug = "example.com", storage.url_to_slug(URL)
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, URL, domain, slug)
        for i in range(n):
            text = f"내용 {i}"
            dn = f"2026-06-{i + 1:02d}T00-00-00"
            d = storage.page_dir(domain, slug) / dn
            d.mkdir(parents=True)
            (d / "content.md").write_text(text, encoding="utf-8")
            db.insert_snapshot(
                conn, page_id, taken_at=f"2026-06-{i + 1:02d}T00:00:00+00:00",
                dir_name=dn, content_hash=storage.content_sha256(text),
                final_url=URL, http_status=200, changed=1,
            )
        db.backfill_snapshot_bytes(conn)
    return page_id


def test_page_timeline(tmp_db):
    page_id = seed_page()
    _, token = make_user(role="archiver")
    body = client_for(token).get(f"/api/web/pages/{page_id}").json()
    assert body["page"]["url"] == URL
    assert len(body["snapshots"]) == 2
    # 최신 순 정렬 — 첫 행이 최신(idx=2), 마지막 행이 최초(idx=1, new)
    assert body["snapshots"][0]["idx"] == 2
    assert body["snapshots"][0]["badge"] in ("changed", "same")
    assert body["snapshots"][1]["idx"] == 1
    assert body["snapshots"][1]["badge"] == "new"  # 최초 스냅샷(#1)이 신규
    assert body["total"] == 2
    assert body["total_pages"] == 1
    assert body["page_num"] == 1
    assert body["limit"] == 25
    assert body["limits"] == [10, 25, 50, 100]
    assert len(body["checks"]) == 1
    assert body["can_archive"] is True
    assert body["can_delete"] is False  # archiver 는 삭제 권한 없음


def test_page_timeline_pagination(tmp_db):
    """최신 순 페이징 — idx(history 번호)는 전체 기준으로 페이지가 바뀌어도 유지."""
    page_id = seed_n_snapshots(11)
    _, token = make_user(role="archiver")
    c = client_for(token)
    body = c.get(f"/api/web/pages/{page_id}?limit=10&page=1").json()
    assert body["total"] == 11
    assert body["total_pages"] == 2
    assert body["page_num"] == 1
    assert body["limit"] == 10
    assert len(body["snapshots"]) == 10
    assert body["snapshots"][0]["idx"] == 11   # 1페이지 첫 행 = 최신
    assert body["snapshots"][-1]["idx"] == 2
    body2 = c.get(f"/api/web/pages/{page_id}?limit=10&page=2").json()
    assert body2["page_num"] == 2
    assert len(body2["snapshots"]) == 1        # 2페이지 = 나머지 1개
    assert body2["snapshots"][0]["idx"] == 1
    assert body2["snapshots"][0]["badge"] == "new"


def test_page_timeline_limit_clamped(tmp_db):
    """허용 집합(10·25·50·100) 밖 limit 은 기본값(25)으로 clamp."""
    page_id = seed_page()
    _, token = make_user(role="archiver")
    body = client_for(token).get(f"/api/web/pages/{page_id}?limit=999").json()
    assert body["limit"] == 25


def test_page_timeline_page_overflow_clamped(tmp_db):
    """범위 밖 page 는 마지막(유효) 페이지로 clamp."""
    page_id = seed_page()
    _, token = make_user(role="archiver")
    body = client_for(token).get(f"/api/web/pages/{page_id}?page=99").json()
    assert body["page_num"] == 1


def test_page_timeline_404(tmp_db):
    _, token = make_user()
    assert client_for(token).get("/api/web/pages/9999").status_code == 404


def test_page_timeline_hides_authenticated_and_checks(tmp_db):
    """비소유자에겐 인증 스냅샷이 빠지고, 가려진 게 있으면 checks 도 숨겨진다."""
    owner, _ = make_user(email="owner@test.co", role="archiver")
    _, other_token = make_user(email="other@test.co", role="archiver")
    page_id = seed_auth_page(owner)
    body = client_for(other_token).get(f"/api/web/pages/{page_id}").json()
    hashes = [s["snap"]["content_hash"] for s in body["snapshots"]]
    assert "publichash01" in hashes
    assert "secrethash99" not in hashes  # 인증 스냅샷 숨김
    assert body["checks"] == []          # 가려진 인증 스냅샷 있으면 checks 도 숨김


def test_page_timeline_owner_sees_authenticated(tmp_db):
    owner, owner_token = make_user(email="owner@test.co", role="archiver")
    page_id = seed_auth_page(owner)
    body = client_for(owner_token).get(f"/api/web/pages/{page_id}").json()
    hashes = [s["snap"]["content_hash"] for s in body["snapshots"]]
    assert "secrethash99" in hashes
    assert any(c["content_hash"] == "checkhashCC9" for c in body["checks"])


# ---- snapshots/{id} (뷰어 메타) ----


def test_snapshot_meta(tmp_db):
    seed_page()
    _, token = make_user()
    body = client_for(token).get("/api/web/snapshots/1").json()
    assert body["snap"]["id"] == 1
    assert body["page_html_url"] == "/snapshot/1/file/page.html"
    assert body["has_screenshot"] is True
    assert body["documents"] == []


def test_snapshot_404(tmp_db):
    _, token = make_user()
    assert client_for(token).get("/api/web/snapshots/9999").status_code == 404


def test_snapshot_authenticated_owner_only(tmp_db):
    owner, owner_token = make_user(email="owner@test.co", role="archiver")
    _, other_token = make_user(email="other@test.co", role="archiver")
    seed_auth_page(owner)
    # 인증 스냅샷은 2번(secrethash99) — 소유자만 200, 타인은 404 은폐
    assert client_for(owner_token).get("/api/web/snapshots/2").status_code == 200
    assert client_for(other_token).get("/api/web/snapshots/2").status_code == 404
    # 공개 스냅샷 1번은 누구나
    assert client_for(other_token).get("/api/web/snapshots/1").status_code == 200


# ---- diff/{id} ----


def test_diff_default_latest_two(tmp_db):
    page_id = seed_page()
    _, token = make_user()
    body = client_for(token).get(f"/api/web/diff/{page_id}").json()
    assert body["from_idx"] == 1 and body["to_idx"] == 2
    assert body["total"] == 2
    assert body["rows"]  # 라인 단위 diff 행
    assert body["added"] >= 1


def test_diff_bad_range(tmp_db):
    page_id = seed_page()
    _, token = make_user()
    r = client_for(token).get(f"/api/web/diff/{page_id}?from=2&to=1")
    assert r.status_code == 400


def test_diff_too_few_snapshots(tmp_db):
    page_id = seed_page(contents=("한 개뿐",))
    _, token = make_user()
    assert client_for(token).get(f"/api/web/diff/{page_id}").status_code == 400


def test_diff_authenticated_denied(tmp_db):
    owner, owner_token = make_user(email="owner@test.co", role="archiver")
    _, other_token = make_user(email="other@test.co", role="archiver")
    page_id = seed_auth_page(owner)
    # 인증 스냅샷이 한쪽에 끼면 비소유자는 404
    assert client_for(other_token).get(f"/api/web/diff/{page_id}").status_code == 404
    assert client_for(owner_token).get(f"/api/web/diff/{page_id}").status_code == 200


# ---- search ----


def test_search_requires_view(tmp_db):
    """검색은 view 권한(viewer 이상)이 하한 — 권한 없는 pending 등은 401(미인증 취급)."""
    _, token = make_user(role="viewer")
    r = client_for(token).get("/api/web/search?q=")
    assert r.status_code == 200
    assert r.json()["q"] == ""


def test_search_query_when_unavailable(tmp_db):
    """검색 인덱스 미가용/빈 질의여도 200 + 구조 유지."""
    seed_page()
    _, token = make_user()
    body = client_for(token).get("/api/web/search?q=본문").json()
    assert "available" in body
    assert body["q"] == "본문"
    assert body["per_page"] == 20


# ---- documents ----


def test_documents_list(tmp_db):
    seed_page()
    _, token = make_user()
    body = client_for(token).get("/api/web/documents").json()
    assert "groups" in body and "totals" in body
    assert body["page"] == 1
    assert body["has_next"] is False


# ---- schedules ----


def test_schedules_list(tmp_db):
    page_id = seed_page()
    with db.connect() as conn:
        db.upsert_schedule(conn, page_id, interval_seconds=43200,
                           next_run_at="2026-07-01T00:00:00+00:00")
    _, token = make_user(role="archiver")
    body = client_for(token).get("/api/web/schedules").json()
    assert len(body["items"]) == 1
    assert body["items"][0]["interval_seconds"] == 43200
    assert body["items"][0]["label"]
    assert body["can_archive"] is True


# ---- logs ----


def test_logs_filter_by_status(tmp_db):
    insert_log("new", started_at="2026-06-01T00:00:00+00:00")
    insert_log("error", started_at="2026-06-02T00:00:00+00:00", error="boom")
    _, token = make_user(role="admin")
    c = client_for(token)
    allrows = c.get("/api/web/logs").json()
    assert allrows["total"] == 2
    errors = c.get("/api/web/logs?status=error").json()
    assert errors["total"] == 1
    assert errors["items"][0]["log"]["status"] == "error"


def test_logs_invalid_status_ignored(tmp_db):
    insert_log("new")
    _, token = make_user(role="admin")
    body = client_for(token).get("/api/web/logs?status=bogus").json()
    assert body["status"] == ""  # 화이트리스트 밖은 무시
    assert body["total"] == 1


def test_logs_domain_filter(tmp_db):
    insert_log("new", domain="a.example.com")
    insert_log("new", domain="b.example.com")
    _, token = make_user(role="admin")
    body = client_for(token).get("/api/web/logs?domain=a.example.com").json()
    assert body["total"] == 1
    assert set(body["domains"]) == {"a.example.com", "b.example.com"}


def test_logs_pagination(tmp_db):
    for i in range(5):
        insert_log("new", started_at=f"2026-06-0{i + 1}T00:00:00+00:00")
    _, token = make_user(role="admin")
    body = client_for(token).get("/api/web/logs?limit=10&page=1").json()
    assert body["total"] == 5
    assert body["limit"] == 10
    assert body["page_num"] == 1
