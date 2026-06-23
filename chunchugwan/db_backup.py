"""S3 DB 백업 — index.db(+rules.json)를 S3 `db-backups/` 프리픽스에 백업.

S3 모드에서 비활성화될 전체 백업(backup.py)의 대체 내구성 수단이다. 실행 중에도
안전하도록 sqlite backup API(backup._consistent_db_copy)로 일관 복사한 index.db 와
rules.json 을 tar.gz 단일 객체로 묶어, 종단 무결성(sha256 체크섬)으로 업로드한다.
보존 개수(rotation)를 넘는 오래된 백업만 삭제하고 최신은 항상 남긴다.

정기 실행은 serve 스케줄러 폴링(run_scheduled)에서, 즉시 실행은 웹/CLI(run_blocking)
에서 한다. S3 모드가 아니거나 일시중지(인스턴스 이전·스토리지 마이그레이션) 중에는
정기 백업을 건너뛴다. 비밀값(S3 키)·백업 내용은 로그에 출력하지 않는다.

복구(이 백업을 읽어 복원)는 이번 범위가 아니다(P5) — 형식은 P5 가 그대로 읽도록
단순하게 둔다: ``<prefix>/db-backups/<UTC 타임스탬프>.tar.gz`` 안에 ``index.db`` 와
(있으면) ``rules.json``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import shutil
import tarfile
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import backup, config, db

logger = logging.getLogger(__name__)

# 백업 객체 프리픽스 (버킷 내, WCCG_S3_PREFIX 하위)
_BACKUP_DIR = "db-backups"
_SUFFIX = ".tar.gz"

# 즉시 실행 동시성 가드 (serve 단일 프로세스 인메모리)
_lock = threading.Lock()
_running = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp() -> str:
    """정렬 가능한 UTC 타임스탬프 (콜론→하이픈)."""
    return _utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")


def s3_mode() -> bool:
    """활성 백엔드가 's3' 인지."""
    with db.connect() as conn:
        return db.storage_backend(conn) == "s3"


def _client_bucket_prefix():
    """boto3 클라이언트 + 버킷 + 백업 키 프리픽스. env 불완전 시 RuntimeError.

    blobstore 인터페이스를 바꾸지 않으려고 db-backups 전용 클라이언트를 만든다
    (db-backups 는 blob 이 아니라 DB 내구성 객체라 경로 매핑과 무관).
    """
    s = config.s3_settings()  # 필수 env 검증 (비밀값 노출 없이 RuntimeError)
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=s["endpoint_url"] or None,
        region_name=s["region"],
        aws_access_key_id=s["access_key_id"],
        aws_secret_access_key=s["secret_access_key"],
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if s["force_path_style"] else "auto"},
            connect_timeout=10,
            read_timeout=120,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )
    prefix = s["prefix"]
    base = f"{prefix}/{_BACKUP_DIR}/" if prefix else f"{_BACKUP_DIR}/"
    return client, s["bucket"], base


def _build_archive() -> bytes:
    """일관 복사 index.db + (있으면) rules.json 을 tar.gz bytes 로 묶는다."""
    tmp = Path(tempfile.mkdtemp(prefix="wccg-dbbackup-"))
    try:
        db_copy = tmp / "index.db"
        backup._consistent_db_copy(db_copy)  # sqlite backup API — 실행 중 안전
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(db_copy, arcname="index.db")
            if config.RULES_PATH.is_file():
                tar.add(config.RULES_PATH, arcname="rules.json")
        return buf.getvalue()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _list_backups(client, bucket: str, base: str) -> list[dict]:
    """백업 객체 목록 — [{key, bytes, at}], 타임스탬프 키 사전순(=시간순) 정렬."""
    paginator = client.get_paginator("list_objects_v2")
    items: list[dict] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=base):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(_SUFFIX):
                items.append({
                    "key": obj["Key"],
                    "bytes": obj["Size"],
                    "at": obj["LastModified"].isoformat(),
                })
    items.sort(key=lambda x: x["key"])
    return items


def _rotate(client, bucket: str, base: str) -> None:
    """보존 개수를 넘는 오래된 백업만 삭제 (최신 keep 개는 항상 보존)."""
    with db.connect() as conn:
        keep = db.db_backup_keep(conn)
    items = _list_backups(client, bucket, base)
    if len(items) <= keep:
        return
    for old in items[:-keep]:  # 오래된 것부터, 최신 keep 개 제외
        client.delete_object(Bucket=bucket, Key=old["key"])


def run_once() -> dict:
    """백업 1회 — 일관 복사 + 업로드(sha256 체크섬) + rotation. 메타 반환·저장."""
    client, bucket, base = _client_bucket_prefix()
    data = _build_archive()
    sha = hashlib.sha256(data).hexdigest()
    key = f"{base}{_timestamp()}{_SUFFIX}"
    client.put_object(
        Bucket=bucket, Key=key, Body=data,
        ChecksumSHA256=base64.b64encode(bytes.fromhex(sha)).decode("ascii"),
    )
    _rotate(client, bucket, base)
    meta = {
        "last_at": _utcnow().isoformat(timespec="seconds"),
        "last_key": key,
        "last_bytes": len(data),
        "last_sha256": sha,
        "last_status": "ok",
        "last_error": None,
    }
    with db.connect() as conn:
        db.set_db_backup_meta(conn, meta)
    return meta


def run_blocking() -> dict:
    """동기 즉시 백업 (웹/CLI). S3 아니거나 진행 중이면 RuntimeError."""
    global _running
    with _lock:
        if not s3_mode():
            raise RuntimeError("S3 모드에서만 DB 백업을 쓸 수 있습니다.")
        if _running:
            raise RuntimeError("이미 백업이 진행 중입니다.")
        _running = True
    try:
        return run_once()
    finally:
        with _lock:
            _running = False


def _due(last_at: str | None, interval_hours: int) -> bool:
    """마지막 백업 이후 주기가 지났는지 (없거나 파싱 실패면 도래로 본다)."""
    if not last_at:
        return True
    try:
        prev = datetime.fromisoformat(last_at)
    except ValueError:
        return True
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=timezone.utc)
    return _utcnow() >= prev + timedelta(hours=interval_hours)


def run_scheduled() -> None:
    """스케줄러 폴링에서 호출 — S3 모드·비일시중지·주기 도래 시에만 백업.

    실패해도 예외를 던지지 않는다(스케줄러 스레드 보호) — 다음 주기에 재시도하고
    오류 종류만 기록한다(비밀값·내용 비노출).
    """
    with db.connect() as conn:
        if db.storage_backend(conn) != "s3":
            return
        if db.writes_paused(conn):  # 인스턴스 이전·스토리지 마이그레이션 중 건너뜀
            return
        interval = db.db_backup_interval_hours(conn)
        meta = db.db_backup_meta(conn)
    if not _due((meta or {}).get("last_at"), interval):
        return
    try:
        run_once()
    except Exception as e:  # noqa: BLE001 — 스케줄러 스레드 생존, 다음 주기 재시도
        logger.warning("정기 DB 백업 실패(다음 주기 재시도): %s", type(e).__name__)
        with db.connect() as conn:
            prev = db.db_backup_meta(conn) or {}
            prev.update({
                "last_status": "error",
                "last_error": type(e).__name__,
                "last_attempt_at": _utcnow().isoformat(timespec="seconds"),
            })
            db.set_db_backup_meta(conn, prev)


def status() -> dict:
    """DB 백업 상태 — 설정·마지막 결과·진행 중 여부·백업 목록 요약.

    S3 list 는 S3 모드일 때 1회만 호출한다(빈번 폴링 아님 — status/run/카드 진입용).
    """
    with db.connect() as conn:
        s3 = db.storage_backend(conn) == "s3"
        interval = db.db_backup_interval_hours(conn)
        keep = db.db_backup_keep(conn)
        meta = db.db_backup_meta(conn) or {}
    with _lock:
        running = _running
    out: dict = {
        "s3_mode": s3,
        "running": running,
        "interval_hours": interval,
        "keep": keep,
        "last_at": meta.get("last_at"),
        "last_status": meta.get("last_status"),
        "last_error": meta.get("last_error"),
        "last_bytes": meta.get("last_bytes"),
        "backups": [],
        "count": 0,
    }
    if s3:
        try:
            client, bucket, base = _client_bucket_prefix()
            items = _list_backups(client, bucket, base)
            out["count"] = len(items)
            out["backups"] = list(reversed(items[-10:]))  # 최신 10개, 최신 우선
        except Exception as e:  # noqa: BLE001 — 목록 실패는 상태 표시만 영향
            out["list_error"] = type(e).__name__
    return out
