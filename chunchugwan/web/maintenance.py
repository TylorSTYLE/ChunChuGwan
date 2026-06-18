"""유지보수 공용 헬퍼 — 백업/내보내기 다운로드·업로드 저장·전체 재색인 진행 상태.

C2 컷오버로 SSR `system_routes` 를 제거하면서, 그 모듈이 갖고 있던 비-SSR 공용
자산(SPA 의 `/api/web/system/*` 가 쓰는 것)을 여기로 옮겼다. 라우트는 없고
순수 헬퍼·인메모리 상태만 둔다.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .. import searchindex

logger = logging.getLogger(__name__)

# 네트워크 태그 라벨/설명 길이 제한 — /api/web/system/network-tags 검증 공용.
MAX_NETWORK_TAG_NAME_LENGTH = 60
MAX_NETWORK_TAG_DESC_LENGTH = 200


def tar_download(make: Callable[[Path], Path], prefix: str) -> FileResponse:
    """코어 함수로 tar.gz 를 만들어 다운로드로 응답 (전송 후 임시 파일 정리).

    전체 백업·내보내기·사이트 단위 내보내기가 같은 헬퍼를 쓴다.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix=f"wccg-{prefix}-"))
    try:
        out = make(tmpdir)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    return FileResponse(
        out, media_type="application/gzip", filename=out.name,
        background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
    )


def _save_upload(file: UploadFile) -> Path:
    """업로드 파일을 임시 파일로 저장 후 경로 반환."""
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
    return Path(tmp.name)


# 전체 다시 색인 진행 상태 — serve 단일 프로세스의 인메모리 (app._active_jobs 와 같은 패턴).
# 백그라운드 스레드가 갱신하고, 시스템 화면이 /api/web/system/search/reindex/status 를 폴링한다.
_reindex_lock = threading.Lock()
_reindex_state: dict = {
    "running": False, "done": 0, "total": 0,
    "result": None, "error": None, "finished_at": None,
}


def reindex_status() -> dict:
    """전체 다시 색인 진행 상태의 사본 (폴링/초기 렌더용)."""
    with _reindex_lock:
        return dict(_reindex_state)


def _reindex_worker() -> None:
    """백그라운드 재색인 — 진행률을 인메모리 상태에 갱신."""
    def _progress(done: int, total: int) -> None:
        with _reindex_lock:
            _reindex_state["done"] = done
            _reindex_state["total"] = total

    try:
        count = searchindex.reindex_all(progress=_progress)
        with _reindex_lock:
            _reindex_state["result"] = count
            _reindex_state["error"] = None
    except Exception as e:  # noqa: BLE001 — 실패해도 상태만 기록, 스레드는 정상 종료
        logger.exception("검색 인덱스 전체 다시 색인 실패")
        with _reindex_lock:
            _reindex_state["error"] = str(e)
    finally:
        with _reindex_lock:
            _reindex_state["running"] = False
            _reindex_state["finished_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
