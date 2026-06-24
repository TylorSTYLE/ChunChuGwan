"""클러스터(federation) 엔드포인트 (/api/cluster) — 시스템 키 게이트, A 측 서빙.

`/api/v1`(개인 키)·`/api/web`(세션)과 **분리된** 머신-투-머신 채널이다. 매 요청
서버측에서 시스템 키(owner=NULL) 유효성 + 방향 권한을 재검증한다 — B 의 클라이언트측
판단은 신뢰하지 않는다. 통신은 항상 B 가 개시한다(이 라우터는 A 가 응답만).

- 인증: `Authorization: Bearer <시스템 키>`. 개인 키(owner 값)·세션은 거부.
- 방향 권한(키 소유자=B 기준): can_cluster_send=B 가 push 가능,
  can_cluster_receive=B 가 pull 가능. 미허가/폐기는 거부(403/401).
- 인증 실패는 IP 별 인증 보호(`auth_throttle` 의 `cluster_ip` 버킷)로 무차별 대입 방어.
- 전송 계층 보호는 리버스 프록시 HTTPS — URL/쿼리스트링에 키·민감정보 금지.

스냅샷 전송(pull 목록·envelope·blob, push 수신)은 후속 단계에서 더한다.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from .. import auth, cluster, config, db

router = APIRouter(prefix="/api/cluster")


def _cluster_throttle(ip: str) -> None:
    """클러스터 인증 실패에 IP 별 인증 보호 — 버킷 분리(`cluster_ip`), 한도 초과 429.

    카운트는 인증 핸들러 주 트랜잭션과 분리된 별도 conn 에서 한다(실패로 401/429 를
    던져 롤백돼도 시도 횟수가 남도록 — authentication.md 규칙). 로그인 IP 한도·창 재사용.
    """
    with db.connect() as conn:
        if not db.auth_throttle_enabled(conn):
            return
        s = db.auth_throttle_settings(conn)
        allowed, retry = db.throttle_hit(
            conn, "cluster_ip", ip, s["login_ip_limit"], s["login_window_minutes"] * 60
        )
    if not allowed:
        raise HTTPException(
            429, "요청이 너무 많습니다 — 잠시 후 다시 시도하세요",
            headers={"Retry-After": str(retry)},
        )


def _cluster_auth(request: Request) -> None:
    """클러스터 게이트 — 시스템 키(owner=NULL) + 클러스터 권한을 매 요청 재검증.

    유효 키 행을 request.state.cluster_key 에 적재한다. `/api/v1` 의 개인 키 게이트와
    달리 AUTH 비활성 우회가 없다 — 클러스터는 명시 발급 키로만 동작하는 옵트인 채널이라
    항상 키를 강제한다. 인증 실패는 IP 별 인증 보호 후 401.
    """
    request.state.cluster_key = None
    token = _extract_token(request)
    ip = request.client.host if request.client is not None else "unknown"
    key = None
    with db.connect() as conn:
        resolved = auth.resolve_api_key(conn, token) if token else None
        # 시스템 키 전용(owner=NULL) + 클러스터 권한이 하나라도 있어야 한다.
        if (
            resolved is not None
            and resolved["owner_user_id"] is None
            and (resolved["can_cluster_send"] or resolved["can_cluster_receive"])
        ):
            db.touch_api_key(conn, resolved["id"])
            key = dict(resolved)
    if key is not None:
        request.state.cluster_key = key
        return  # 성공 — 정상 트래픽은 throttle 을 건드리지 않는다.
    _cluster_throttle(ip)
    raise HTTPException(
        401, "유효한 클러스터 시스템 키가 필요합니다",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _extract_token(request: Request) -> str:
    """Authorization: Bearer 또는 X-API-Key 헤더에서 키 원문 추출 (api_routes 와 동일 규약)."""
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def require_cluster_send(request: Request) -> dict:
    """B 가 push(보내기) 하려면 키에 can_cluster_send 가 있어야 한다 (A 측 수신 게이트)."""
    key = request.state.cluster_key
    if not key["can_cluster_send"]:
        raise HTTPException(403, "이 키에는 클러스터 받기(수신) 권한이 없습니다")
    return key


def require_cluster_receive(request: Request) -> dict:
    """B 가 pull(받기) 하려면 키에 can_cluster_receive 가 있어야 한다 (A 측 서빙 게이트)."""
    key = request.state.cluster_key
    if not key["can_cluster_receive"]:
        raise HTTPException(403, "이 키에는 클러스터 보내기(송신) 권한이 없습니다")
    return key


@router.get("/status", dependencies=[Depends(_cluster_auth)])
def cluster_status(request: Request) -> dict:
    """핸드셰이크·주기 권한 갱신 — 이 노드의 신원 + 이 키의 방향 권한·활성 상태.

    B 는 등록 시 node_id·protocol_version 을 받고, 주기 조정마다 키 활성·방향 권한을
    다시 확인한다(폐기 시 키 조회 자체가 401 로 떨어져 영구 중단 신호가 된다).
    """
    key = request.state.cluster_key
    with db.connect() as conn:
        identity = cluster.node_identity(conn)
    identity["key"] = {
        "active": True,
        "can_cluster_send": bool(key["can_cluster_send"]),
        "can_cluster_receive": bool(key["can_cluster_receive"]),
    }
    return identity


# ── 받기(pull) — B 가 can_cluster_receive 키로 A 의 공유분을 가져간다 ──


@router.get("/snapshots", dependencies=[Depends(_cluster_auth)])
def cluster_snapshots(
    request: Request,
    after: int = Query(0, ge=0),
    limit: int = Query(config.CLUSTER_SYNC_BATCH_MAX, ge=1),
) -> dict:
    """송신 커서 이후 공유 가능한 스냅샷 목록(단조 id). 출처측이 보호를 강제한다."""
    require_cluster_receive(request)
    limit = min(limit, config.CLUSTER_SYNC_BATCH_MAX)
    with db.connect() as conn:
        rows = db.list_shareable_snapshots_after(conn, after, limit)
        node_id = db.cluster_node_id(conn)
    return {
        "node_id": node_id,
        "snapshots": [
            {"id": r["id"], "page_url": r["page_url"],
             "content_hash": r["content_hash"], "taken_at": r["taken_at"]}
            for r in rows
        ],
    }


@router.get("/snapshots/{snapshot_id}", dependencies=[Depends(_cluster_auth)])
def cluster_snapshot_envelope(request: Request, snapshot_id: int) -> dict:
    """스냅샷 1건 envelope — 보호분/수신분/없는 id 는 404(존재 비노출)."""
    require_cluster_receive(request)
    with db.connect() as conn:
        if not db.snapshot_shareable(conn, snapshot_id):
            raise HTTPException(404, "스냅샷 없음")
        envelope = cluster.serialize_snapshot(conn, snapshot_id)
    if envelope is None:
        raise HTTPException(404, "스냅샷 없음")
    return envelope


@router.get("/blobs/{kind}/{name}", dependencies=[Depends(_cluster_auth)])
def cluster_blob_get(request: Request, kind: str, name: str) -> Response:
    """공유 CAS 블롭 1개 서빙(받기) — 콘텐츠 주소(sha256) 이름 검증, 첨부 다운로드."""
    require_cluster_receive(request)
    payload = cluster.read_cas_blob(kind, name)
    if payload is None:
        raise HTTPException(404, "블롭 없음")
    return Response(content=payload, media_type="application/octet-stream")


# ── 보내기(push) — B 가 can_cluster_send 키로 A 에 적재를 요청한다(A 가 수신) ──


class NegotiateReq(BaseModel):
    blobs: list[dict]  # [{kind, name}]


def _require_not_busy() -> None:
    """수신 백프레셔 — 바쁘면 429 Retry-After (보내는 쪽이 백오프하도록)."""
    with db.connect() as conn:
        retry = cluster.is_busy(conn)
    if retry:
        raise HTTPException(429, "수신 측이 바쁩니다 — 잠시 후 다시 시도하세요",
                            headers={"Retry-After": str(retry)})


@router.post("/negotiate", dependencies=[Depends(_cluster_auth)])
def cluster_negotiate(request: Request, body: NegotiateReq) -> dict:
    """보내는 쪽이 가진 (kind, name) 중 A 가 **없는 것만** 돌려준다(없는 것만 업로드)."""
    require_cluster_send(request)
    _require_not_busy()
    missing = []
    for b in body.blobs:
        kind, name = b.get("kind"), b.get("name")
        if not isinstance(kind, str) or not isinstance(name, str):
            continue
        if cluster.blob_path(kind, name) is None:
            continue  # 잘못된 이름은 협상에서 제외
        if not cluster._has_blob(kind, name):
            missing.append({"kind": kind, "name": name})
    return {"missing": missing}


@router.post("/blobs/{kind}/{name}", dependencies=[Depends(_cluster_auth)])
def cluster_blob_put(
    request: Request, kind: str, name: str,
    payload: bytes = Body(default=b"", media_type="application/octet-stream"),
) -> dict:
    """CAS 블롭 1개 수신 — sha256 검증 후 저장(보내기). 상한 초과·불일치는 거부."""
    require_cluster_send(request)
    _require_not_busy()
    if len(payload) > config.CLUSTER_BLOB_MAX_BYTES:
        raise HTTPException(413, "블롭이 너무 큽니다")
    try:
        cluster.store_cas_blob(kind, name, payload)
    except cluster.IntegrityError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/snapshots", dependencies=[Depends(_cluster_auth)])
def cluster_snapshot_receive(request: Request, envelope: dict = Body(...)) -> dict:
    """스냅샷 envelope 수신·적재(보내기) — 코어 경유, 중복은 스킵. 바쁘면 429."""
    require_cluster_send(request)
    _require_not_busy()
    if envelope.get("protocol_version") != config.CLUSTER_PROTOCOL_VERSION:
        raise HTTPException(409, "프로토콜 버전이 호환되지 않습니다")
    origin = envelope.get("origin_node_id")
    if not isinstance(origin, str) or not origin:
        raise HTTPException(400, "출처 노드 식별자가 없습니다")
    with db.connect() as conn:
        # 보낸 쪽으로 되돌려보내기 방지 — origin 이 우리 자신이면 거부.
        if origin == db.cluster_node_id(conn):
            raise HTTPException(409, "자기 출처 아카이브는 수신하지 않습니다")
        try:
            snap_id = cluster.import_snapshot(conn, envelope, peer_node_id=origin)
        except cluster.IntegrityError as e:
            raise HTTPException(400, str(e))
        except (KeyError, TypeError) as e:
            raise HTTPException(400, f"envelope 형식 오류: {e}")
        if snap_id is not None:
            # 수신 신규 적재만 기록 (중복은 무기록). source='cluster' + 출처 피어.
            cluster.log_received(conn, origin, envelope, snap_id)
    return {"status": "new" if snap_id is not None else "duplicate"}
