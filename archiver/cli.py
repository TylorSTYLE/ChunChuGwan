"""CLI. 모든 쓰기 작업의 유일한 진입점(대시보드도 내부적으로 이 레이어 호출)."""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import click

from . import capture as capture_mod
from . import db, extract, storage


@click.group()
def main() -> None:
    """개인 웹 아카이빙 시스템."""


@main.command()
@click.argument("url")
@click.option("--force", is_flag=True, help="콘텐츠가 동일해도 스냅샷 강제 저장")
def add(url: str, force: bool) -> None:
    """URL을 아카이빙한다."""
    try:
        norm = storage.normalize_url(url)
    except ValueError as e:
        raise click.ClickException(str(e))
    domain = urlsplit(norm).hostname or ""
    slug = storage.url_to_slug(norm)

    # 해시가 같으면 스냅샷 디렉토리를 만들지 않도록 임시 디렉토리에 먼저 캡처
    tmp_dir = Path(tempfile.mkdtemp(prefix="archiver-"))
    try:
        try:
            result = capture_mod.capture(norm, tmp_dir)
        except capture_mod.CaptureError as e:
            raise click.ClickException(str(e))

        text = extract.extract_text(result.raw_html, norm)
        normalized = extract.normalize(text)
        content_hash = storage.content_sha256(normalized)

        with db.connect() as conn:
            page_id = db.get_or_create_page(conn, norm, domain, slug)
            prev = db.last_snapshot(conn, page_id)

            if prev and prev["content_hash"] == content_hash and not force:
                db.insert_check(conn, page_id, content_hash)
                click.echo(f"변경 없음 (마지막 스냅샷 {prev['taken_at']})")
                return

            taken_at = datetime.now(timezone.utc)
            meta = storage.SnapshotMeta(
                url=norm,
                final_url=result.final_url,
                taken_at=taken_at.isoformat(timespec="seconds"),
                content_hash=content_hash,
                http_status=result.http_status,
                title=result.title,
            )
            snap_dir = storage.finalize_snapshot(
                tmp_dir, domain, slug, meta, normalized, taken_at
            )
            changed = 1 if prev is None else int(prev["content_hash"] != content_hash)
            db.insert_snapshot(
                conn,
                page_id,
                taken_at=meta.taken_at,
                dir_name=snap_dir.name,
                content_hash=content_hash,
                final_url=result.final_url,
                http_status=result.http_status,
                changed=changed,
            )

        status = "신규" if prev is None else ("변경" if changed else "동일(강제 저장)")
        click.echo(f"저장됨 [{status}]: {snap_dir}")
        click.echo(
            f"  hash {content_hash[:12]}  http {result.http_status}  "
            f"title {result.title or '-'}"
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@main.command("list")
def list_cmd() -> None:
    """아카이브 전체 현황. TODO(M3): db.list_pages 출력."""
    raise click.ClickException("미구현 (M3)")


@main.command()
@click.argument("url")
def history(url: str) -> None:
    """URL의 스냅샷 히스토리. TODO(M3)."""
    raise click.ClickException("미구현 (M3)")


@main.command()
@click.argument("url")
@click.option("--from", "from_idx", type=int, default=None, help="비교 기준 스냅샷 번호(오래된 쪽)")
@click.option("--to", "to_idx", type=int, default=None, help="비교 대상 스냅샷 번호(최신 쪽)")
def diff(url: str, from_idx: int | None, to_idx: int | None) -> None:
    """스냅샷 비교. 기본은 최신 2개. TODO(M3): differ.diff_text 결과 출력."""
    raise click.ClickException("미구현 (M3)")


@main.command()
@click.option("--port", default=None, type=int)
def serve(port: int | None) -> None:
    """대시보드 실행. TODO(M4): uvicorn.run(web.app:app, host=127.0.0.1)."""
    raise click.ClickException("미구현 (M4)")


if __name__ == "__main__":
    main()
