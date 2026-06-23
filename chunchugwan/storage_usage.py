"""온디맨드 S3 카테고리별 사용량 — ListObjectsV2 로 최상위 프리픽스별 Size 합산.

요청 경로(GET·화면 진입)에서는 절대 S3 를 호출하지 않는다 — 캐시된 값만 읽고,
실제 스캔은 명시적 트리거([업데이트] POST·CLI --scan)에서만 한다. 결과(카테고리별
바이트 + 스캔 시각)는 DB 설정에 캐시한다(db.set_s3_usage). 비밀값은 노출하지 않는다.

로컬 사용량(index.db + cache/ + read-through 캐시)은 storage.local_usage() 가 S3
호출 없이 계산하고, 로컬 모드의 사용량은 storage.archive_disk_usage() 가 그대로다.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import config, db, storage

# blob 최상위 카테고리 (로컬 미터 어휘와 일관) + S3 의 DB백업
_CATEGORIES = ("sites", "resources", "documents", "db-backups")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def scan_s3_usage() -> dict:
    """S3 전 객체를 페이지네이션 순회해 카테고리별 Size 합산 후 DB 에 캐시.

    명시적 트리거에서만 호출한다(요청 경로 자동 호출 금지). 카테고리는 WCCG_S3_PREFIX
    하위의 최상위 프리픽스(sites·resources·documents·db-backups), 그 외는 other.
    """
    import boto3
    from botocore.config import Config

    s = config.s3_settings()  # 필수 env 검증 (비밀값 노출 없이 RuntimeError)
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
            read_timeout=300,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )
    bucket = s["bucket"]
    prefix = s["prefix"]
    list_prefix = f"{prefix}/" if prefix else ""

    categories: dict[str, int] = {c: 0 for c in _CATEGORIES}
    categories["other"] = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=list_prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(list_prefix):] if list_prefix else obj["Key"]
            top = rel.split("/", 1)[0] if "/" in rel else rel
            cat = top if top in categories else "other"
            categories[cat] += obj["Size"]

    result = {
        "categories": categories,
        "total": sum(categories.values()),
        "scanned_at": _utcnow(),
    }
    with db.connect() as conn:
        db.set_s3_usage(conn, result)
    return result


def usage_snapshot() -> dict:
    """캐시만 읽는 사용량 스냅샷 — S3 미호출 (GET·화면 진입용).

    backend='s3' 면 로컬 분해(local) + 캐시된 S3 카테고리(s3, 미스캔이면 None),
    'local' 이면 기존 archive_disk_usage(archive)를 돌려준다.
    """
    with db.connect() as conn:
        backend = db.storage_backend(conn)
        s3 = db.s3_usage(conn)
    out: dict = {"backend": backend, "local": None, "s3": None, "archive": None}
    if backend == "s3":
        out["local"] = storage.local_usage()
        out["s3"] = s3  # 캐시 (없으면 None — 프론트가 [업데이트] 유도)
    else:
        out["archive"] = storage.archive_disk_usage()
    return out
