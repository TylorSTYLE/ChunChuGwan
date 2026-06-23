"""blob 저장 백엔드 추상화.

아카이브의 blob 트리(``sites/``·``resources/``·``documents/``)에 대한 파일
입출력을 단일 경계로 모은다. 코어 모듈(storage·resources·documents)이 직접
``pathlib``/``os``/``shutil`` 을 호출하는 대신 이 백엔드를 경유하게 해서,
이후 원격 객체 저장소(S3 등) 백엔드를 같은 인터페이스로 끼울 수 있게 한다.

이 모듈은 blob 의 *내용*(읽기/쓰기/존재·크기/삭제/이동/열거)만 다룬다 —
경로 계산(resource_path·cas_path·page_dir·스냅샷 디렉토리 이름)은 호출 모듈에
그대로 남고, 절대 경로를 백엔드에 넘긴다. ``index.db``·``cache/`` 같은 비-blob
데이터는 이 경계를 거치지 않는다.

LocalBlobStore 의 각 메서드는 종전 코어 코드의 파일 연산을 그대로 옮긴
것이라 로컬 동작이 1바이트도 바뀌지 않는다 (원자적 쓰기·mtime 갱신·EXDEV
이동 폴백 등 미묘한 동작 포함).

S3BlobStore 는 같은 계약을 boto3(endpoint_url + path-style)로 구현한다 —
키 레이아웃은 로컬 상대 경로를 미러(``sites/…``·``resources/{ab}/…``·
``documents/{ab}/…``)하고 선택적 키 프리픽스 하위에 둔다. 존재 확인
(``is_file``·``is_dir``·``size``)은 HEAD/LIST 로 객체를 받지 않고, 서빙·읽기
시점의 ``local_path`` 만 객체를 로컬 read-through 캐시로 materialize 한다
(콘텐츠 주소·불변이라 무효화 없이 용량 상한 + LRU 제거만). 비밀값은 로그·
예외에 노출하지 않는다.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    """blob 저장 백엔드 인터페이스 — 절대 경로 단위의 blob 입출력 연산."""

    # ---- 읽기 ----
    def read_bytes(self, path: Path, *, size: int | None = None) -> bytes:
        """blob 바이트 읽기. size 가 주어지면 앞 size 바이트만 읽는다."""
        ...

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        """blob 텍스트 읽기."""
        ...

    # ---- 쓰기 ----
    def write_bytes(self, path: Path, data: bytes) -> None:
        """blob 바이트 쓰기 (비원자적, 부모 디렉토리 존재 전제)."""
        ...

    def write_text(self, path: Path, text: str, *, encoding: str = "utf-8") -> None:
        """blob 텍스트 쓰기 (비원자적, 부모 디렉토리 존재 전제)."""
        ...

    def write_atomic(self, path: Path, data: bytes) -> None:
        """blob 바이트를 원자적으로 쓰기 (부모 생성 + 임시 파일 → os.replace)."""
        ...

    def put_verified(self, path: Path, data: bytes, sha256_hex: str) -> None:
        """무결성 보장 쓰기 — 마이그레이션 copy 전용 (원자적 + 종단 체크섬).

        로컬은 원자적 쓰기, 원격은 sha256 체크섬으로 업로드 종단 무결성을 확보한다.
        호출부가 data 의 sha256 이 sha256_hex 와 일치함을 이미 검증한 상태여야 한다.
        """
        ...

    # ---- 이동 ----
    def move(self, src: Path, dst: Path) -> None:
        """파일/디렉토리를 blob 경로로 이동 (shutil.move 의미)."""
        ...

    # ---- 검사 ----
    def is_file(self, path: Path) -> bool:
        """경로가 일반 파일인지."""
        ...

    def is_dir(self, path: Path) -> bool:
        """경로가 디렉토리인지."""
        ...

    def size(self, path: Path) -> int:
        """blob 크기 (바이트). 없으면 OSError."""
        ...

    def local_path(self, path: Path) -> Path:
        """로컬에서 직접 읽고/쓸 수 있는 경로 (PIL·스트리밍·FileResponse 서빙용).

        로컬 백엔드는 입력 경로 그대로(identity), 원격 백엔드는 객체를 로컬
        read-through 캐시로 materialize 한 뒤 그 캐시 경로를 돌려준다.
        """
        ...

    # ---- 수명 ----
    def touch_mtime(self, path: Path) -> None:
        """기존 blob 의 수정 시각(mtime)만 현재로 갱신 (sweep 유예 창)."""
        ...

    def touch_create(self, path: Path) -> None:
        """빈 마커 파일 생성 (이미 있으면 mtime 갱신)."""
        ...

    def delete(self, path: Path) -> None:
        """blob 삭제 (없으면 무시)."""
        ...

    def rmdir(self, path: Path) -> None:
        """빈 디렉토리 제거 (비어 있지 않거나 없으면 무시)."""
        ...

    def rmtree(self, path: Path) -> None:
        """디렉토리 트리 제거 (없으면 무시)."""
        ...

    def mkdir(self, path: Path, *, parents: bool = False, exist_ok: bool = False) -> None:
        """디렉토리 생성."""
        ...

    # ---- 열거 ----
    def iterdir(self, path: Path) -> Iterator[Path]:
        """디렉토리 직속 항목 열거."""
        ...

    def glob(self, path: Path, pattern: str) -> Iterator[Path]:
        """디렉토리 하위 glob."""
        ...

    def rglob(self, path: Path, pattern: str) -> Iterator[Path]:
        """디렉토리 재귀 glob."""
        ...


class LocalBlobStore:
    """로컬 파일시스템 blob 백엔드 — 절대 경로 그대로 파일을 다룬다.

    각 메서드는 종전 코어 모듈의 파일 연산과 동일하게 동작한다.
    """

    def read_bytes(self, path: Path, *, size: int | None = None) -> bytes:
        if size is None:
            return path.read_bytes()
        with path.open("rb") as f:
            return f.read(size)

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        return path.read_text(encoding=encoding)

    def write_bytes(self, path: Path, data: bytes) -> None:
        path.write_bytes(data)

    def write_text(self, path: Path, text: str, *, encoding: str = "utf-8") -> None:
        path.write_text(text, encoding=encoding)

    def write_atomic(self, path: Path, data: bytes) -> None:
        # 동시 아카이빙(스케줄러 + 수동)에 안전하도록 임시 파일 후 원자적 교체
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.replace(tmp, path)

    def put_verified(self, path: Path, data: bytes, sha256_hex: str) -> None:
        # 로컬은 원자적 쓰기로 부분/손상 파일을 막는다 (sha256 은 호출부가 검증).
        self.write_atomic(path, data)

    def move(self, src: Path, dst: Path) -> None:
        shutil.move(str(src), dst)

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def is_dir(self, path: Path) -> bool:
        return path.is_dir()

    def size(self, path: Path) -> int:
        return path.stat().st_size

    def local_path(self, path: Path) -> Path:
        return path

    def touch_mtime(self, path: Path) -> None:
        os.utime(path)

    def touch_create(self, path: Path) -> None:
        path.touch()

    def delete(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def rmdir(self, path: Path) -> None:
        try:
            path.rmdir()
        except OSError:
            pass

    def rmtree(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)

    def mkdir(self, path: Path, *, parents: bool = False, exist_ok: bool = False) -> None:
        path.mkdir(parents=parents, exist_ok=exist_ok)

    def iterdir(self, path: Path) -> Iterator[Path]:
        return path.iterdir()

    def glob(self, path: Path, pattern: str) -> Iterator[Path]:
        return path.glob(pattern)

    def rglob(self, path: Path, pattern: str) -> Iterator[Path]:
        return path.rglob(pattern)


# ---- S3(객체 저장소) 백엔드 ----


def _disable_expect_100_continue(client) -> None:
    """S3 클라이언트의 ``Expect: 100-continue`` 를 끈다 (PUT 헤더를 한 번에 전송).

    일부 S3 호환 서버(Garage/MinIO 등)는 100-continue 의 중간 응답을 Python
    http.client 가 엄격 파싱하다 HeaderParsingError 경고(+트레이스백)를 쏟는다 —
    업로드 자체는 200 으로 성공하지만 로그가 시끄럽다. Expect 헤더를 빼면 한 번의
    요청/응답으로 처리돼 경고가 사라진다(업로드 동작은 동일). botocore 내부가
    바뀌어 핸들러가 없어도 죽지 않게 best-effort 로 무시한다.
    """
    try:
        from botocore.handlers import add_expect_header

        client.meta.events.unregister("before-call.s3", add_expect_header)
    except Exception:  # noqa: BLE001
        pass


class S3BlobStore:
    """boto3 기반 S3/MinIO blob 백엔드 (read-through 캐시 포함).

    blob 절대 경로를 아카이브 루트 기준 상대 경로로 바꿔 S3 키로 쓴다
    (선택적 프리픽스 하위). 존재 확인은 HEAD/LIST 로 객체를 받지 않고,
    서빙·경로 의존 연산(local_path)만 객체를 로컬 캐시로 materialize 한다.
    """

    def __init__(
        self,
        *,
        bucket: str,
        archive_root: Path,
        cache_dir: Path,
        cache_max_bytes: int,
        endpoint_url: str = "",
        region: str = "us-east-1",
        access_key_id: str,
        secret_access_key: str,
        force_path_style: bool = True,
        prefix: str = "",
    ) -> None:
        import boto3
        from botocore.config import Config

        self._bucket = bucket
        self._archive_root = archive_root
        self._cache_dir = cache_dir
        self._cache_max_bytes = cache_max_bytes
        # 키 프리픽스는 슬래시로 정규화 (빈 값이면 프리픽스 없음)
        self._prefix = prefix.strip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if force_path_style else "auto"},
                connect_timeout=10,
                read_timeout=60,
                retries={"max_attempts": 3, "mode": "standard"},
                # 마이그레이션 동시 전송(ThreadPoolExecutor)에 연결 풀 여유를 둔다.
                max_pool_connections=16,
            ),
        )
        _disable_expect_100_continue(self._client)

    # ---- 키 ↔ 경로 매핑 ----
    def _rel(self, path: Path) -> str:
        """blob 절대 경로 → 아카이브 루트 기준 상대 POSIX 경로."""
        return path.relative_to(self._archive_root).as_posix()

    def _key(self, path: Path) -> str:
        """blob 절대 경로 → S3 객체 키 (프리픽스 포함)."""
        rel = self._rel(path)
        return f"{self._prefix}/{rel}" if self._prefix else rel

    def _to_path(self, key: str) -> Path:
        """S3 객체 키 → blob 절대 경로 (프리픽스 제거)."""
        rel = key[len(self._prefix) + 1:] if self._prefix else key
        return self._archive_root / rel

    def _client_error_code(self, e) -> str:
        """ClientError 의 HTTP/에러 코드 문자열 (없으면 빈 문자열)."""
        return str(e.response.get("Error", {}).get("Code", ""))

    # ---- 읽기 ----
    def read_bytes(self, path: Path, *, size: int | None = None) -> bytes:
        kwargs = {"Bucket": self._bucket, "Key": self._key(path)}
        if size is not None:
            kwargs["Range"] = f"bytes=0-{size - 1}"
        resp = self._client.get_object(**kwargs)
        try:
            return resp["Body"].read()
        finally:
            resp["Body"].close()

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding)

    # ---- 쓰기 (S3 PUT 는 원자적) ----
    def write_bytes(self, path: Path, data: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=self._key(path), Body=data)

    def write_text(self, path: Path, text: str, *, encoding: str = "utf-8") -> None:
        self.write_bytes(path, text.encode(encoding))

    def write_atomic(self, path: Path, data: bytes) -> None:
        self.write_bytes(path, data)

    def put_verified(self, path: Path, data: bytes, sha256_hex: str) -> None:
        # S3 가 본문 sha256 을 검증하게 해 업로드 종단 무결성을 확보한다
        # (불일치면 boto3 가 오류 — 부분/손상 객체가 생기지 않는다).
        import base64

        checksum = base64.b64encode(bytes.fromhex(sha256_hex)).decode("ascii")
        self._client.put_object(
            Bucket=self._bucket, Key=self._key(path), Body=data,
            ChecksumSHA256=checksum,
        )

    # ---- 이동 (로컬 스테이징 → S3 업로드) ----
    def move(self, src: Path, dst: Path) -> None:
        if src.is_dir():
            for child in sorted(src.rglob("*")):
                if child.is_file():
                    rel = child.relative_to(src)
                    self._client.upload_file(
                        str(child), self._bucket, self._key(dst / rel)
                    )
            shutil.rmtree(src, ignore_errors=True)
        else:
            self._client.upload_file(str(src), self._bucket, self._key(dst))
            src.unlink()

    # ---- 검사 (객체 다운로드 없음) ----
    def is_file(self, path: Path) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._key(path))
            return True
        except self._client.exceptions.ClientError as e:
            if self._client_error_code(e) in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def is_dir(self, path: Path) -> bool:
        prefix = self._key(path).rstrip("/") + "/"
        resp = self._client.list_objects_v2(
            Bucket=self._bucket, Prefix=prefix, MaxKeys=1
        )
        return resp.get("KeyCount", 0) > 0

    def size(self, path: Path) -> int:
        resp = self._client.head_object(Bucket=self._bucket, Key=self._key(path))
        return int(resp["ContentLength"])

    def local_path(self, path: Path) -> Path:
        """객체를 로컬 read-through 캐시로 materialize 한 뒤 캐시 경로 반환.

        캐시 히트면 다운로드 없이 mtime 만 갱신(LRU 기준)하고, 미스면 임시
        파일로 받아 원자적 교체(temp→os.replace)한다 — 부분 파일이 서빙되지
        않게 하고 동시 다운로드 경합을 허용한다. blob 은 불변이라 무효화는
        없고 용량 상한 초과 시 LRU 제거만 한다.
        """
        cache_file = self._cache_dir / self._rel(path)
        if cache_file.is_file():
            try:
                os.utime(cache_file)
            except OSError:
                pass
            return cache_file
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=cache_file.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                self._client.download_fileobj(self._bucket, self._key(path), f)
            os.replace(tmp, cache_file)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        _enforce_cache_budget(self._cache_dir, self._cache_max_bytes)
        return cache_file

    # ---- 수명 ----
    def touch_mtime(self, path: Path) -> None:
        # S3 객체는 불변/콘텐츠 주소라 sweep 유예 창 개념이 없다 — no-op.
        pass

    def touch_create(self, path: Path) -> None:
        self._client.put_object(Bucket=self._bucket, Key=self._key(path), Body=b"")

    def delete(self, path: Path) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=self._key(path))

    def rmdir(self, path: Path) -> None:
        # S3 에는 빈 디렉토리 개념이 없다 — no-op.
        pass

    def rmtree(self, path: Path) -> None:
        prefix = self._key(path).rstrip("/") + "/"
        paginator = self._client.get_paginator("list_objects_v2")
        batch: list[dict] = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                batch.append({"Key": obj["Key"]})
                if len(batch) == 1000:
                    self._client.delete_objects(
                        Bucket=self._bucket, Delete={"Objects": batch}
                    )
                    batch = []
        if batch:
            self._client.delete_objects(Bucket=self._bucket, Delete={"Objects": batch})

    def mkdir(self, path: Path, *, parents: bool = False, exist_ok: bool = False) -> None:
        # S3 에는 디렉토리 개념이 없다 — 키 쓰기 시 프리픽스가 자동 생성된다.
        pass

    # ---- 열거 (list_objects_v2) ----
    def _all_keys(self, prefix: str) -> Iterator[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]

    def iterdir(self, path: Path) -> Iterator[Path]:
        prefix = self._key(path).rstrip("/") + "/"
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self._bucket, Prefix=prefix, Delimiter="/"
        ):
            for obj in page.get("Contents", []):
                yield self._to_path(obj["Key"])
            for cp in page.get("CommonPrefixes", []):
                yield self._to_path(cp["Prefix"].rstrip("/"))

    def glob(self, path: Path, pattern: str) -> Iterator[Path]:
        import fnmatch

        segments = pattern.split("/")
        depth = len(segments)
        base_prefix = self._key(path).rstrip("/") + "/"
        base_rel_parts = self._rel(path).split("/") if self._rel(path) else []
        seen: set[str] = set()
        for key in self._all_keys(base_prefix):
            rel_parts = self._to_path(key).relative_to(path).parts
            if len(rel_parts) < depth:
                continue
            candidate = rel_parts[:depth]
            if not all(
                fnmatch.fnmatch(seg, pat) for seg, pat in zip(candidate, segments)
            ):
                continue
            joined = "/".join(candidate)
            if joined not in seen:
                seen.add(joined)
                yield self._archive_root.joinpath(*base_rel_parts, *candidate)

    def rglob(self, path: Path, pattern: str) -> Iterator[Path]:
        import fnmatch

        base_prefix = self._key(path).rstrip("/") + "/"
        for key in self._all_keys(base_prefix):
            p = self._to_path(key)
            if fnmatch.fnmatch(p.name, pattern):
                yield p


def _enforce_cache_budget(cache_dir: Path, max_bytes: int) -> None:
    """read-through 캐시 총량이 상한을 넘으면 LRU(가장 오래 미접근)부터 제거.

    blob 은 불변이라 콘텐츠 무효화는 없고 용량 관리만 한다. 동시 서빙
    프로세스 간 제거 경합은 best-effort 로 무시한다 (이미 지워진 파일 등).
    """
    if max_bytes <= 0 or not cache_dir.is_dir():
        return
    files: list[tuple[float, int, Path]] = []
    total = 0
    for f in cache_dir.rglob("*"):
        if not f.is_file() or f.suffix == ".tmp":
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, f))
        total += st.st_size
    if total <= max_bytes:
        return
    files.sort(key=lambda x: x[0])  # 오래된 접근(mtime) 우선
    for _mtime, fsize, f in files:
        if total <= max_bytes:
            break
        try:
            f.unlink()
            total -= fsize
        except OSError:
            pass
