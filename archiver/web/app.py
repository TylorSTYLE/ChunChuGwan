"""읽기 전용 대시보드 + 재아카이빙 트리거.

보안 원칙 (CLAUDE.md 5번):
- 바인딩은 127.0.0.1 고정
- 스냅샷 HTML 렌더링은 templates/snapshot.html 의
  <iframe sandbox="">  (allow-* 토큰 전부 없음 = 스크립트/폼/팝업 차단) 안에서만
- 스냅샷 파일 서빙 시 경로는 DB에 기록된 dir_name 으로만 조립.
  사용자 입력 경로를 직접 파일시스템에 매핑하지 말 것.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Web Archiver")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# TODO(M4): 라우트 구현
# GET  /                          → index.html  (db.list_pages)
# GET  /page/{page_id}            → timeline.html (db.list_snapshots)
# GET  /snapshot/{snapshot_id}    → snapshot.html (sandbox iframe 래퍼)
# GET  /snapshot/{snapshot_id}/file/{name}
#       name ∈ {page.html, screenshot.png, content.md} 화이트리스트만 서빙
# GET  /diff/{page_id}?from=&to=  → diff.html (differ.diff_text 결과)
# POST /page/{page_id}/rearchive  → BackgroundTasks 로 add 흐름 호출


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
