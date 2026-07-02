"""SvelteKit SPA 사이트 API(/api/web/sites) 테스트 — 목록·상세·페이징·byte·권한 플래그.

Phase C2 컷오버로 제거되는 SSR 사이트 화면(app.index/site_view)의 검증 로직을 JSON
API 기준으로 대체한다. SSR test_web.py(site·pagination·byte)·test_deletion(권한 플래그)
의 사이트 화면 단정에 대응한다.

인증서: SSR /sites/{id} 가 노출하던 TLS 인증서 이력을 /api/web/sites/{id} 가 cert_rows 로
내려주고(주체·발급자·SAN·유효/확인 기간·일련번호·서명 알고리즘·지문·현재/검증 플래그) SPA
사이트 화면이 호스트별 카드로 상세 표시한다 — test_site_detail_certificates 가 카드가 쓰는
응답 필드를 검증하고, .pem 다운로드 바이너리 라우트는 test_certs.py 가 본다.
"""
import json

import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, storage
from chunchugwan.web import app as web_app

DOMAIN = "example.com"


def _cert_info(host=DOMAIN, fingerprint="ab" * 32):
    """upsert_site_certificate 용 파싱 결과 dict (test_certs._info 와 동일 형식)."""
    return {
        "host": host, "fingerprint": fingerprint,
        "subject": "CN=example.com", "issuer": "CN=Test CA", "serial": "1a2b",
        "san": json.dumps(["example.com", "www.example.com"]),
        "not_before": "2026-01-01T00:00:00+00:00",
        "not_after": "2026-12-31T23:59:59+00:00",
        "signature_algorithm": "sha256",
        "pem": "-----BEGIN CERTIFICATE-----\nMA==\n-----END CERTIFICATE-----\n",
    }


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
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


def seed_site(n_pages=1, snaps_per_page=1):
    """같은 도메인에 페이지 n_pages 개(각 snaps_per_page 스냅샷) 시드 → site_id."""
    with db.connect() as conn:
        for p in range(n_pages):
            url = f"https://{DOMAIN}/post{p}"
            slug = storage.url_to_slug(url)
            page_id = db.get_or_create_page(conn, url, DOMAIN, slug)
            for i in range(snaps_per_page):
                dn = f"2026-06-0{i + 1}T00-00-00"
                d = storage.page_dir(DOMAIN, slug) / dn
                d.mkdir(parents=True)
                (d / "content.md").write_text(f"본문{p}-{i}", encoding="utf-8")
                db.insert_snapshot(
                    conn, page_id, taken_at=f"2026-06-0{i + 1}T00:00:00+00:00",
                    dir_name=dn, content_hash=f"hash{p}{i}",
                    final_url=url, http_status=200, changed=1,
                )
        db.backfill_snapshot_bytes(conn)
        site_id = db.get_site_by_key(conn, DOMAIN)["id"]
    return site_id


# ---- 인증 가드 ----


def test_site_detail_requires_session(tmp_db):
    site_id = seed_site()
    assert client_for().get(f"/api/web/sites/{site_id}").status_code == 401


def test_read_gets_require_view_permission(tmp_db):
    """읽기 GET 은 view 권한을 요구한다(검색과 일관) — view 뺀 계정은 403, 있으면 200."""
    seed_site()
    uid, token = make_user("noview@test.co", role="viewer")
    with db.connect() as conn:
        db.set_permission_overrides(conn, uid, {"view": False})  # view 제거
    c = client_for(token)
    for path in ("/api/web/dashboard", "/api/web/sites",
                 "/api/web/documents", "/api/web/schedules"):
        assert c.get(path).status_code == 403, path
    # view 를 가진 일반 viewer 는 정상 열람
    _, tok_ok = make_user("hasview@test.co", role="viewer")
    assert client_for(tok_ok).get("/api/web/sites").status_code == 200


# ---- 목록 ----


def test_sites_list_counts(tmp_db):
    seed_site(n_pages=2, snaps_per_page=2)
    _, token = make_user()
    item = client_for(token).get("/api/web/sites").json()["items"][0]
    assert item["site_key"] == DOMAIN
    assert item["page_count"] == 2
    assert item["snapshot_count"] == 4
    assert item["bytes"] > 0


# ---- 상세 ----


def test_site_detail(tmp_db):
    site_id = seed_site(n_pages=1, snaps_per_page=2)
    _, token = make_user(role="archiver")
    body = client_for(token).get(f"/api/web/sites/{site_id}").json()
    assert body["site"]["site_key"] == DOMAIN
    assert body["page_count"] == 1
    assert body["snapshot_total"] == 2
    assert len(body["pages"]) == 1
    assert body["pages"][0]["bytes"] > 0
    assert body["site_bytes"] > 0
    assert body["pager"]["page"] == 1 and body["pager"]["per_page"] == 10
    assert body["can_archive"] is True
    assert body["can_delete"] is False  # archiver
    assert "can_manage_credentials" in body


def test_site_detail_404(tmp_db):
    _, token = make_user()
    assert client_for(token).get("/api/web/sites/9999").status_code == 404


def test_site_detail_certificates(tmp_db):
    site_id = seed_site()
    with db.connect() as conn:
        db.upsert_site_certificate(conn, site_id, _cert_info(), verified=True)
    _, token = make_user(email="a@test.co", role="archiver")
    body = client_for(token).get(f"/api/web/sites/{site_id}").json()
    assert len(body["certificates"]) == 1
    cert = body["certificates"][0]
    assert cert["is_current"] is True
    assert cert["san"] == ["example.com", "www.example.com"]
    assert cert["pem_url"] == f"/sites/{site_id}/certificates/{cert['cert']['id']}.pem"
    # SPA 카드가 표시하는 상세 필드를 응답이 모두 담아야 한다 (표시 회귀 방지)
    c = cert["cert"]
    assert c["subject"] == "CN=example.com"
    assert c["issuer"] == "CN=Test CA"
    assert c["serial"] == "1a2b"
    assert c["fingerprint"] == "ab" * 32
    assert c["not_before"] == "2026-01-01T00:00:00+00:00"
    assert c["not_after"] == "2026-12-31T23:59:59+00:00"
    assert c["signature_algorithm"] == "sha256"
    assert c["verified"] == 1
    assert c["first_seen_at"] and c["last_seen_at"]


def test_site_detail_no_certificates(tmp_db):
    site_id = seed_site()
    _, token = make_user()
    assert client_for(token).get(f"/api/web/sites/{site_id}").json()["certificates"] == []


def test_site_detail_can_flags_by_role(tmp_db):
    site_id = seed_site()
    _, viewer = make_user(email="v@test.co", role="viewer")
    _, admin = make_user(email="a@test.co", role="admin")
    vbody = client_for(viewer).get(f"/api/web/sites/{site_id}").json()
    assert vbody["can_archive"] is False and vbody["can_delete"] is False
    abody = client_for(admin).get(f"/api/web/sites/{site_id}").json()
    assert abody["can_archive"] is True and abody["can_delete"] is True


# ---- 페이징 ----


def test_error_detail_translated_by_locale(tmp_db):
    """경계 예외 핸들러가 HTTPException detail 을 요청 로케일로 번역한다 (H9).

    라우트가 한국어 detail 을 그대로 raise 해도 en 사용자는 영어로 받는다 —
    미들웨어가 적재한 request.state.locale 이 핸들러까지 전파되는지도 함께 검증.
    """
    uid, token = make_user()
    c = client_for(token)
    # 기본(ko) 로케일: 원문 유지
    r_ko = c.get("/api/web/sites/999999")
    assert r_ko.status_code == 404 and r_ko.json()["detail"] == "사이트 없음"
    # 저장 로케일 en: 카탈로그 번역
    with db.connect() as conn:
        db.set_user_locale(conn, uid, "en")
    r_en = c.get("/api/web/sites/999999")
    assert r_en.status_code == 404 and r_en.json()["detail"] == "Site not found"


def test_site_detail_pagination(tmp_db):
    site_id = seed_site(n_pages=5)
    _, token = make_user()
    c = client_for(token)
    body = c.get(f"/api/web/sites/{site_id}?per_page=25&page=1").json()
    assert body["pager"]["total"] == 5
    assert body["pager"]["per_page"] == 25
    assert body["pager"]["total_pages"] == 1
    # 잘못된 per_page 는 기본값 10 으로 폴백
    bad = c.get(f"/api/web/sites/{site_id}?per_page=999").json()
    assert bad["pager"]["per_page"] == 10
    # 범위 초과 page 는 마지막 페이지로 클램프
    clamped = c.get(f"/api/web/sites/{site_id}?page=99").json()
    assert clamped["pager"]["page"] == clamped["pager"]["total_pages"]


# ---- 린 목록 엔드포인트 (상세 화면 페이저 in-place 갱신용) ----


def test_site_lists_endpoint(tmp_db):
    """GET /sites/{id}/lists — 3개 목록(페이지·회차·실패)과 각 페이저만 반환(통계·인증서 등 제외)."""
    site_id = seed_site(n_pages=30)
    _, token = make_user()
    c = client_for(token)
    # 1페이지(per_page 25) → 25개, 메타 정확, 린 응답(6개 키만)
    body = c.get(f"/api/web/sites/{site_id}/lists?per_page=25&page=1").json()
    assert set(body.keys()) == {
        "pages", "pager", "crawls", "crawls_pager", "failed_items", "failed_pager"
    }
    assert len(body["pages"]) == 25
    assert body["pager"] == {"page": 1, "total_pages": 2, "per_page": 25, "total": 30}
    assert body["pages"][0]["bytes"] > 0 and "url" in body["pages"][0]
    # 2페이지 → 나머지 5개, 1페이지와 겹치지 않음(오프셋 슬라이싱)
    page1_ids = {p["id"] for p in body["pages"]}
    body2 = c.get(f"/api/web/sites/{site_id}/lists?per_page=25&page=2").json()
    assert len(body2["pages"]) == 5 and body2["pager"]["page"] == 2
    assert page1_ids.isdisjoint({p["id"] for p in body2["pages"]})
    # site_detail 과 동일한 페이징 규칙: 잘못된 per_page→10, 범위초과 page→마지막으로 클램프
    assert c.get(f"/api/web/sites/{site_id}/lists?per_page=999").json()["pager"]["per_page"] == 10
    clamped = c.get(f"/api/web/sites/{site_id}/lists?page=99&per_page=25").json()
    assert clamped["pager"]["page"] == clamped["pager"]["total_pages"] == 2


def test_site_lists_endpoint_requires_session(tmp_db):
    site_id = seed_site()
    assert client_for().get(f"/api/web/sites/{site_id}/lists").status_code == 401


def test_site_lists_endpoint_404(tmp_db):
    _, token = make_user()
    assert client_for(token).get("/api/web/sites/9999/lists").status_code == 404


# ---- archive/list 필터·페이징 (전체 사이트 대상) ----


def _seed_named_sites(domains):
    """각 도메인에 페이지 1개씩 시드 (목록·필터 페이징 테스트용)."""
    with db.connect() as conn:
        for dom in domains:
            url = f"https://{dom}/p0"
            db.get_or_create_page(conn, url, dom, storage.url_to_slug(url))


def test_sites_filter_applies_to_whole_list(tmp_db):
    """q 는 현재 페이지가 아니라 전체 사이트에서 site_key 부분 일치로 거른다."""
    _seed_named_sites(
        [f"shop{i:02d}.example" for i in range(30)] + ["blog.example", "news.example"]
    )
    _, token = make_user()
    c = client_for(token)
    # 필터 없음: 기본 per_page 25, 총 32, 2페이지
    body = c.get("/api/web/sites").json()
    assert body["total"] == 32 and body["limit"] == 25 and body["total_pages"] == 2
    assert len(body["items"]) == 25 and body["limits"] == [10, 25, 50, 100]
    # q=shop → 전체에서 30개 매칭(현재 페이지에 안 보이던 것 포함), 2페이지로 분할
    # (목록 페이지 크기 쿼리명은 limit — 프론트·딥링크와 일치, H3)
    p1 = c.get("/api/web/sites?q=shop&limit=25&page=1").json()
    assert p1["q"] == "shop" and p1["total"] == 30 and p1["total_pages"] == 2
    assert len(p1["items"]) == 25 and all("shop" in it["site_key"] for it in p1["items"])
    p2 = c.get("/api/web/sites?q=shop&limit=25&page=2").json()
    assert len(p2["items"]) == 5 and all("shop" in it["site_key"] for it in p2["items"])
    assert {it["site_key"] for it in p1["items"]}.isdisjoint(
        {it["site_key"] for it in p2["items"]}
    )
    # 매칭 없으면 0
    assert c.get("/api/web/sites?q=nomatch").json()["total"] == 0


def test_sites_pagination_clamp_and_bad_per_page(tmp_db):
    _seed_named_sites([f"s{i:02d}.example" for i in range(12)])
    _, token = make_user()
    c = client_for(token)
    assert c.get("/api/web/sites?limit=10&page=1").json()["total_pages"] == 2
    # 허용 밖 limit → 기본 25
    assert c.get("/api/web/sites?limit=999").json()["limit"] == 25
    # 범위 초과 page → 마지막으로 클램프
    clamped = c.get("/api/web/sites?limit=10&page=99").json()
    assert clamped["page_num"] == clamped["total_pages"] == 2


# ---- 사이트 상세: 회차·실패 목록 페이징 (각각 독립) ----


def _seed_crawls(n):
    with db.connect() as conn:
        for _ in range(n):
            db.insert_crawl(
                conn, start_url=f"https://{DOMAIN}/", scope_host=DOMAIN,
                scope_path="/", max_pages=10, max_depth=2, delay_seconds=0, source="cli",
            )
        return db.get_site_by_key(conn, DOMAIN)["id"]


def _seed_failed(n):
    with db.connect() as conn:
        for i in range(n):
            url = f"https://{DOMAIN}/fail{i}"
            pid = db.get_or_create_page(conn, url, DOMAIN, storage.url_to_slug(url))
            db.insert_archive_log(
                conn, url=url, domain=DOMAIN, source="web", status="error",
                page_id=pid, started_at=f"2026-06-10T00:{i:02d}:00+00:00", duration_ms=10,
            )
        return db.get_site_by_key(conn, DOMAIN)["id"]


def test_site_lists_crawls_pagination(tmp_db):
    sid = _seed_crawls(13)
    _, token = make_user()
    c = client_for(token)
    body = c.get(f"/api/web/sites/{sid}/lists?crawls_per_page=10&crawls_page=1").json()
    assert body["crawls_pager"] == {"page": 1, "total_pages": 2, "per_page": 10, "total": 13}
    assert len(body["crawls"]) == 10
    body2 = c.get(f"/api/web/sites/{sid}/lists?crawls_per_page=10&crawls_page=2").json()
    assert len(body2["crawls"]) == 3 and body2["crawls_pager"]["page"] == 2
    # site_detail 도 같은 회차 페이저를 내려준다
    detail = c.get(f"/api/web/sites/{sid}?crawls_per_page=10").json()
    assert detail["crawls_pager"]["total"] == 13 and len(detail["crawls"]) == 10


def test_site_lists_failed_pagination(tmp_db):
    sid = _seed_failed(13)
    _, token = make_user()
    c = client_for(token)
    body = c.get(f"/api/web/sites/{sid}/lists?failed_per_page=10&failed_page=1").json()
    assert body["failed_pager"]["total"] == 13 and body["failed_pager"]["total_pages"] == 2
    assert len(body["failed_items"]) == 10
    body2 = c.get(f"/api/web/sites/{sid}/lists?failed_per_page=10&failed_page=2").json()
    assert len(body2["failed_items"]) == 3
    # 한 목록 페이징은 다른 목록 위치에 영향 없음(독립 파라미터)
    assert body["pager"]["page"] == 1 and body["crawls_pager"]["page"] == 1
