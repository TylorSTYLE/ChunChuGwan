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
    assert body["pager"]["page"] == 1 and body["pager"]["per_page"] == 50
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


def test_site_detail_pagination(tmp_db):
    site_id = seed_site(n_pages=5)
    _, token = make_user()
    c = client_for(token)
    body = c.get(f"/api/web/sites/{site_id}?per_page=25&page=1").json()
    assert body["pager"]["total"] == 5
    assert body["pager"]["per_page"] == 25
    assert body["pager"]["total_pages"] == 1
    # 잘못된 per_page 는 기본값 50 으로 폴백
    bad = c.get(f"/api/web/sites/{site_id}?per_page=999").json()
    assert bad["pager"]["per_page"] == 50
    # 범위 초과 page 는 마지막 페이지로 클램프
    clamped = c.get(f"/api/web/sites/{site_id}?page=99").json()
    assert clamped["pager"]["page"] == clamped["pager"]["total_pages"]


# ---- 린 페이지목록 엔드포인트 (상세 화면 페이저 in-place 갱신용) ----


def test_site_pages_endpoint(tmp_db):
    """GET /sites/{id}/pages — pages/pager 만 반환(통계·인증서 등 제외)하고 슬라이싱·클램프 동작."""
    site_id = seed_site(n_pages=30)
    _, token = make_user()
    c = client_for(token)
    # 1페이지(per_page 25) → 25개, 메타 정확, 린 응답(무거운 키 없음)
    body = c.get(f"/api/web/sites/{site_id}/pages?per_page=25&page=1").json()
    assert set(body.keys()) == {"pages", "pager"}
    assert len(body["pages"]) == 25
    assert body["pager"] == {"page": 1, "total_pages": 2, "per_page": 25, "total": 30}
    assert body["pages"][0]["bytes"] > 0 and "url" in body["pages"][0]
    # 2페이지 → 나머지 5개, 1페이지와 겹치지 않음(오프셋 슬라이싱)
    page1_ids = {p["id"] for p in body["pages"]}
    body2 = c.get(f"/api/web/sites/{site_id}/pages?per_page=25&page=2").json()
    assert len(body2["pages"]) == 5 and body2["pager"]["page"] == 2
    assert page1_ids.isdisjoint({p["id"] for p in body2["pages"]})
    # site_detail 과 동일한 페이징 규칙: 잘못된 per_page→50, 범위초과 page→마지막으로 클램프
    assert c.get(f"/api/web/sites/{site_id}/pages?per_page=999").json()["pager"]["per_page"] == 50
    clamped = c.get(f"/api/web/sites/{site_id}/pages?page=99&per_page=25").json()
    assert clamped["pager"]["page"] == clamped["pager"]["total_pages"] == 2


def test_site_pages_endpoint_requires_session(tmp_db):
    site_id = seed_site()
    assert client_for().get(f"/api/web/sites/{site_id}/pages").status_code == 401


def test_site_pages_endpoint_404(tmp_db):
    _, token = make_user()
    assert client_for(token).get("/api/web/sites/9999/pages").status_code == 404
