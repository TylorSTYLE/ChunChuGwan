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

import base64
import hashlib
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import config, crypto, db, documents, resources, storage

# 핸드셰이크/상태 엔드포인트 경로 (B 가 A 에 붙는 경로 — 라우터 prefix 와 일치)
STATUS_PATH = "/api/cluster/status"

# 전송 envelope 에 인라인으로 담는 스냅샷 디렉토리 파일 (공유 CAS 가 아닌 스냅샷 고유 파일).
# 큰 공유 자원·문서는 CAS 블롭으로 별도 협상해 "피어에 없는 것만" 전송한다.
_INTEGRITY_CHUNK = 1024 * 1024


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


class PeerBusy(ClusterError):
    """피어가 바빠 수신을 잠시 거부했다 (429) — 백오프 후 다음 사이클에 재시도."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"피어가 바쁩니다 (재시도 {retry_after}s)")
        self.retry_after = retry_after


class IntegrityError(ClusterError):
    """수신 블롭의 sha256 이 CAS 이름과 불일치 — 손상·변조로 보고 거부한다."""


# ── CAS 블롭 — 스냅샷 간 공유 자원(resource)·문서(document)는 콘텐츠 주소(sha256+ext) ──
# 전송은 "피어에 없는 sha256 만" 협상해 보낸다. kind 로 두 CAS 스토어를 구분한다.


def blob_path(kind: str, name: str) -> Path | None:
    """(kind, CAS 이름) → 저장 경로. 형식 검증 실패면 None(traversal·잘못된 이름 방어)."""
    if kind == "resource":
        return resources.resource_path(name) if resources.is_valid_name(name) else None
    if kind == "document":
        return documents.cas_path(name) if documents.is_valid_cas_name(name) else None
    return None


def _blob_sha(name: str) -> str:
    """CAS 이름(sha256+ext)에서 sha256 부분 추출."""
    return name.split(".", 1)[0]


def store_cas_blob(kind: str, name: str, payload: bytes) -> None:
    """수신 CAS 블롭을 검증 후 저장 — sha256 불일치는 거부(무결성), 이미 있으면 멱등."""
    path = blob_path(kind, name)
    if path is None:
        raise IntegrityError(f"잘못된 CAS 이름: {name!r}")
    if hashlib.sha256(payload).hexdigest() != _blob_sha(name):
        raise IntegrityError(f"CAS 블롭 sha256 불일치: {name!r}")
    store = config.blob_store()
    if not store.is_file(path):
        store.write_atomic(path, payload)


def read_cas_blob(kind: str, name: str) -> bytes | None:
    """로컬 CAS 블롭 읽기 — 서빙용. 형식 불량·부재면 None."""
    path = blob_path(kind, name)
    if path is None:
        return None
    store = config.blob_store()
    return store.read_bytes(path) if store.is_file(path) else None


def _has_blob(kind: str, name: str) -> bool:
    path = blob_path(kind, name)
    return path is not None and config.blob_store().is_file(path)


def is_busy(conn: sqlite3.Connection) -> int:
    """수신 백프레셔 판정 — 바쁘면 Retry-After 초, 여유면 0.

    이전·스토리지 마이그레이션(쓰기 중단)이거나 대기·진행 아카이빙 작업이 임계 이상이면
    "서로 여유 있을 때"만 받도록 잠시 거부한다(보내는 쪽은 백오프).
    """
    if db.writes_paused(conn):
        return config.CLUSTER_BUSY_RETRY_AFTER_SECONDS
    if db.count_active_archive_jobs(conn) >= config.CLUSTER_BUSY_JOBS_THRESHOLD:
        return config.CLUSTER_BUSY_RETRY_AFTER_SECONDS
    return 0


# ── 스냅샷 1건 직렬화/적재 (전송 원자 단위 — 코어 storage+db 경유, 원칙 1) ──


def serialize_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> dict | None:
    """스냅샷 1건을 전송 envelope 로 직렬화 — 메타 + 인라인 스냅샷 파일 + CAS 참조.

    공유 CAS 블롭(자원·문서)은 인라인하지 않고 sha256 참조만 담는다(수신측이 없는 것만
    별도로 가져간다). provenance 는 이 노드(출처)의 UUID + 이 snapshots.id. 없으면 None.
    """
    row = conn.execute(
        """
        SELECT s.*, p.url AS page_url, p.domain, p.slug
        FROM snapshots s JOIN pages p ON p.id = s.page_id
        WHERE s.id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return None
    snap_dir = storage.page_dir(row["domain"], row["slug"]) / row["dir_name"]
    store = config.blob_store()
    files = []
    for f in storage.snapshot_files(snap_dir):
        data = store.read_bytes(snap_dir / str(f["name"]))
        files.append({"name": f["name"], "b64": base64.b64encode(data).decode("ascii")})
    resource_refs = [
        {"name": r["name"], "url": r["url"]}
        for r in db.get_snapshot_resource_refs(conn, snapshot_id)
    ]
    document_refs = [
        {"url": d["url"], "file": d["file"], "bytes": d["bytes"],
         "sha256": d["sha256"], "content_type": d["content_type"]}
        for d in db.get_snapshot_document_refs(conn, snapshot_id)
    ]
    return {
        "protocol_version": config.CLUSTER_PROTOCOL_VERSION,
        "origin_node_id": db.cluster_node_id(conn),
        "origin_ref": str(snapshot_id),
        "page": {"url": row["page_url"], "domain": row["domain"], "slug": row["slug"]},
        "snapshot": {
            "taken_at": row["taken_at"], "dir_name": row["dir_name"],
            "content_hash": row["content_hash"], "final_url": row["final_url"],
            "http_status": row["http_status"], "changed": row["changed"],
            "note": row["note"], "title": row["title"],
            "origin": row["origin"], "incomplete": row["incomplete"],
            "authenticated": row["authenticated"],
        },
        "files": files,
        "resources": resource_refs,
        "documents": document_refs,
    }


def _envelope_blobs(envelope: dict) -> list[tuple[str, str]]:
    """envelope 가 참조하는 CAS 블롭 (kind, name) 목록 — 자원 + 문서."""
    out: list[tuple[str, str]] = []
    for r in envelope.get("resources") or []:
        out.append(("resource", r["name"]))
    for d in envelope.get("documents") or []:
        name = documents.cas_name(d["sha256"], d["file"])
        if name:
            out.append(("document", name))
    return out


def missing_blobs(envelope: dict) -> list[tuple[str, str]]:
    """envelope 참조 중 로컬 CAS 에 없는 (kind, name) — 수신측이 이것만 가져온다."""
    return [(k, n) for (k, n) in _envelope_blobs(envelope) if not _has_blob(k, n)]


def import_snapshot(
    conn: sqlite3.Connection, envelope: dict, *, peer_node_id: str
) -> int | None:
    """수신 envelope 를 새 스냅샷으로 적재 (코어 경유) — 새 snapshots.id, 중복이면 None.

    중복(동일 출처 노드 + 원본 snapshots.id)은 처리·저장 모두 생략한다(로깅도 호출부에서
    생략 — None 반환). 참조 CAS 블롭이 하나라도 없으면 부분 적재 대신 예외(호출부가 먼저
    블롭을 받아둔다). 수신분의 보호는 사이트 기본값(1=보호)을 따라 재연합되지 않고,
    origin_node_id 기록으로 되돌려보내기도 막힌다.
    """
    origin_ref = str(envelope.get("origin_ref") or "")
    snap = envelope["snapshot"]
    page = envelope["page"]
    # 중복 수신 — 같은 출처의 같은 원본 스냅샷이면 스킵(로깅도 호출부에서 생략).
    if db.find_snapshot_by_provenance(conn, peer_node_id, origin_ref) is not None:
        return None
    # 무결성 — 참조 블롭이 모두 로컬에 있어야 한다(부분 적재 금지).
    miss = missing_blobs(envelope)
    if miss:
        raise IntegrityError(f"필요한 CAS 블롭이 없습니다: {miss[:3]}")

    # 페이지 — URL 매칭(없으면 생성). 새 사이트는 보호 기본값(1)을 따른다.
    existing = db.get_page(conn, page["url"])
    if existing is None:
        site_id = db.get_or_create_site(conn, storage.site_key(page["url"]))
        cur = conn.execute(
            "INSERT INTO pages (url, domain, slug, site_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (page["url"], page["domain"], page["slug"], site_id, db._utcnow()),
        )
        page_id = cur.lastrowid
        domain, slug = page["domain"], page["slug"]
    else:
        page_id = existing["id"]
        domain, slug = existing["domain"], existing["slug"]

    # 같은 (page, dir_name) 중복 방지 (다른 경로로 이미 받았을 때)
    dup = conn.execute(
        "SELECT id FROM snapshots WHERE page_id = ? AND dir_name = ?",
        (page_id, snap["dir_name"]),
    ).fetchone()
    if dup is not None:
        return None

    # 스냅샷 행 — provenance 기록(수신분 표식·되돌려보내기 방지). authenticated 는
    # 출처값을 따르되 authenticated_by 는 NULL(로컬 사용자와 무관 — 관리자 전용 열람).
    snap_id = db.insert_snapshot(
        conn, page_id,
        taken_at=snap["taken_at"], dir_name=snap["dir_name"],
        content_hash=snap["content_hash"], final_url=snap["final_url"],
        http_status=snap.get("http_status"),
        changed=int(snap.get("changed", 1)), note=snap.get("note"),
        title=snap.get("title"),
        origin=snap.get("origin", "server"),
        incomplete=int(snap.get("incomplete", 0)),
        authenticated=int(snap.get("authenticated", 0)),
        origin_node_id=peer_node_id, origin_ref=origin_ref,
    )

    # 스냅샷 디렉토리 파일 — 코어 저장 경로(blob_store)로 기록.
    store = config.blob_store()
    dst_dir = storage.page_dir(domain, slug) / snap["dir_name"]
    for f in envelope.get("files") or []:
        store.write_atomic(dst_dir / str(f["name"]), base64.b64decode(f["b64"]))
    db.update_snapshot_bytes(conn, snap_id, storage.snapshot_dir_bytes(dst_dir))

    # 자원·문서 참조 행 — CAS 블롭은 이미 위에서 존재가 보장됨.
    refs = envelope.get("resources") or []
    if refs:
        db.insert_snapshot_resources(conn, snap_id, [{"name": r["name"], "url": r.get("url")} for r in refs])
    docs = envelope.get("documents") or []
    if docs:
        db.insert_snapshot_documents(conn, snap_id, [{
            "url": d.get("url") or "", "file": d["file"],
            "bytes": int(d.get("bytes") or 0), "sha256": d["sha256"],
            "content_type": d.get("content_type") or "application/octet-stream",
        } for d in docs])
    return snap_id


def log_received(
    conn: sqlite3.Connection, origin_node_id: str, envelope: dict, snap_id: int
) -> None:
    """수신 신규 아카이브를 archive_logs 에 기록 — source='cluster' + 출처 피어 id.

    push 수신(A 측 라우트)·pull 수신(B 측 조정 루프) 양쪽에서 공용. 중복 수신은
    호출 전에 걸러지므로 신규만 기록된다(중복 무기록)."""
    peer = db.get_cluster_peer_by_node(conn, origin_node_id)
    snap = envelope.get("snapshot") or {}
    page = envelope.get("page") or {}
    page_row = db.get_page(conn, page.get("url") or "")
    db.insert_archive_log(
        conn,
        url=page.get("url") or "",
        domain=(page.get("domain") or ""),
        page_id=page_row["id"] if page_row else None,
        snapshot_id=snap_id,
        source="cluster",
        status="new",
        started_at=db._utcnow(),
        content_hash=snap.get("content_hash"),
        cluster_peer_id=peer["id"] if peer else None,
    )


# ── B 측 HTTP 클라이언트 (pull/push — 통신은 항상 B 가 개시) ──


def _check_resp(resp: httpx.Response) -> httpx.Response:
    """공통 응답 처리 — 401/403→폐기, 429→백프레셔, 그 외 비200→일시 오류."""
    if resp.status_code in (401, 403):
        raise PeerAuthRejected("피어가 키를 거부했습니다")
    if resp.status_code == 429:
        try:
            retry = int(resp.headers.get("Retry-After", config.CLUSTER_BUSY_RETRY_AFTER_SECONDS))
        except ValueError:
            retry = config.CLUSTER_BUSY_RETRY_AFTER_SECONDS
        raise PeerBusy(retry)
    if resp.status_code != 200:
        raise PeerUnavailable(f"피어 응답 오류 (HTTP {resp.status_code})")
    return resp


def pull_list(base_url: str, api_key: str, *, after: int, limit: int) -> list[dict]:
    """받기 — 커서 이후 공유 가능한 피어 스냅샷 목록(단조 id)."""
    try:
        with _client() as c:
            resp = c.get(base_url + "/api/cluster/snapshots",
                         params={"after": after, "limit": limit},
                         headers=_auth_headers(api_key))
    except httpx.HTTPError as e:
        raise PeerUnavailable(str(e)) from e
    return _check_resp(resp).json().get("snapshots", [])


def pull_envelope(base_url: str, api_key: str, snapshot_id: int) -> dict:
    """받기 — 스냅샷 1건 envelope."""
    try:
        with _client() as c:
            resp = c.get(f"{base_url}/api/cluster/snapshots/{snapshot_id}",
                         headers=_auth_headers(api_key))
    except httpx.HTTPError as e:
        raise PeerUnavailable(str(e)) from e
    return _check_resp(resp).json()


def pull_blob(base_url: str, api_key: str, kind: str, name: str) -> bytes:
    """받기 — 없는 CAS 블롭 1개."""
    try:
        with _client() as c:
            resp = c.get(f"{base_url}/api/cluster/blobs/{kind}/{name}",
                         headers=_auth_headers(api_key))
    except httpx.HTTPError as e:
        raise PeerUnavailable(str(e)) from e
    return _check_resp(resp).content


def push_negotiate(base_url: str, api_key: str, blobs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """보내기 — A 가 갖고 있지 않은 (kind, name) 목록을 받는다(없는 것만 업로드)."""
    payload = [{"kind": k, "name": n} for (k, n) in blobs]
    try:
        with _client() as c:
            resp = c.post(base_url + "/api/cluster/negotiate",
                          json={"blobs": payload}, headers=_auth_headers(api_key))
    except httpx.HTTPError as e:
        raise PeerUnavailable(str(e)) from e
    miss = _check_resp(resp).json().get("missing", [])
    return [(m["kind"], m["name"]) for m in miss]


def push_blob(base_url: str, api_key: str, kind: str, name: str, payload: bytes) -> None:
    """보내기 — CAS 블롭 1개 업로드(A 가 검증·저장)."""
    try:
        with _client() as c:
            resp = c.post(f"{base_url}/api/cluster/blobs/{kind}/{name}",
                          content=payload,
                          headers={**_auth_headers(api_key),
                                   "Content-Type": "application/octet-stream"})
    except httpx.HTTPError as e:
        raise PeerUnavailable(str(e)) from e
    _check_resp(resp)


def push_snapshot(base_url: str, api_key: str, envelope: dict) -> str:
    """보내기 — 스냅샷 envelope 적재 요청(A 가 import). 반환 status('new'|'duplicate')."""
    try:
        with _client() as c:
            resp = c.post(base_url + "/api/cluster/snapshots",
                          json=envelope, headers=_auth_headers(api_key))
    except httpx.HTTPError as e:
        raise PeerUnavailable(str(e)) from e
    return _check_resp(resp).json().get("status", "new")
