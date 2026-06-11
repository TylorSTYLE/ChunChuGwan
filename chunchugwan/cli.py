"""CLI. 모든 쓰기 작업의 유일한 진입점(대시보드도 내부적으로 이 레이어 호출)."""

from __future__ import annotations

import logging
import tarfile
from pathlib import Path

import click

from . import backup as backup_mod
from . import capture as capture_mod
from . import config, db, differ, pipeline, resources, scheduler, storage

_STATUS_LABELS = {"new": "신규", "changed": "변경", "forced_same": "동일(강제 저장)"}


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="단계별 상세 로그를 stderr 로 출력")
def main(verbose: bool) -> None:
    """춘추관 — 개인 웹 아카이빙 시스템."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


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
    else:
        click.echo(f"  +{d.added}줄 / -{d.removed}줄")
        click.echo(d.unified)

    base = storage.page_dir(page["domain"], page["slug"])
    old_shot = storage.find_screenshot(base / old_snap["dir_name"])
    new_shot = storage.find_screenshot(base / new_snap["dir_name"])
    if old_shot is not None and new_shot is not None:
        ratio, out_png = differ.cached_screenshot_diff(
            old_shot, new_shot, f"shotdiff-{old_snap['id']}-{new_snap['id']}"
        )
        click.echo(f"스크린샷 변경 픽셀 {ratio:.2%}  (하이라이트: {out_png})")


@main.group()
def schedule() -> None:
    """주기적 자동 재아카이빙 관리 (최소 1시간 ~ 최대 1주일)."""


@schedule.command("add")
@click.argument("url")
@click.option(
    "--every", required=True,
    help="반복 주기 — 1h ~ 1w (예: 1h, 90m, 12h, 3d, 1w)",
)
def schedule_add(url: str, every: str) -> None:
    """URL에 반복 주기를 등록/변경한다. 다음 실행은 지금 + 주기."""
    try:
        seconds = scheduler.parse_interval(every)
        row = scheduler.set_schedule(url, seconds)
    except ValueError as e:
        raise click.ClickException(str(e))
    click.echo(
        f"스케줄 등록: {row['url']} — {scheduler.format_interval(seconds)} 주기, "
        f"다음 실행 {row['next_run_at']}"
    )


@schedule.command("list")
def schedule_list() -> None:
    """등록된 스케줄 목록 (다음 실행이 가까운 순)."""
    with db.connect() as conn:
        rows = db.list_schedules(conn)
    if not rows:
        click.echo("등록된 스케줄이 없습니다.")
        return
    click.echo(f"{'주기':<12}  {'다음 실행':<25}  {'마지막 실행':<25}  URL")
    for r in rows:
        click.echo(
            f"{scheduler.format_interval(r['interval_seconds']):<12}  "
            f"{r['next_run_at']:<25}  {r['last_run_at'] or '-':<25}  {r['url']}"
        )


@schedule.command("remove")
@click.argument("url")
def schedule_remove(url: str) -> None:
    """URL의 스케줄을 해제한다."""
    try:
        removed = scheduler.remove_schedule(url)
    except ValueError as e:
        raise click.ClickException(str(e))
    if not removed:
        raise click.ClickException("등록된 스케줄이 없는 URL 입니다")
    click.echo("스케줄 해제됨")


@schedule.command("run")
def schedule_run() -> None:
    """기한이 된 스케줄을 한 번 실행 (cron 용 — serve 중에는 자동 실행됨)."""
    results = scheduler.run_due()
    if not results:
        click.echo("실행할 스케줄이 없습니다.")
        return
    for r in results:
        click.echo(f"{r.url} — {r.status}" + (f" ({r.error})" if r.error else ""))


def _fmt_mb(n: int) -> str:
    return f"{n / 1048576:.1f}MB"


@main.command()
@click.option("--yes", is_flag=True, help="확인 없이 진행")
def compact(yes: bool) -> None:
    """기존 스냅샷 저장 공간 압축 — 공유 자원 추출 + HTML gzip + 스크린샷 WebP.

    내용 보존 변환이라 스냅샷이 담는 정보는 그대로다 (불변 원칙의 유일한 예외).
    새 스냅샷은 저장 시점에 같은 형태로 압축되므로 한 번만 실행하면 된다.
    """
    count = len(resources.snapshot_dirs())
    if count == 0:
        click.echo("압축할 스냅샷이 없습니다.")
        return
    if not yes:
        click.confirm(
            f"스냅샷 {count}개의 파일을 압축 저장 형태(page.html.gz·"
            "raw.html.gz·screenshot.webp + 공유 자원)로 변환합니다. 계속할까요?",
            abort=True,
        )

    result = resources.compact_all()
    if result.converted == 0:
        click.echo(f"스냅샷 {result.total}개 모두 이미 압축 형태입니다.")
        return
    click.echo(
        f"변환 {result.converted}/{result.total}개 · "
        f"공유 자원 {result.externalized}개 추출 · "
        f"{_fmt_mb(result.before_bytes)} → {_fmt_mb(result.after_bytes)} "
        f"({_fmt_mb(result.saved_bytes)} 절약)"
    )


def _counts_label(manifest: dict) -> str:
    """manifest 의 counts 를 확인 메시지용 한 줄로."""
    c = manifest.get("counts", {})
    return (
        f"페이지 {c.get('pages', '?')}개, 스냅샷 {c.get('snapshots', '?')}개, "
        f"확인 기록 {c.get('checks', '?')}개"
    )


@main.command()
@click.argument("dest", type=click.Path(path_type=Path), default=".", required=False)
def backup(dest: Path) -> None:
    """전체 백업 tar.gz 생성 — DB(인증 포함)·스냅샷 파일·rules.json."""
    try:
        out = backup_mod.create_backup(dest)
    except OSError as e:
        raise click.ClickException(f"백업 실패: {e}")
    click.echo(f"백업 생성: {out}")


@main.command()
@click.argument("src", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--yes", is_flag=True, help="확인 없이 진행")
def restore(src: Path, yes: bool) -> None:
    """전체 백업에서 복원 — 현재 아카이브 루트를 백업 시점 상태로 교체."""
    try:
        manifest = backup_mod.read_manifest(src)
    except (ValueError, tarfile.TarError, OSError) as e:
        raise click.ClickException(f"백업 파일을 읽을 수 없습니다: {e}")
    if manifest["kind"] != "full":
        raise click.ClickException(
            "전체 백업 파일이 아닙니다 — 아카이브 내보내기는 wccg import 로 가져오세요"
        )
    if not yes:
        click.confirm(
            f"현재 데이터(인증 포함)를 모두 백업 시점으로 대체합니다 "
            f"(백업: {manifest.get('created_at', '?')}, {_counts_label(manifest)}). 계속할까요?",
            abort=True,
        )
    try:
        backup_mod.restore_backup(src)
    except (ValueError, tarfile.TarError, OSError) as e:
        raise click.ClickException(f"복원 실패: {e}")
    click.echo(f"복원 완료: {_counts_label(manifest)}")


@main.command()
@click.argument("dest", type=click.Path(path_type=Path), default=".", required=False)
def export(dest: Path) -> None:
    """아카이브 데이터만 내보내기 — 페이지·스냅샷·확인 기록 + 파일 (인증·로그 제외)."""
    try:
        out = backup_mod.export_archive(dest)
    except OSError as e:
        raise click.ClickException(f"내보내기 실패: {e}")
    click.echo(f"내보내기 생성: {out}")


@main.command("import")
@click.argument("src", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--mode", type=click.Choice(["merge", "overwrite"]), default="merge",
    show_default=True, help="merge=기존 유지+중복 스킵, overwrite=아카이브 데이터 교체",
)
@click.option("--yes", is_flag=True, help="overwrite 확인 없이 진행")
def import_cmd(src: Path, mode: str, yes: bool) -> None:
    """내보낸 아카이브 데이터 가져오기 (인증 데이터는 건드리지 않음)."""
    if mode == "overwrite" and not yes:
        click.confirm(
            "기존 아카이브 데이터(페이지·스냅샷·확인 기록·파일)를 모두 지우고 가져옵니다. "
            "계속할까요?",
            abort=True,
        )
    try:
        result = backup_mod.import_archive(src, mode=mode)
    except (ValueError, tarfile.TarError, OSError) as e:
        raise click.ClickException(f"가져오기 실패: {e}")
    click.echo(
        f"가져오기 완료 [{mode}]: 페이지 +{result.pages_added}, "
        f"스냅샷 +{result.snapshots_added} (스킵 {result.snapshots_skipped}), "
        f"확인 기록 +{result.checks_added}"
    )


def _is_loopback(host: str) -> bool:
    """바인딩 주소가 루프백인지 (외부 노출 안전장치 판정용)."""
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")


@main.command()
@click.option("--port", default=None, type=int)
@click.option("--host", default=None, help="바인딩 주소 (기본 127.0.0.1, 외부 노출 시 인증 필수)")
def serve(port: int | None, host: str | None) -> None:
    """대시보드 실행 (기본 loopback 전용)."""
    import uvicorn

    bind_host = host or config.DASHBOARD_HOST
    if not _is_loopback(bind_host):
        if not config.AUTH_ENABLED:
            raise click.ClickException(
                f"WCCG_AUTH=off 상태로는 {bind_host} 에 바인딩할 수 없습니다. "
                "외부 노출에는 인증이 필수입니다."
            )
        if config.oidc_enabled() and not config.PUBLIC_URL:
            click.echo(
                "경고: 외부 바인딩 + OIDC 사용 중인데 WCCG_PUBLIC_URL 이 비어 있습니다. "
                "redirect_uri 가 localhost 로 조립되어 SSO 콜백이 실패할 수 있습니다.",
                err=True,
            )

    if config.AUTH_ENABLED:
        from . import auth

        with db.connect() as conn:
            if db.count_users(conn) == 0:
                if auth.bootstrap_admin_from_env(conn):
                    click.echo(f"최초 구동 — 관리자 계정 등록: {config.ADMIN_EMAIL}")
                else:
                    click.echo(
                        "최초 구동 — 관리자 미등록. 브라우저 첫 접속 시 /setup 에서 "
                        "등록하거나 WCCG_ADMIN_EMAIL/PASSWORD 를 설정하세요."
                    )

    uvicorn.run(
        "chunchugwan.web.app:app",
        host=bind_host,
        port=port or config.DASHBOARD_PORT,
    )


if __name__ == "__main__":
    main()
