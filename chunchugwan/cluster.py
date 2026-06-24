"""클러스터(federation) 코어 — 노드 식별·피어 연결·핸드셰이크·HTTP 클라이언트.

여러 춘추관 인스턴스가 아카이브를 선택적으로 주고받는다. **통신은 항상 B(연결을
등록한 쪽)가 개시**한다 — 전송=push, 받기=pull. A 는 B 로 능동 연결하지 않는다.

- 노드 식별: 영속 UUID(`db.cluster_node_id`)로만 매칭한다. 디스플레이 이름은
  표시용일 뿐 신뢰·식별 근거가 아니다.
- 피어 연결(B 측)은 A 의 주소 + A 발급 시스템 키 + 방향(보내기/받기)을 등록한다.
  저장 키는 평문 금지 — `crypto.encrypt` 대칭 암호화(원칙 6 예외, export 제외).
- 피어 base_url 은 신뢰 운영자 설정(캡처 대상 아님)이라 netcheck 캡처 게이트(원칙 7)
  대상이 아니다 — 단 스킴·형식 정규화·검증은 한다.
- 전송 계층 보호는 리버스 프록시 HTTPS 에 의존한다(키는 헤더로만, URL/쿼리 금지).

스냅샷 1건 직렬화/적재(전송 본체)는 후속 단계에서 이 모듈에 더한다.
"""
from __future__ import annotations

import sqlite3
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import config, crypto, db

# 핸드셰이크/상태 엔드포인트 경로 (B 가 A 에 붙는 경로 — 라우터 prefix 와 일치)
STATUS_PATH = "/api/cluster/status"


class ClusterError(RuntimeError):
    """클러스터 연결·핸드셰이크 일반 오류 (사용자에게 메시지를 그대로 보인다)."""


class ProtocolMismatch(ClusterError):
    """피어의 프로토콜 버전이 호환되지 않는다 (운영자 개입 전까지 종료)."""


class PeerAuthRejected(ClusterError):
    """피어가 키를 거부했다 (401/403) — 폐기로 보고 이 피어 폴링을 영구 중단한다."""


class PeerUnavailable(ClusterError):
    """피어 네트워크 오류·5xx — 일시 오류이므로 종료가 아니라 백오프 재시도한다."""


def node_identity(conn: sqlite3.Connection) -> dict:
    """이 노드의 핸드셰이크 신원 — UUID·표시 이름·프로토콜 버전.

    A 의 상태 엔드포인트가 이 값을 내려주고, B 는 등록 시 peer_node_id 로 받는다.
    """
    return {
        "node_id": db.cluster_node_id(conn),
        "display_name": db.cluster_display_name(conn),
        "protocol_version": config.CLUSTER_PROTOCOL_VERSION,
    }


def normalize_base_url(raw: str) -> str:
    """피어 base_url 정규화·검증 — http/https 만, 끝 슬래시 제거, 경로 보존.

    리버스 프록시 서브경로(`https://host/ccg`)도 허용하되 쿼리·프래그먼트는 버린다.
    캡처 대상이 아니므로 netcheck 게이트는 적용하지 않는다(신뢰 운영자 입력).
    """
    raw = (raw or "").strip()
    if not raw:
        raise ClusterError("피어 주소가 비어 있습니다")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ClusterError("피어 주소는 http 또는 https 여야 합니다")
    if not parts.netloc:
        raise ClusterError("피어 주소의 호스트가 올바르지 않습니다")
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _client() -> httpx.Client:
    """피어 호출용 HTTP 클라이언트 — 타임아웃 필수, 리다이렉트 미추적(엔드포인트 고정)."""
    return httpx.Client(
        timeout=config.CLUSTER_HTTP_TIMEOUT_SECONDS,
        follow_redirects=False,
    )


def _auth_headers(api_key: str) -> dict[str, str]:
    """피어 인증 헤더 — 키는 Authorization 헤더로만 보낸다(URL/쿼리 금지)."""
    return {"Authorization": f"Bearer {api_key}"}


def fetch_status(base_url: str, api_key: str) -> dict:
    """피어(A)의 상태 엔드포인트 조회 — 핸드셰이크·주기 권한 갱신 공용.

    반환: {node_id, display_name, protocol_version, key: {active, can_cluster_send,
    can_cluster_receive}}. 인증 실패(401/403)·프로토콜 불호환·네트워크 오류는
    ClusterError 계열로 올린다(호출부가 상태 판정에 쓴다).
    """
    url = base_url + STATUS_PATH
    try:
        with _client() as client:
            resp = client.get(url, headers=_auth_headers(api_key))
    except httpx.HTTPError as e:
        raise PeerUnavailable(f"피어에 연결할 수 없습니다: {e}") from e
    if resp.status_code in (401, 403):
        raise PeerAuthRejected("피어가 이 키를 거부했습니다 (권한 없음 또는 폐기됨)")
    if resp.status_code != 200:
        raise PeerUnavailable(f"피어 상태 조회 실패 (HTTP {resp.status_code})")
    try:
        data = resp.json()
    except ValueError as e:
        raise ClusterError("피어 응답을 해석할 수 없습니다") from e
    peer_version = data.get("protocol_version")
    if peer_version != config.CLUSTER_PROTOCOL_VERSION:
        raise ProtocolMismatch(
            f"프로토콜 버전이 호환되지 않습니다 (이 노드 {config.CLUSTER_PROTOCOL_VERSION}, "
            f"피어 {peer_version})"
        )
    if not data.get("node_id"):
        raise ClusterError("피어가 노드 식별자를 주지 않았습니다")
    return data


def register_peer(
    conn: sqlite3.Connection,
    *,
    base_url: str,
    api_key: str,
    send_enabled: bool,
    receive_enabled: bool,
) -> int:
    """피어 연결 등록(B 측) — 핸드셰이크로 A 의 UUID·버전을 받고 키를 암호화 저장.

    가드: WCCG_SECRET_KEY 미설정 거부, 자기 자신 연결 거부, 같은 피어 UUID 중복
    거부, 프로토콜 불호환 거부. 성공 시 peer id 반환. 모든 거부는 ClusterError.
    """
    if not crypto.is_configured():
        raise ClusterError(
            "WCCG_SECRET_KEY 가 설정되지 않아 클러스터 키를 안전하게 저장할 수 없습니다"
        )
    base_url = normalize_base_url(base_url)
    api_key = (api_key or "").strip()
    if not api_key:
        raise ClusterError("피어 발급 키가 비어 있습니다")
    if not (send_enabled or receive_enabled):
        raise ClusterError("보내기 또는 받기 중 하나 이상을 선택하세요")

    status = fetch_status(base_url, api_key)
    peer_node_id = status["node_id"]
    if peer_node_id == db.cluster_node_id(conn):
        raise ClusterError("자기 자신은 피어로 등록할 수 없습니다")
    if db.get_cluster_peer_by_node(conn, peer_node_id) is not None:
        raise ClusterError("이미 등록된 피어입니다 (같은 노드)")

    return db.create_cluster_peer(
        conn,
        peer_node_id=peer_node_id,
        display_name=str(status.get("display_name") or ""),
        base_url=base_url,
        api_key_enc=crypto.encrypt(api_key),
        send_enabled=send_enabled,
        receive_enabled=receive_enabled,
    )


def peer_api_key(peer: sqlite3.Row) -> str:
    """피어 저장 키 복호화 — 조정 루프·전송이 A 호출 시 쓴다.

    복호화 실패(키 부재·불일치)는 crypto 예외를 그대로 올린다(호출부가 error 상태로).
    """
    return crypto.decrypt(peer["api_key_enc"])
