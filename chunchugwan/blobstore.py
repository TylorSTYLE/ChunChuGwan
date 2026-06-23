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
        """로컬에서 직접 읽고/쓸 수 있는 경로 (PIL·스트리밍 등 경로 의존 연산용)."""
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
