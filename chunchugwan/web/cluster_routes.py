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

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import auth, cluster, db

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
