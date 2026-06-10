"""CLI. 모든 쓰기 작업의 유일한 진입점(대시보드도 내부적으로 이 레이어 호출)."""

from __future__ import annotations

import click

from . import capture as capture_mod
from . import config, db, differ, pipeline, storage

_STATUS_LABELS = {"new": "신규", "changed": "변경", "forced_same": "동일(강제 저장)"}


@click.group()
def main() -> None:
    """개인 웹 아카이빙 시스템."""


@main.command()
@click.argument("url")
@click.option("--force", is_flag=True, help="콘텐츠가 동일해도 스냅샷 강제 저장")
def add(url: str, force: bool) -> None:
    """URL을 아카이빙한다."""
    try:
        outcome = pipeline.archive_url(url, force=force)
    except (ValueError, capture_mod.CaptureError) as e:
        raise click.ClickException(str(e))

    if outcome.status == "unchanged":
        click.echo(f"변경 없음 (마지막 스냅샷 {outcome.last_taken_at})")
        return
    click.echo(f"저장됨 [{_STATUS_LABELS[outcome.status]}]: {outcome.snapshot_dir}")
    click.echo(
        f"  hash {outcome.content_hash[:12]}  http {outcome.http_status}  "
        f"title {outcome.title or '-'}"
    )


@main.command("list")
def list_cmd() -> None:
    """아카이브 전체 현황."""
    with db.connect() as conn:
        pages = db.list_pages(conn)
    if not pages:
        click.echo("아카이브된 페이지가 없습니다.")
        return
    click.echo(f"{'스냅샷':>4}  {'마지막 캡처':<25}  URL")
    for row in pages:
        click.echo(
            f"{row['snapshot_count']:>6}  {row['last_taken_at'] or '-':<25}  {row['url']}"
        )


def _find_page(conn, url: str):
    """정규화 URL로 page row 조회. 없으면 ClickException."""
    try:
        norm = storage.normalize_url(url)
    except ValueError as e:
        raise click.ClickException(str(e))
    page = db.get_page(conn, norm)
    if page is None:
        raise click.ClickException(f"아카이브에 없는 URL: {norm}")
    return page


@main.command()
@click.argument("url")
def history(url: str) -> None:
    """URL의 스냅샷 히스토리 (오래된 순, 번호는 diff --from/--to 에 사용)."""
    with db.connect() as conn:
        page = _find_page(conn, url)
        snaps = db.list_snapshots(conn, page["id"])
    click.echo(page["url"])
    for i, s in enumerate(snaps, 1):
        badge = "신규" if i == 1 else ("변경" if s["changed"] else "동일")
        click.echo(
            f"{i:>3}  {s['taken_at']}  {s['content_hash'][:12]}  [{badge}]  {s['dir_name']}"
        )


def _snapshot_text(page, snap) -> str:
    """스냅샷의 content.md 내용을 읽는다."""
    path = storage.page_dir(page["domain"], page["slug"]) / snap["dir_name"] / "content.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise click.ClickException(f"content.md 읽기 실패: {path} ({e})")


@main.command()
@click.argument("url")
@click.option("--from", "from_idx", type=int, default=None, help="비교 기준 스냅샷 번호(오래된 쪽)")
@click.option("--to", "to_idx", type=int, default=None, help="비교 대상 스냅샷 번호(최신 쪽)")
def diff(url: str, from_idx: int | None, to_idx: int | None) -> None:
    """스냅샷 비교. 기본은 최신 2개."""
    with db.connect() as conn:
        page = _find_page(conn, url)
        snaps = db.list_snapshots(conn, page["id"])
    if len(snaps) < 2:
        raise click.ClickException(f"비교하려면 스냅샷이 2개 이상 필요합니다 (현재 {len(snaps)}개)")

    if to_idx is None:
        to_idx = len(snaps)
    if from_idx is None:
        from_idx = to_idx - 1
    if not (1 <= from_idx < to_idx <= len(snaps)):
        raise click.ClickException(
            f"잘못된 범위: --from {from_idx} --to {to_idx} (1 ~ {len(snaps)}, from < to)"
        )

    old_snap, new_snap = snaps[from_idx - 1], snaps[to_idx - 1]
    d = differ.diff_text(_snapshot_text(page, old_snap), _snapshot_text(page, new_snap))

    click.echo(f"{page['url']}")
    click.echo(f"  {from_idx}: {old_snap['taken_at']}  →  {to_idx}: {new_snap['taken_at']}")
    if d.identical:
        click.echo("변경 없음")
        return
    click.echo(f"  +{d.added}줄 / -{d.removed}줄")
    click.echo(d.unified)


@main.command()
@click.option("--port", default=None, type=int)
def serve(port: int | None) -> None:
    """대시보드 실행 (localhost 전용)."""
    import uvicorn

    uvicorn.run(
        "archiver.web.app:app",
        host=config.DASHBOARD_HOST,
        port=port or config.DASHBOARD_PORT,
    )


if __name__ == "__main__":
    main()
