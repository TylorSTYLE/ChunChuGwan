"""클러스터 전송 — 스냅샷 1건 직렬화/적재·보호 강제·provenance·커서·페이싱.

A 측 서빙/수신 엔드포인트(TestClient + 클러스터 키)와 B 측 조정 루프 pull/push
(HTTP 클라이언트 모킹)를 검증한다. 전송 단위는 스냅샷 1건(원자)이다.
"""
import hashlib

import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, cluster, cluster_sync, config, db, storage
from chunchugwan.web import app as web_app

POST_HEADERS = {"X-Requested-With": "fetch", "Origin": "http://testserver"}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "SECRET_KEY", "test-cluster-secret")
    monkeypatch.setattr(config, "CLUSTER_SEND_MIN_INTERVAL_SECONDS", 0)
    cluster_sync._backoff_until.clear()
    cluster_sync._backoff_fails.clear()
    with db.connect() as conn:
        db.create_user(conn, "seed@test.co", auth.hash_password("seedpass123"), role="admin")
    yield


def _seed_snapshot(url, dir_name="2026-06-01T00-00-00", *, shareable=True, text="본문"):
    """페이지 1개 + 스냅샷(content.md·meta.json) 구성. shareable 면 보호 OFF."""
    domain = url.split("/")[2]
    slug = storage.url_to_slug(url)
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, url, domain, slug)
        snap_dir = storage.page_dir(domain, slug) / dir_name
        snap_dir.mkdir(parents=True, exist_ok=True)
        (snap_dir / "content.md").write_text(text, encoding="utf-8")
        (snap_dir / "page.html.gz").write_bytes(b"\x1f\x8bfake-gz")
        snap_id = db.insert_snapshot(
            conn, page_id, taken_at=dir_name[:10] + "T00:00:00+00:00",
            dir_name=dir_name, content_hash=storage.content_sha256(text),
            final_url=url, http_status=200, changed=1,
        )
        db.update_snapshot_bytes(conn, snap_id, storage.snapshot_dir_bytes(snap_dir))
        if shareable:
            db.set_page_cluster_protect(conn, page_id, False)
    return page_id, snap_id


def _key(*, send=False, receive=False):
    with db.connect() as conn:
        return auth.issue_api_key(
            conn, "peer", can_view=False, can_archive=False, created_by=None,
            ttl_seconds=None, owner_user_id=None,
            can_cluster_send=send, can_cluster_receive=receive,
        )


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


# ---- A 측: 받기 서빙 (pull) ----


def test_list_only_shareable(tmp_db):
    _, sid = _seed_snapshot("https://a.test/p1", shareable=True)
    _seed_snapshot("https://a.test/p2", shareable=False)  # 보호 ON → 제외
    c = TestClient(web_app.app)
    r = c.get("/api/cluster/snapshots", headers=_hdr(_key(receive=True)))
    assert r.status_code == 200
    ids = [s["id"] for s in r.json()["snapshots"]]
    assert ids == [sid]


def test_envelope_roundtrip_fields(tmp_db):
    _, sid = _seed_snapshot("https://a.test/p1", shareable=True)
    c = TestClient(web_app.app)
    r = c.get(f"/api/cluster/snapshots/{sid}", headers=_hdr(_key(receive=True)))
    assert r.status_code == 200
    env = r.json()
    with db.connect() as conn:
        assert env["origin_node_id"] == db.cluster_node_id(conn)
    assert env["origin_ref"] == str(sid)
    assert env["page"]["url"] == "https://a.test/p1"
    names = {f["name"] for f in env["files"]}
    assert {"content.md", "page.html.gz"} <= names


def test_protected_envelope_404(tmp_db):
    _, sid = _seed_snapshot("https://a.test/p2", shareable=False)
    c = TestClient(web_app.app)
    assert c.get(f"/api/cluster/snapshots/{sid}", headers=_hdr(_key(receive=True))).status_code == 404


def test_direction_gates(tmp_db):
    _, sid = _seed_snapshot("https://a.test/p1")
    c = TestClient(web_app.app)
    recv, send = _key(receive=True), _key(send=True)
    # receive 키로는 push(POST) 불가
    assert c.post("/api/cluster/snapshots", json={}, headers={**_hdr(recv), **POST_HEADERS}).status_code == 403
    # send 키로는 pull(GET 목록) 불가
    assert c.get("/api/cluster/snapshots", headers=_hdr(send)).status_code == 403


def test_busy_backpressure(tmp_db, monkeypatch):
    c = TestClient(web_app.app)
    monkeypatch.setattr(db, "count_active_archive_jobs", lambda conn: 99)
    r = c.post("/api/cluster/snapshots", json={"protocol_version": config.CLUSTER_PROTOCOL_VERSION},
               headers={**_hdr(_key(send=True)), **POST_HEADERS})
    assert r.status_code == 429 and r.headers.get("Retry-After")


# ---- A 측: 보내기 수신 (push) + provenance/중복/로그 ----


def _foreign_envelope(url="https://foreign.test/x", origin="node-FOREIGN", ref="7", text="외부본문"):
    return {
        "protocol_version": config.CLUSTER_PROTOCOL_VERSION,
        "origin_node_id": origin,
        "origin_ref": ref,
        "page": {"url": url, "domain": url.split("/")[2], "slug": storage.url_to_slug(url)},
        "snapshot": {
            "taken_at": "2026-06-01T00:00:00+00:00", "dir_name": "2026-06-01T00-00-00",
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "final_url": url, "http_status": 200, "changed": 1, "note": None,
            "title": "외부", "origin": "server", "incomplete": 0, "authenticated": 0,
        },
        "files": [{"name": "content.md",
                   "b64": __import__("base64").b64encode(text.encode()).decode()}],
        "resources": [], "documents": [],
    }


def test_push_receive_creates_with_provenance_and_log(tmp_db):
    c = TestClient(web_app.app)
    env = _foreign_envelope()
    r = c.post("/api/cluster/snapshots", json=env, headers={**_hdr(_key(send=True)), **POST_HEADERS})
    assert r.status_code == 200 and r.json()["status"] == "new"
    with db.connect() as conn:
        snap = db.find_snapshot_by_provenance(conn, "node-FOREIGN", "7")
        assert snap is not None and snap["origin_node_id"] == "node-FOREIGN"
        logs = db.list_archive_logs(conn, limit=10)
        assert any(log["source"] == "cluster" and log["status"] == "new" for log in logs)
    # 중복 수신 — status duplicate, 새 로그 없음
    r2 = c.post("/api/cluster/snapshots", json=env, headers={**_hdr(_key(send=True)), **POST_HEADERS})
    assert r2.json()["status"] == "duplicate"
    with db.connect() as conn:
        assert db.count_archive_logs(conn) == 1


def test_push_receive_rejects_self_origin(tmp_db):
    c = TestClient(web_app.app)
    with db.connect() as conn:
        my = db.cluster_node_id(conn)
    env = _foreign_envelope(origin=my)
    r = c.post("/api/cluster/snapshots", json=env, headers={**_hdr(_key(send=True)), **POST_HEADERS})
    assert r.status_code == 409


# ---- B 측: 조정 루프 pull/push (HTTP 클라이언트 모킹) ----


def _make_peer(node_id="peer-X", send=False, receive=True):
    from chunchugwan import crypto
    with db.connect() as conn:
        return db.create_cluster_peer(
            conn, peer_node_id=node_id, display_name="X", base_url="https://x.test",
            api_key_enc=crypto.encrypt("wccg_k"), send_enabled=send, receive_enabled=receive,
        )


def test_pull_delta_imports_and_advances_cursor(tmp_db, monkeypatch):
    pid = _make_peer(node_id="peer-SRC", receive=True)
    env = _foreign_envelope(origin="peer-SRC", ref="42")
    monkeypatch.setattr(cluster, "pull_list",
                        lambda b, k, after, limit: [{"id": 42}] if after < 42 else [])
    monkeypatch.setattr(cluster, "pull_envelope", lambda b, k, sid: env)
    cluster_sync._pull_delta(pid, "https://x.test", "wccg_k", "peer-SRC")
    with db.connect() as conn:
        assert db.find_snapshot_by_provenance(conn, "peer-SRC", "42") is not None
        assert db.get_cluster_peer(conn, pid)["receive_cursor"] == 42
        assert any(log["source"] == "cluster" for log in db.list_archive_logs(conn, limit=5))
    # 재실행 — 커서 이후 신규 없음 → 중복 적재·로그 없음
    cluster_sync._pull_delta(pid, "https://x.test", "wccg_k", "peer-SRC")
    with db.connect() as conn:
        assert db.count_archive_logs(conn) == 1


def test_push_delta_sends_shareable_and_logs(tmp_db, monkeypatch):
    _, sid = _seed_snapshot("https://a.test/p1", shareable=True)
    _seed_snapshot("https://a.test/secret", dir_name="2026-06-02T00-00-00", shareable=False)
    pid = _make_peer(node_id="peer-DST", send=True, receive=False)
    sent = []
    monkeypatch.setattr(cluster, "push_negotiate", lambda b, k, blobs: [])
    monkeypatch.setattr(cluster, "push_snapshot",
                        lambda b, k, env: sent.append(env) or "new")
    cluster_sync._push_delta(pid, "https://x.test", "wccg_k")
    # 공유분만 전송, 보호분 제외
    sent_refs = [e["origin_ref"] for e in sent]
    assert sent_refs == [str(sid)]
    with db.connect() as conn:
        assert db.get_cluster_peer(conn, pid)["send_cursor"] == sid
        actions = [a for a in db.list_audit_logs(conn, limit=10) if a["action"] == "cluster_send"]
        assert len(actions) == 1


def test_push_delta_backs_off_on_busy(tmp_db, monkeypatch):
    _seed_snapshot("https://a.test/p1", shareable=True)
    pid = _make_peer(node_id="peer-DST", send=True)

    def busy(b, k, blobs):
        raise cluster.PeerBusy(60)

    monkeypatch.setattr(cluster, "push_negotiate", busy)
    cluster_sync._push_delta(pid, "https://x.test", "wccg_k")
    with db.connect() as conn:
        # 커서 미전진 (다음 사이클 재시도)
        assert db.get_cluster_peer(conn, pid)["send_cursor"] == 0


# ---- CAS 블롭 — 무결성·서빙·협상 ----


def test_store_cas_blob_integrity(tmp_db):
    body = b"resource-bytes"
    name = hashlib.sha256(body).hexdigest() + ".png"
    cluster.store_cas_blob("resource", name, body)
    assert cluster.read_cas_blob("resource", name) == body
    # sha256 불일치는 거부
    bad = hashlib.sha256(b"other").hexdigest() + ".png"
    with pytest.raises(cluster.IntegrityError):
        cluster.store_cas_blob("resource", bad, body)


def test_blob_get_serves_and_404(tmp_db):
    body = b"img-data"
    name = hashlib.sha256(body).hexdigest() + ".png"
    cluster.store_cas_blob("resource", name, body)
    c = TestClient(web_app.app)
    r = c.get(f"/api/cluster/blobs/resource/{name}", headers=_hdr(_key(receive=True)))
    assert r.status_code == 200 and r.content == body
    missing = hashlib.sha256(b"nope").hexdigest() + ".png"
    assert c.get(f"/api/cluster/blobs/resource/{missing}", headers=_hdr(_key(receive=True))).status_code == 404


def test_negotiate_returns_only_missing(tmp_db):
    body = b"have-this"
    have = hashlib.sha256(body).hexdigest() + ".png"
    cluster.store_cas_blob("resource", have, body)
    miss = hashlib.sha256(b"missing").hexdigest() + ".png"
    c = TestClient(web_app.app)
    r = c.post("/api/cluster/negotiate",
               json={"blobs": [{"kind": "resource", "name": have},
                               {"kind": "resource", "name": miss}]},
               headers={**_hdr(_key(send=True)), **POST_HEADERS})
    assert r.status_code == 200
    names = [m["name"] for m in r.json()["missing"]]
    assert names == [miss]


def test_blob_put_verifies_sha(tmp_db):
    c = TestClient(web_app.app)
    body = b"upload-bytes"
    name = hashlib.sha256(body).hexdigest() + ".png"
    r = c.post(f"/api/cluster/blobs/resource/{name}", content=body,
               headers={**_hdr(_key(send=True)), **POST_HEADERS,
                        "Content-Type": "application/octet-stream"})
    assert r.status_code == 200 and cluster.read_cas_blob("resource", name) == body
    # 이름과 다른 내용 → 무결성 거부
    r2 = c.post(f"/api/cluster/blobs/resource/{name}", content=b"tampered",
                headers={**_hdr(_key(send=True)), **POST_HEADERS,
                         "Content-Type": "application/octet-stream"})
    assert r2.status_code == 400


def test_push_delta_uploads_missing_blob(tmp_db, monkeypatch):
    # 자원 블롭이 있는 스냅샷을 push 할 때 협상에서 받은 결손만 업로드한다.
    _, sid = _seed_snapshot("https://a.test/withres", shareable=True)
    body = b"shared-resource"
    name = hashlib.sha256(body).hexdigest() + ".css"
    cluster.store_cas_blob("resource", name, body)
    with db.connect() as conn:
        db.insert_snapshot_resources(conn, sid, [{"name": name, "url": "https://a.test/s.css"}])
    pid = _make_peer(node_id="peer-DST", send=True, receive=False)
    uploaded = []
    monkeypatch.setattr(cluster, "push_negotiate", lambda b, k, blobs: [("resource", name)])
    monkeypatch.setattr(cluster, "push_blob",
                        lambda b, k, kind, nm, payload: uploaded.append((kind, nm, payload)))
    monkeypatch.setattr(cluster, "push_snapshot", lambda b, k, env: "new")
    cluster_sync._push_delta(pid, "https://x.test", "wccg_k")
    assert uploaded == [("resource", name, body)]


# ---- 보호 opt-in (워커 후처리·enqueue 전달) ----


def test_apply_archive_protect_page_and_new_site(tmp_db):
    pid, _ = _seed_snapshot("https://new.test/only", shareable=False)
    with db.connect() as conn:
        page = db.get_page(conn, "https://new.test/only")
        site_id = page["site_id"]
        # 새 사이트(소속 페이지 1개) → page + site 기본값 모두 적용
        db.apply_archive_protect(conn, page["id"], protect=False, site_protect_default=False)
        assert db.get_page_by_id(conn, page["id"])["cluster_protect"] == 0
        assert db.get_site(conn, site_id)["cluster_protect_default"] == 0


def test_apply_archive_protect_keeps_existing_site_default(tmp_db):
    _seed_snapshot("https://multi.test/a")
    db.get_or_create_page  # noqa
    p2, _ = _seed_snapshot("https://multi.test/b", dir_name="2026-06-02T00-00-00")
    with db.connect() as conn:
        page = db.get_page(conn, "https://multi.test/b")
        site_id = page["site_id"]
        db.set_site_cluster_protect_default(conn, site_id, True)
        # 소속 페이지 2개(기존 사이트) → site 기본값은 덮지 않는다
        db.apply_archive_protect(conn, page["id"], protect=False, site_protect_default=False)
        assert db.get_site(conn, site_id)["cluster_protect_default"] == 1  # 유지
        assert db.get_page_by_id(conn, page["id"])["cluster_protect"] == 0  # page 는 적용


def test_web_archive_enqueues_protect(tmp_db, monkeypatch):
    # netcheck 게이트 우회 — 공인 주소로 가정
    from chunchugwan.web import app as appmod
    monkeypatch.setattr(appmod, "_network_gate", lambda req, norm, tag: None)
    c = _admin_client()
    r = c.post("/api/web/archive",
               json={"url": "https://pub.test/x", "protect": False},
               headers=POST_HEADERS)
    assert r.status_code == 202
    with db.connect() as conn:
        job = conn.execute("SELECT protect, site_protect_default FROM archive_jobs "
                           "WHERE url = ?", ("https://pub.test/x",)).fetchone()
    assert job["protect"] == 0 and job["site_protect_default"] == 0


def _admin_client():
    with db.connect() as conn:
        uid = db.create_user(conn, "admin2@test.co", auth.hash_password("adminpass123"), role="admin")
        token = auth.issue_session(conn, uid)
    c = TestClient(web_app.app)
    c.cookies.set(config.SESSION_COOKIE, token)
    return c


# ---- 보호 해소 순서 ----


def test_protect_resolution_order(tmp_db):
    pid, _ = _seed_snapshot("https://a.test/p1", shareable=False)
    with db.connect() as conn:
        page = db.get_page(conn, "https://a.test/p1")
        site_id = page["site_id"]
        # 시스템 기본 보호 ON, 사이트 기본 없음 → 보호
        db.set_setting(conn, db.CLUSTER_PROTECT_DEFAULT_KEY, "on")
        db.set_site_cluster_protect_default(conn, site_id, True)
        db.set_page_cluster_protect(conn, page["id"], None)
        assert db.resolve_page_cluster_protected(conn, page["id"]) is True
        # 사이트 기본 OFF → 전송 허용
        db.set_site_cluster_protect_default(conn, site_id, False)
        assert db.resolve_page_cluster_protected(conn, page["id"]) is False
        # 페이지 명시 ON 이 사이트보다 우선
        db.set_page_cluster_protect(conn, page["id"], True)
        assert db.resolve_page_cluster_protected(conn, page["id"]) is True
