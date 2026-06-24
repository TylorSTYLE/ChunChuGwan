"""클러스터(federation) — 인증 게이트·핸드셰이크·관리 라우트·조정 루프.

전송 본체(스냅샷 push/pull)는 후속 단계에서 별도 검증한다. 여기서는 노드 식별·
시스템 키 게이트·피어 등록 가드·방향 권한·상태 전이를 다룬다.
"""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, cluster, cluster_sync, config, db
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
    # 조정 루프의 in-memory 백오프 초기화 (테스트 간 격리)
    cluster_sync._backoff_until.clear()
    cluster_sync._backoff_fails.clear()
    # 시드 사용자 — 사용자 0명이면 first_run 게이트가 /api 를 전부 401 로 막는다.
    with db.connect() as conn:
        db.create_user(conn, "seed@test.co", auth.hash_password("seedpass123"), role="admin")
    yield


def _admin_client():
    with db.connect() as conn:
        uid = db.create_user(conn, "admin@test.co", auth.hash_password("adminpass123"), role="admin")
        token = auth.issue_session(conn, uid)
    c = TestClient(web_app.app)
    c.cookies.set(config.SESSION_COOKIE, token)
    return c


def _issue_cluster_key(*, send=False, receive=False):
    with db.connect() as conn:
        return auth.issue_api_key(
            conn, "peer", can_view=False, can_archive=False, created_by=None,
            ttl_seconds=None, owner_user_id=None,
            can_cluster_send=send, can_cluster_receive=receive,
        )


# ---- 노드 식별 ----


def test_node_id_persists(tmp_db):
    with db.connect() as conn:
        first = db.cluster_node_id(conn)
        again = db.cluster_node_id(conn)
    assert first == again and len(first) == 36


# ---- /api/cluster/status 게이트 ----


def test_status_requires_cluster_key(tmp_db):
    c = TestClient(web_app.app)
    assert c.get("/api/cluster/status").status_code == 401
    assert c.get("/api/cluster/status", headers={"Authorization": "Bearer wccg_bogus"}).status_code == 401


def test_status_rejects_personal_key(tmp_db):
    # owner 가 있는 개인 키는 클러스터 게이트가 거부한다.
    with db.connect() as conn:
        uid = db.create_user(conn, "u@test.co", auth.hash_password("pw123456789"), role="admin")
        tok = auth.issue_api_key(
            conn, "ext", can_view=True, can_archive=False, created_by=None,
            ttl_seconds=None, owner_user_id=uid,
        )
    c = TestClient(web_app.app)
    assert c.get("/api/cluster/status", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_status_rejects_key_without_cluster_perm(tmp_db):
    with db.connect() as conn:
        tok = auth.issue_api_key(
            conn, "sys", can_view=True, can_archive=False, created_by=None,
            ttl_seconds=None, owner_user_id=None,
        )
    c = TestClient(web_app.app)
    assert c.get("/api/cluster/status", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_status_returns_identity_and_perms(tmp_db):
    tok = _issue_cluster_key(receive=True)
    with db.connect() as conn:
        nid = db.cluster_node_id(conn)
    c = TestClient(web_app.app)
    r = c.get("/api/cluster/status", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    body = r.json()
    assert body["node_id"] == nid
    assert body["protocol_version"] == config.CLUSTER_PROTOCOL_VERSION
    assert body["key"] == {"active": True, "can_cluster_send": False, "can_cluster_receive": True}


# ---- 핸드셰이크 가드 (register_peer) ----


def _fake_status(node_id, perms=None):
    return {
        "node_id": node_id,
        "display_name": "Peer " + node_id,
        "protocol_version": config.CLUSTER_PROTOCOL_VERSION,
        "key": perms or {"can_cluster_send": True, "can_cluster_receive": True},
    }


def test_register_self_connect_rejected(tmp_db, monkeypatch):
    with db.connect() as conn:
        my = db.cluster_node_id(conn)
    monkeypatch.setattr(cluster, "fetch_status", lambda b, k: _fake_status(my))
    with db.connect() as conn:
        with pytest.raises(cluster.ClusterError, match="자기 자신"):
            cluster.register_peer(conn, base_url="https://self.example",
                                  api_key="wccg_x", send_enabled=False, receive_enabled=True)


def test_register_and_duplicate_rejected(tmp_db, monkeypatch):
    monkeypatch.setattr(cluster, "fetch_status", lambda b, k: _fake_status("peer-AAA"))
    with db.connect() as conn:
        pid = cluster.register_peer(conn, base_url="https://a.example/",
                                    api_key="wccg_secret", send_enabled=False, receive_enabled=True)
        peer = db.get_cluster_peer(conn, pid)
        assert peer["peer_node_id"] == "peer-AAA"
        assert peer["base_url"] == "https://a.example"  # 끝 슬래시 제거
        assert peer["status"] == "active"
        # 키는 암호문으로 저장되고 복호화 가능 (평문 아님)
        assert peer["api_key_enc"] != "wccg_secret"
        assert cluster.peer_api_key(peer) == "wccg_secret"
        with pytest.raises(cluster.ClusterError, match="이미 등록"):
            cluster.register_peer(conn, base_url="https://a.example",
                                  api_key="wccg_secret", send_enabled=False, receive_enabled=True)


def test_register_protocol_mismatch(tmp_db, monkeypatch):
    def bad(b, k):
        s = _fake_status("peer-OLD")
        s["protocol_version"] = 999
        return s
    # fetch_status 자체가 버전 검증을 하므로 실제 fetch_status 의 검증 경로를 흉내낸다
    monkeypatch.setattr(cluster, "fetch_status",
                        lambda b, k: (_ for _ in ()).throw(cluster.ProtocolMismatch("불호환")))
    with db.connect() as conn:
        with pytest.raises(cluster.ProtocolMismatch):
            cluster.register_peer(conn, base_url="https://old.example",
                                  api_key="wccg_x", send_enabled=False, receive_enabled=True)


def test_register_requires_secret_key(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "")
    with db.connect() as conn:
        with pytest.raises(cluster.ClusterError, match="WCCG_SECRET_KEY"):
            cluster.register_peer(conn, base_url="https://a.example",
                                  api_key="wccg_x", send_enabled=False, receive_enabled=True)


def test_normalize_base_url_rejects_bad_scheme(tmp_db):
    with pytest.raises(cluster.ClusterError):
        cluster.normalize_base_url("ftp://x.example")
    assert cluster.normalize_base_url("https://x.example/ccg/") == "https://x.example/ccg"


# ---- 관리 라우트 ----


def test_cluster_routes_require_manage_system(tmp_db):
    with db.connect() as conn:
        uid = db.create_user(conn, "arch@test.co", auth.hash_password("pw123456789"), role="archiver")
        token = auth.issue_session(conn, uid)
    c = TestClient(web_app.app)
    c.cookies.set(config.SESSION_COOKIE, token)
    assert c.get("/api/web/system/cluster").status_code == 403


def test_cluster_management_flow(tmp_db, monkeypatch):
    c = _admin_client()
    r = c.get("/api/web/system/cluster")
    assert r.status_code == 200 and r.json()["secret_configured"] is True

    # 디스플레이 이름
    assert c.post("/api/web/system/cluster/node", json={"display_name": "우리노드"},
                  headers=POST_HEADERS).status_code == 200
    assert c.get("/api/web/system/cluster").json()["node"]["display_name"] == "우리노드"

    # 동기화 설정 — 범위 밖 주기는 클램핑
    c.post("/api/web/system/cluster/sync-settings",
           json={"sync_interval_seconds": 1, "protect_default": False}, headers=POST_HEADERS)
    g = c.get("/api/web/system/cluster").json()
    assert g["sync_interval_seconds"] == config.CLUSTER_SYNC_INTERVAL_SECONDS_MIN
    assert g["protect_default"] is False

    # 피어 등록 (핸드셰이크 모킹) → 방향 변경 → 삭제
    monkeypatch.setattr(cluster, "fetch_status", lambda b, k: _fake_status("peer-Z"))
    r = c.post("/api/web/system/cluster/peers",
               json={"base_url": "https://z.example", "api_key": "wccg_z", "receive_enabled": True},
               headers=POST_HEADERS)
    assert r.status_code == 200
    pid = r.json()["peer"]["id"]
    # 암호화 키는 응답에 절대 노출되지 않는다
    assert "api_key_enc" not in r.json()["peer"]

    assert c.post(f"/api/web/system/cluster/peers/{pid}",
                  json={"send_enabled": True, "receive_enabled": False},
                  headers=POST_HEADERS).status_code == 200
    peer = c.get("/api/web/system/cluster").json()["peers"][0]
    assert peer["send_enabled"] is True and peer["receive_enabled"] is False

    assert c.post(f"/api/web/system/cluster/peers/{pid}/delete",
                  headers=POST_HEADERS).status_code == 200
    assert c.get("/api/web/system/cluster").json()["peers"] == []


# ---- 조정 루프 상태 전이 ----


def _make_peer(node_id="peer-R", send=False, receive=True):
    with db.connect() as conn:
        from chunchugwan import crypto
        return db.create_cluster_peer(
            conn, peer_node_id=node_id, display_name="", base_url="https://r.example",
            api_key_enc=crypto.encrypt("wccg_key"),
            send_enabled=send, receive_enabled=receive,
        )


def test_reconcile_marks_revoked_on_auth_reject(tmp_db, monkeypatch):
    pid = _make_peer()
    monkeypatch.setattr(cluster, "fetch_status",
                        lambda b, k: (_ for _ in ()).throw(cluster.PeerAuthRejected("폐기")))
    cluster_sync.reconcile_peer(pid, interval=60)
    with db.connect() as conn:
        assert db.get_cluster_peer(conn, pid)["status"] == "revoked"
    # 폐기된 피어는 이후 run_due 가 폴링하지 않는다
    called = []
    monkeypatch.setattr(cluster, "fetch_status", lambda b, k: called.append(1) or _fake_status("peer-R"))
    cluster_sync.run_due()
    assert called == []


def test_reconcile_degraded_on_unavailable(tmp_db, monkeypatch):
    pid = _make_peer()
    monkeypatch.setattr(cluster, "fetch_status",
                        lambda b, k: (_ for _ in ()).throw(cluster.PeerUnavailable("타임아웃")))
    cluster_sync.reconcile_peer(pid, interval=60)
    with db.connect() as conn:
        peer = db.get_cluster_peer(conn, pid)
    assert peer["status"] == "degraded" and "타임아웃" in peer["last_error"]


def test_reconcile_active_updates_display_name(tmp_db, monkeypatch):
    pid = _make_peer(node_id="peer-R")
    monkeypatch.setattr(cluster, "fetch_status",
                        lambda b, k: {**_fake_status("peer-R"), "display_name": "최신이름"})
    cluster_sync.reconcile_peer(pid, interval=60)
    with db.connect() as conn:
        peer = db.get_cluster_peer(conn, pid)
    assert peer["status"] == "active" and peer["display_name"] == "최신이름"
