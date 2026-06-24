"""클러스터 조정 루프 — 피어별 주기 사이클(권한 갱신 → pull 델타 → push 델타).

**B(연결을 등록한 쪽)에서만** 동작한다. 한 사이클에서 피어별로 순서대로:
  1) 상태/권한 갱신: A 의 상태 엔드포인트 조회 → 키 활성·방향 권한. 키 폐기(401/403)면
     이 피어 폴링 영구 중단(revoked). 5xx/타임아웃 등 일시 오류는 종료가 아니라 백오프 재시도.
  2) 받기(허용 시): 커서 교환으로 "내 마지막 수신 커서 이후 신규"만 pull (전송 단계에서).
  3) 보내기(허용 시): 마지막 송신 커서 이후의 공유 가능(보호 OFF) 페이지만 push (전송 단계에서).

실제 수행 조건은 (A 키 방향 권한) AND (B 연결 방향 설정)이 모두 켜져야 한다.
통신은 항상 B 가 개시한다(pull/push 모두). scheduler 옆 백그라운드 스레드로 돈다.
"""
from __future__ import annotations

import logging
import threading
import time

from . import cluster, config, crypto, db

logger = logging.getLogger(__name__)

# 일시 오류(degraded) 피어의 in-memory 백오프 — {peer_id: 다음 시도 monotonic 시각}.
# 연속 실패마다 대기를 늘려 다운된 피어를 매 사이클 두드리지 않는다(상한 1시간).
# 영속 상태가 아니라 프로세스 재기동 시 초기화된다(다음 사이클에 곧 재평가).
_backoff_until: dict[int, float] = {}
_backoff_fails: dict[int, int] = {}
_BACKOFF_CAP_SECONDS = 3600

# 루프가 폴링하는 상태 — revoked(키 폐기)·error(프로토콜 불호환)는 운영자 개입 전까지 제외.
_POLLABLE = ("pending", "active", "degraded")


def _note_failure(peer_id: int, interval: int) -> None:
    """연속 실패 카운트 증가 + 다음 시도 시각 백오프(지수, 상한)."""
    fails = _backoff_fails.get(peer_id, 0) + 1
    _backoff_fails[peer_id] = fails
    delay = min(interval * (2 ** (fails - 1)), _BACKOFF_CAP_SECONDS)
    _backoff_until[peer_id] = time.monotonic() + delay


def _clear_failure(peer_id: int) -> None:
    """성공 시 백오프 초기화."""
    _backoff_fails.pop(peer_id, None)
    _backoff_until.pop(peer_id, None)


def reconcile_peer(peer_id: int, *, interval: int) -> None:
    """피어 1개 1사이클 — 상태/권한 갱신 후 허용 방향으로 pull/push 델타.

    네트워크 I/O 는 DB 커넥션 밖에서 한다(긴 I/O 가 커넥션을 잡지 않게). 상태 전이는
    별도 커넥션에서 기록한다. 예외는 호출부(run_due)가 잡아 스레드를 살린다.
    """
    with db.connect() as conn:
        peer = db.get_cluster_peer(conn, peer_id)
    if peer is None or peer["status"] not in _POLLABLE:
        return

    # 1) 상태/권한 갱신 — 키 복호화 후 A 의 상태 엔드포인트 조회.
    try:
        api_key = cluster.peer_api_key(peer)
    except (crypto.SecretKeyMissing, crypto.SecretDecryptError) as e:
        _mark(peer_id, "error", str(e))
        return
    try:
        status = cluster.fetch_status(peer["base_url"], api_key)
    except cluster.PeerAuthRejected as e:
        # 폐기 — 영구 중단(운영자가 재등록/재발급 전까지 폴링 제외).
        _mark(peer_id, "revoked", str(e))
        _clear_failure(peer_id)
        logger.info("클러스터 피어 폐기로 폴링 중단: %s", peer["base_url"])
        return
    except cluster.ProtocolMismatch as e:
        _mark(peer_id, "error", str(e))
        _clear_failure(peer_id)
        return
    except cluster.ClusterError as e:
        # 일시 오류(네트워크·5xx·응답 손상) — 종료가 아니라 백오프 재시도.
        _mark(peer_id, "degraded", str(e))
        _note_failure(peer_id, interval)
        return

    # 성공 — 디스플레이 이름 캐시 갱신 + active 복귀.
    key_perms = status.get("key") or {}
    with db.connect() as conn:
        db.update_cluster_peer_meta(
            conn, peer_id, display_name=str(status.get("display_name") or "")
        )
        db.set_cluster_peer_status(conn, peer_id, "active", last_error=None)
    _clear_failure(peer_id)

    # 실제 수행 조건: (A 키 권한) AND (B 연결 설정) 모두 켜진 방향만.
    can_receive = bool(peer["receive_enabled"]) and bool(key_perms.get("can_cluster_receive"))
    can_send = bool(peer["send_enabled"]) and bool(key_perms.get("can_cluster_send"))

    # 2) 받기(pull) / 3) 보내기(push) — 허용 방향만. 전송 단위는 스냅샷 1건(원자).
    # 전송 실패(네트워크·피어 오류)는 사이클을 깨지 않고 다음 사이클에 재시도한다
    # (상태 점검은 이미 성공했으므로 active 유지 — 일시 전송 오류로 폐기 판정하지 않는다).
    peer_node_id = peer["peer_node_id"]
    if can_receive:
        try:
            _pull_delta(peer_id, peer["base_url"], api_key, peer_node_id)
        except cluster.ClusterError as e:
            logger.warning("클러스터 받기 중단(#%d): %s", peer_id, e)
    if can_send:
        try:
            _push_delta(peer_id, peer["base_url"], api_key)
        except cluster.ClusterError as e:
            logger.warning("클러스터 보내기 중단(#%d): %s", peer_id, e)


def _pace() -> None:
    """전송 건당 최소 간격 — 대상 서버에 양보(크롤 delay 와 같은 철학)."""
    time.sleep(config.CLUSTER_SEND_MIN_INTERVAL_SECONDS)


def _pull_delta(peer_id: int, base_url: str, api_key: str, peer_node_id: str) -> None:
    """받기 델타 — 내 수신 커서 이후 피어의 신규 공유분만 pull (사이클당 배치 상한).

    건당 원자: 블롭 협상→적재→커서 전진. 중단 시 진행 중 1건만 손실되고 커서는 성공한
    데까지만 올라 다음 사이클에 이어진다(부분 적재 없음). 피어가 바쁘면(429) 이번 사이클 중단.
    """
    with db.connect() as conn:
        cursor = db.get_cluster_peer(conn, peer_id)["receive_cursor"]
    try:
        items = cluster.pull_list(base_url, api_key, after=cursor,
                                  limit=config.CLUSTER_SYNC_BATCH_MAX)
    except cluster.PeerBusy:
        return
    for it in items:
        snap_id = it["id"]
        try:
            envelope = cluster.pull_envelope(base_url, api_key, snap_id)
            for kind, name in cluster.missing_blobs(envelope):
                payload = cluster.pull_blob(base_url, api_key, kind, name)
                cluster.store_cas_blob(kind, name, payload)
            with db.connect() as conn:
                new_id = cluster.import_snapshot(conn, envelope, peer_node_id=peer_node_id)
                if new_id is not None:
                    cluster.log_received(conn, peer_node_id, envelope, new_id)
                db.advance_cluster_peer_cursor(conn, peer_id, direction="receive", cursor=snap_id)
        except cluster.PeerBusy:
            break  # 피어 백프레셔 — 다음 사이클에 재시도(커서 미전진)
        except cluster.ClusterError as e:
            # 진행 중 1건 실패 — 커서 미전진으로 다음 사이클 재시도(부분 적재 없음).
            logger.warning("클러스터 받기 실패(#%d snap %s): %s", peer_id, snap_id, e)
            break
        _pace()


def _push_delta(peer_id: int, base_url: str, api_key: str) -> None:
    """보내기 델타 — 내 송신 커서 이후의 공유 가능(보호 OFF·로컬 생성) 스냅샷만 push.

    출처측(=이 노드)이 보호를 전송 전 강제한다(list_shareable_snapshots_after). 건당
    협상→없는 블롭만 업로드→envelope 적재 요청→커서 전진. 429·오류는 이번 사이클 중단.
    """
    with db.connect() as conn:
        cursor = db.get_cluster_peer(conn, peer_id)["send_cursor"]
        rows = db.list_shareable_snapshots_after(conn, cursor, config.CLUSTER_SYNC_BATCH_MAX)
    for r in rows:
        snap_id = r["id"]
        try:
            with db.connect() as conn:
                envelope = cluster.serialize_snapshot(conn, snap_id)
            if envelope is None:
                _advance_send(peer_id, snap_id)  # 사라진 스냅샷 — 커서만 전진
                continue
            missing = cluster.push_negotiate(base_url, api_key,
                                             cluster._envelope_blobs(envelope))
            for kind, name in missing:
                payload = cluster.read_cas_blob(kind, name)
                if payload is None:
                    raise cluster.ClusterError(f"로컬 CAS 블롭 없음: {name}")
                cluster.push_blob(base_url, api_key, kind, name, payload)
            status = cluster.push_snapshot(base_url, api_key, envelope)
            with db.connect() as conn:
                if status == "new":
                    _log_sent(conn, peer_id, envelope)
                db.advance_cluster_peer_cursor(conn, peer_id, direction="send", cursor=snap_id)
        except cluster.PeerBusy:
            break
        except cluster.ClusterError as e:
            logger.warning("클러스터 보내기 실패(#%d snap %s): %s", peer_id, snap_id, e)
            break
        _pace()


def _advance_send(peer_id: int, cursor: int) -> None:
    with db.connect() as conn:
        db.advance_cluster_peer_cursor(conn, peer_id, direction="send", cursor=cursor)


def _log_sent(conn, peer_id: int, envelope: dict) -> None:
    """전송 신규를 audit_logs 에 기록 — 어떤 아카이브를 어느 피어로 보냈는지(append-only)."""
    peer = db.get_cluster_peer(conn, peer_id)
    page = envelope.get("page") or {}
    url = page.get("url") or ""
    name = (peer["display_name"] or peer["base_url"]) if peer else str(peer_id)
    db.insert_audit_log(
        conn,
        created_at=db._utcnow(),
        actor="cluster",
        action="cluster_send",
        target=url,
        message=f"클러스터 전송: {url} → {name}",
    )


def _mark(peer_id: int, status: str, last_error: str | None) -> None:
    """피어 상태·마지막 오류 기록 (자체 커넥션)."""
    with db.connect() as conn:
        db.set_cluster_peer_status(conn, peer_id, status, last_error=last_error)


def run_due() -> None:
    """기한이 된 피어를 한 번씩 조정 — cron/단발 실행 진입점(스레드 밖에서도 호출 가능).

    이전(마이그레이션)·스토리지 마이그레이션 중에는 쓰기가 멈추므로 전부 건너뛴다.
    백오프 중인 피어는 시도 시각 전이면 건너뛴다.
    """
    with db.connect() as conn:
        if db.writes_paused(conn):
            return
        interval = db.cluster_sync_interval_seconds(conn)
        peers = [p["id"] for p in db.list_cluster_peers(conn) if p["status"] in _POLLABLE]
    now = time.monotonic()
    for peer_id in peers:
        if _backoff_until.get(peer_id, 0) > now:
            continue
        try:
            reconcile_peer(peer_id, interval=interval)
        except Exception:
            logger.exception("클러스터 피어 조정 실패: #%d", peer_id)


def run_loop(stop: threading.Event, *, poll_seconds: int | None = None) -> None:
    """stop 이 설정될 때까지 주기적으로 run_due 실행 (worker/serve 백그라운드 스레드용).

    폴링 간격은 시스템 설정(cluster_sync_interval_seconds)을 첫 구동 시 읽되, 너무
    촘촘한 폴링을 막기 위해 최소 간격 이상으로 잔다. 사이클 내 피어별 백오프는 별도.
    """
    if poll_seconds is None:
        with db.connect() as conn:
            poll_seconds = db.cluster_sync_interval_seconds(conn)
    poll_seconds = max(poll_seconds, config.CLUSTER_SYNC_INTERVAL_SECONDS_MIN)
    logger.info("클러스터 조정 루프 시작 (폴링 %ds)", poll_seconds)
    while not stop.wait(poll_seconds):
        try:
            run_due()
        except Exception:
            logger.exception("클러스터 조정 폴링 실패")
    logger.info("클러스터 조정 루프 종료")
