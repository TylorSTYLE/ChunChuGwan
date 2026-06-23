"""CLI. 모든 쓰기 작업의 유일한 진입점(대시보드도 내부적으로 이 레이어 호출)."""

from __future__ import annotations

import logging
import tarfile
from datetime import datetime
from pathlib import Path

import click

from . import __version__
from . import backup as backup_mod
from . import config, db, searchindex, storage, system_log
# crawler·archive_worker·worker·differ·optimize·scheduler·deletion 은 capture
# (playwright)·extract(lxml)·PIL 을 전이로 끌어와 import 비용이 크다 — 해당 명령
# 에서만 쓰므로 함수 안에서 지연 import 한다 (cron 으로 자주 도는 list/search/add/
# history 등의 콜드 스타트를 줄인다).

_STATUS_LABELS = {"new": "신규", "changed": "변경", "forced_same": "동일(강제 저장)"}


def _warn_if_migrating() -> bool:
    """이전(마이그레이션) 모드면 안내를 출력하고 True 를 반환한다.

    이전 모드 동안 워커·스케줄러·크롤이 코어에서 no-op 이므로, CLI 도
    사용자에게 명확히 알린다 (시스템 설정에서 이전 모드를 꺼야 재개).
    """
    with db.connect() as conn:
        if db.migration_mode_enabled(conn):
            click.echo("이전(마이그레이션) 모드입니다 — 데이터 이전 중에는 "
                       "아카이빙·스케줄·크롤이 중단됩니다. 시스템 설정에서 이전 모드를 끄세요.")
            return True
    return False


def _console_level(verbose: bool, quiet: bool, subcommand: str | None) -> int:
    """콘솔(stderr) 로그 레벨 결정.

    worker/serve 는 장시간 도는 데몬이라 운영 로그(INFO)가 기본으로 보여야
    한다 — docker logs·터미널로 바로 상태를 확인할 수 있게. 그 외 단발 명령은
    조용히(WARNING) 두고 -v 로 INFO 를 켠다. --quiet 는 데몬에서도 WARNING 으로
    낮춘다 (-v 가 우선). 파일·DB(/system/logs) 적재는 이 레벨과 무관하게 INFO.
    """
    if verbose:
        return logging.INFO
    if quiet:
        return logging.WARNING
    return logging.INFO if subcommand in ("serve", "worker") else logging.WARNING


@click.group()
@click.version_option(__version__, "-V", "--version", message="춘추관 %(version)s")
@click.option("-v", "--verbose", is_flag=True, help="모든 명령에서 INFO 로그를 stderr 로 출력")
@click.option("-q", "--quiet", is_flag=True, help="경고 이상만 출력 (worker/serve 의 기본 INFO 를 끈다)")
@click.pass_context
def main(ctx: click.Context, verbose: bool, quiet: bool) -> None:
    """춘추관 — 개인 웹 아카이빙 시스템."""
    level = _console_level(verbose, quiet, ctx.invoked_subcommand)
    root = logging.getLogger()
    fresh = not root.handlers
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if fresh:
        # DB 적재(system_log)가 앱 로거를 INFO 로 낮춰도 콘솔 출력 수준은
        # 그대로 두기 위해 stderr 핸들러에 레벨을 직접 박는다.
        for handler in root.handlers:
            handler.setLevel(level)
        # WCCG_LOG_FILE 가 있으면 회전 파일에도 남긴다 (도커는 볼륨으로 노출).
        # 콘솔 레벨과 무관하게 INFO 이상을 담아 진단에 쓸 수 있게 한다.
        if config.LOG_FILE:
            from logging.handlers import RotatingFileHandler
            try:
                Path(config.LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
                file_handler = RotatingFileHandler(
                    config.LOG_FILE, maxBytes=config.LOG_FILE_MAX_BYTES,
                    backupCount=config.LOG_FILE_BACKUPS, encoding="utf-8",
                )
                file_handler.setLevel(logging.INFO)
                file_handler.setFormatter(logging.Formatter(
                    "%(asctime)s %(levelname)s %(name)s: %(message)s"))
                root.addHandler(file_handler)
            except OSError as e:
                logging.getLogger(__name__).warning(
                    "로그 파일(%s) 설정 실패 — 콘솔만 사용: %s", config.LOG_FILE, e)
    # 시스템 로그 적재 — serve/worker 는 별도 출처, 나머지 명령은 'cli'
    source = ctx.invoked_subcommand
    system_log.install(source if source in ("serve", "worker") else "cli")


@main.command()
@click.argument("url")
@click.option("--force", is_flag=True, help="콘텐츠가 동일해도 스냅샷 강제 저장")
def add(url: str, force: bool) -> None:
    """URL 아카이빙 작업을 큐에 추가한다.

    실제 캡처는 `wccg worker`(또는 serve 단일 프로세스)나 `wccg archive run`
    이 큐를 소비해 실행한다 — 모든 아카이빙을 한 프로세스로 통일해 스텔스
    캡처 설정(WCCG_CAPTURE_*)이 그 프로세스에만 있으면 되게 한다.
    """
    try:
        norm = storage.normalize_url(url)
    except ValueError as e:
        raise click.ClickException(str(e))
    if _warn_if_migrating():
        return
    with db.connect() as conn:
        queued = db.enqueue_archive_job(conn, norm, force=force, source="cli")
    if queued:
        click.echo(f"아카이빙 작업을 큐에 추가했습니다: {norm}")
        click.echo("  `wccg worker` 또는 `wccg archive run` 이 처리합니다.")
    else:
        click.echo(f"이미 큐에 있어 건너뜁니다: {norm}")


_ARCHIVE_STATUS_LABELS = {
    "new": "신규", "changed": "변경", "unchanged": "변경 없음",
    "forced_same": "동일(강제 저장)", "retry": "재시도 예약",
    "failed": "실패", "skipped": "건너뜀",
}


@main.group()
def archive() -> None:
    """단발 아카이빙 작업 큐 (add·대시보드·API 가 넣은 작업)."""


@archive.command("run")
def archive_run() -> None:
    """기한이 된 단발 아카이빙 작업을 모두 처리하고 종료 (cron 용).

    `wccg crawl run`/`schedule run` 과 대칭. worker 를 상주시키지 않는 배포에서
    cron 으로 돌려 큐를 소비한다 (serve/worker 가 돌고 있으면 자동 처리되므로 불필요).
    """
    from . import archive_worker
    if _warn_if_migrating():
        return
    ran = 0
    while True:
        step = archive_worker.process_next()
        if step is None:
            break
        ran += 1
        label = _ARCHIVE_STATUS_LABELS.get(step.status, step.status)
        click.echo(f"{step.url} — {label}"
                   + (f" ({step.error})" if step.error else ""))
    if ran == 0:
        click.echo("처리할 아카이빙 작업이 없습니다.")


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
    from . import differ
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


@main.command()
@click.argument("url")
@click.option(
    "--snapshot", "snapshot_idx", type=int, default=None,
    help="history 번호의 스냅샷 하나만 삭제 (생략 시 페이지 전체)",
)
@click.option(
    "--site", "whole_site", is_flag=True,
    help="URL 이 속한 사이트(서브도메인) 전체 삭제 — 페이지·크롤 회차·크롤 스케줄 포함",
)
@click.option(
    "--hard", is_flag=True,
    help="휴지통을 거치지 않고 즉시 영구 삭제 (휴지통 기능이 켜져 있어도)",
)
@click.option("--yes", is_flag=True, help="확인 없이 진행")
def delete(
    url: str, snapshot_idx: int | None, whole_site: bool, hard: bool, yes: bool
) -> None:
    """아카이브 삭제 — 페이지 전체, 단일 스냅샷(--snapshot N), 사이트 전체(--site).

    휴지통 기능이 켜져 있으면 페이지·사이트 삭제는 즉시 지우지 않고 휴지통으로
    옮긴다(wccg trash 로 복원/영구삭제, 보관 기간 경과 시 자동 삭제). --hard 는
    휴지통을 건너뛰고 즉시 영구 삭제한다. 단일 스냅샷(--snapshot)은 휴지통을 거치지
    않고 항상 즉시 삭제된다. 단일 스냅샷 삭제 시 다음 스냅샷의 변경 표시는 새 직전
    스냅샷 기준으로 자동 보정된다. 실행 로그(archive_logs)는 이력으로 남는다.
    """
    from . import deletion
    if whole_site:
        if snapshot_idx is not None:
            raise click.ClickException("--site 와 --snapshot 은 함께 쓸 수 없습니다")
        _delete_site(url, hard=hard, yes=yes)
        return
    with db.connect() as conn:
        page = _find_page(conn, url)
        snaps = db.list_snapshots(conn, page["id"])
        soft = (not hard) and db.trash_enabled(conn)

    if snapshot_idx is None:
        if not yes:
            verb = "휴지통으로 이동" if soft else "영구 삭제"
            tail = "" if soft else " 되돌릴 수 없습니다."
            click.confirm(
                f"{page['url']} — 스냅샷 {len(snaps)}개를 포함한 아카이브 전체를 "
                f"{verb}합니다.{tail} 계속할까요?",
                abort=True,
            )
        result = deletion.delete_page(page["id"], hard=hard)
        if result.trashed:
            click.echo(
                f"휴지통으로 이동: {result.url} (스냅샷 {result.snapshots_deleted}개)"
                " — wccg trash 로 복원/영구삭제"
            )
        else:
            click.echo(f"영구 삭제됨: {result.url} (스냅샷 {result.snapshots_deleted}개)")
        return

    if not (1 <= snapshot_idx <= len(snaps)):
        raise click.ClickException(
            f"잘못된 번호: --snapshot {snapshot_idx} (1 ~ {len(snaps)})"
        )
    snap = snaps[snapshot_idx - 1]
    if not yes:
        click.confirm(
            f"스냅샷 {snapshot_idx} ({snap['taken_at']}) 을 삭제합니다. "
            "되돌릴 수 없습니다. 계속할까요?",
            abort=True,
        )
    deletion.delete_snapshot(snap["id"])
    click.echo(f"삭제됨: {snap['dir_name']}")


def _delete_site(url: str, *, hard: bool, yes: bool) -> None:
    """URL 이 속한 사이트 전체 삭제 (delete --site 본체)."""
    from . import deletion
    key = storage.site_key(storage.normalize_url(url))
    with db.connect() as conn:
        site = db.get_site_by_key(conn, key)
        if site is None:
            raise click.ClickException(f"사이트 아카이브가 없습니다: {key}")
        pages = db.list_site_pages(conn, site["id"])
        crawls = db.list_site_crawls(conn, site["id"])
        soft = (not hard) and db.trash_enabled(conn)
    if not yes:
        verb = "휴지통으로 이동" if soft else "영구 삭제"
        tail = "" if soft else " 되돌릴 수 없습니다."
        click.confirm(
            f"{key} — 페이지 {len(pages)}개, 크롤 회차 {len(crawls)}개를 포함한 "
            f"사이트 아카이브 전체를 {verb}합니다.{tail} 계속할까요?",
            abort=True,
        )
    result = deletion.delete_site(site["id"], hard=hard)
    head = "휴지통으로 이동" if result.trashed else "영구 삭제됨"
    click.echo(
        f"{head}: {result.site_key} (페이지 {result.pages_deleted}개, "
        f"스냅샷 {result.snapshots_deleted}개, 크롤 {result.crawls_deleted}개)"
        + (" — wccg trash 로 복원/영구삭제" if result.trashed else "")
    )


@main.group()
def trash() -> None:
    """휴지통(삭제 보류 아카이브) 관리 — 목록·복원·영구삭제."""


def _resolve_trash_entry(conn, target: str):
    """id(숫자) 또는 label(URL/사이트키)로 휴지통 항목을 찾는다. 없으면 ClickException."""
    if target.isdigit():
        entry = db.get_trash_entry(conn, int(target))
        if entry is not None:
            return entry
    matches = [e for e in db.list_trash_entries(conn) if e["label"] == target]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise click.ClickException(
            f"여러 항목이 '{target}' 와 일치합니다 — id 로 지정하세요 (trash list)"
        )
    raise click.ClickException(f"휴지통 항목을 찾을 수 없습니다: {target}")


@trash.command("list")
def trash_list() -> None:
    """휴지통 항목 목록 (id·종류·대상·삭제 시각)."""
    with db.connect() as conn:
        entries = db.list_trash_entries(conn)
        retention = db.trash_retention_days(conn)
        enabled = db.trash_enabled(conn)
    if not entries:
        click.echo("휴지통이 비어 있습니다.")
        return
    state = "켜짐" if enabled else "꺼짐 (삭제 시 즉시 영구삭제)"
    auto = f"{retention}일 경과 시 자동 영구삭제" if retention > 0 else "자동 삭제 꺼짐"
    click.echo(f"휴지통: {state} · {auto}")
    for e in entries:
        kind = "사이트" if e["kind"] == "site" else "페이지"
        click.echo(
            f"  [{e['id']}] {kind}  {e['label']}  "
            f"(페이지 {e['page_count']}·스냅샷 {e['snapshot_count']}, "
            f"삭제 {e['deleted_at']})"
        )


@trash.command("restore")
@click.argument("target")
def trash_restore(target: str) -> None:
    """휴지통 항목 복원 — id 또는 URL/사이트키로 지정."""
    from . import deletion
    with db.connect() as conn:
        entry = _resolve_trash_entry(conn, target)
    restored = deletion.restore(entry["id"])
    click.echo(f"복원됨: {restored['label']} ({restored['kind']})")


@trash.command("purge")
@click.argument("target", required=False)
@click.option("--expired", is_flag=True, help="보관 기간이 지난 항목만 영구 삭제")
@click.option("--all", "all_entries", is_flag=True, help="휴지통의 모든 항목 영구 삭제")
@click.option("--yes", is_flag=True, help="확인 없이 진행")
def trash_purge(
    target: str | None, expired: bool, all_entries: bool, yes: bool
) -> None:
    """휴지통 항목 영구 삭제 — 되돌릴 수 없음.

    대상은 하나만: <id|URL> 단건 / --expired(보관 기간 경과분) / --all(전체).
    """
    from . import deletion
    if sum([bool(target), expired, all_entries]) != 1:
        raise click.ClickException(
            "대상을 하나만 지정하세요: <id|URL> | --expired | --all"
        )
    if expired:
        n = deletion.purge_expired()
        click.echo(
            f"보관 기간이 지난 {n}개 항목을 영구 삭제했습니다."
            if n else "영구 삭제할(기한 경과) 항목이 없습니다."
        )
        return
    if all_entries:
        with db.connect() as conn:
            ids = [e["id"] for e in db.list_trash_entries(conn)]
        if not ids:
            click.echo("휴지통이 비어 있습니다.")
            return
        if not yes:
            click.confirm(
                f"휴지통의 {len(ids)}개 항목을 모두 영구 삭제합니다. "
                "되돌릴 수 없습니다. 계속할까요?",
                abort=True,
            )
        for tid in ids:
            deletion.purge(tid)
        click.echo(f"{len(ids)}개 항목을 영구 삭제했습니다.")
        return
    with db.connect() as conn:
        entry = _resolve_trash_entry(conn, target)
    if not yes:
        click.confirm(
            f"{entry['label']} 을(를) 영구 삭제합니다. 되돌릴 수 없습니다. 계속할까요?",
            abort=True,
        )
    deletion.purge(entry["id"])
    click.echo(f"영구 삭제됨: {entry['label']} ({entry['kind']})")


@main.group()
def schedule() -> None:
    """주기적 자동 재아카이빙 관리 (최소 1시간 ~ 최대 1개월)."""


@schedule.command("add")
@click.argument("url")
@click.option(
    "--every", required=True,
    help="반복 주기 — 1h ~ 1mo (예: 1h, 90m, 12h, 3d, 1w, 1mo)",
)
@click.option(
    "--at", "at_time", default=None,
    help="실행 시각 HH:MM (서버 로컬 시간) — 1일 단위 주기에서만 (예: --every 1d --at 09:00)",
)
def schedule_add(url: str, every: str, at_time: str | None) -> None:
    """URL에 반복 주기를 등록/변경한다. 다음 실행은 지금 + 주기."""
    from . import scheduler
    try:
        seconds = scheduler.parse_interval(every)
        row = scheduler.set_schedule(url, seconds, run_at=at_time)
    except ValueError as e:
        raise click.ClickException(str(e))
    click.echo(
        f"스케줄 등록: {row['url']} — {scheduler.format_schedule(seconds, at_time)} 주기, "
        f"다음 실행 {row['next_run_at']}"
    )


@schedule.command("list")
def schedule_list() -> None:
    """등록된 스케줄 목록 (다음 실행이 가까운 순)."""
    from . import scheduler
    with db.connect() as conn:
        rows = db.list_schedules(conn)
    if not rows:
        click.echo("등록된 스케줄이 없습니다.")
        return
    click.echo(f"{'주기':<14}  {'다음 실행':<25}  {'마지막 실행':<25}  URL")
    for r in rows:
        label = scheduler.format_schedule(r["interval_seconds"], r["run_at_time"])
        click.echo(
            f"{label:<14}  "
            f"{r['next_run_at']:<25}  {r['last_run_at'] or '-':<25}  {r['url']}"
        )


@schedule.command("next")
@click.argument("url")
@click.argument("when")
def schedule_next(url: str, when: str) -> None:
    """URL 스케줄의 다음 실행 시각을 변경한다.

    WHEN 은 ISO 형식 (예: 2026-06-12T09:00). 타임존이 없으면 로컬 시간으로
    해석한다. 과거 시각을 주면 다음 폴링에서 즉시 실행된다.
    """
    try:
        dt = datetime.fromisoformat(when)
    except ValueError:
        raise click.ClickException(
            f"잘못된 시각 형식: {when!r} (예: 2026-06-12T09:00)"
        )
    if dt.tzinfo is None:
        dt = dt.astimezone()  # 로컬 타임존 부여
    from . import scheduler
    try:
        row = scheduler.set_next_run(url, dt)
    except ValueError as e:
        raise click.ClickException(str(e))
    click.echo(f"다음 실행 변경: {row['url']} — {row['next_run_at']}")


@schedule.command("remove")
@click.argument("url")
def schedule_remove(url: str) -> None:
    """URL의 스케줄을 해제한다."""
    from . import scheduler
    try:
        removed = scheduler.remove_schedule(url)
    except ValueError as e:
        raise click.ClickException(str(e))
    if not removed:
        raise click.ClickException("등록된 스케줄이 없는 URL 입니다")
    click.echo("스케줄 해제됨")


@schedule.command("run")
def schedule_run() -> None:
    """기한이 된 스케줄을 한 번 실행 (cron 용 — serve 중에는 자동 실행됨).

    크롤 스케줄도 함께 새 크롤로 등록한다 — 등록된 크롤의 페이지 처리는
    `wccg crawl run` (또는 serve 의 크롤러)이 맡는다.
    """
    from . import crawler, scheduler
    if _warn_if_migrating():
        return
    results = scheduler.run_due()
    crawl_steps = crawler.run_due_schedules()
    if not results and not crawl_steps:
        click.echo("실행할 스케줄이 없습니다.")
        return
    for r in results:
        click.echo(f"{r.url} — {r.status}" + (f" ({r.error})" if r.error else ""))
    _echo_crawl_schedule_steps(crawl_steps)


@main.group()
def crawl() -> None:
    """사이트 전체 아카이브 — 같은 호스트, 시작 URL 경로 프리픽스 이하."""


_CRAWL_STATUS_LABELS = {
    "new": "신규", "changed": "변경", "unchanged": "동일", "forced_same": "동일(강제)",
    "retry": "재시도 대기", "failed": "실패", "skipped": "건너뜀",
}


_CRAWL_OPTION_KWARGS: list[tuple[str, dict]] = [
    ("--max-pages", dict(
        default=None, type=int,
        help=f"수집할 최대 페이지 수 (1 ~ {config.CRAWL_MAX_PAGES_LIMIT}, "
             "기본: 시스템 설정)",
    )),
    ("--max-depth", dict(
        default=None, type=int,
        help=f"시작 URL 로부터의 최대 링크 깊이 (0 ~ {config.CRAWL_MAX_DEPTH_LIMIT}, "
             "기본: 시스템 설정)",
    )),
    ("--delay", dict(
        default=None, type=int,
        help="페이지 간 최소 간격(초) — 대상 서버 부담 방지 (기본: 시스템 설정)",
    )),
]


def _crawl_options(fn):
    """crawl add / crawl schedule add 공용 옵션 데코레이터."""
    for name, kwargs in reversed(_CRAWL_OPTION_KWARGS):
        fn = click.option(name, **kwargs)(fn)
    return fn


@crawl.command("add")
@click.argument("url")
@_crawl_options
@click.option(
    "--no-wait", is_flag=True,
    help="등록만 하고 종료 — 실행은 serve 의 크롤러가 큐를 소비",
)
def crawl_add(
    url: str, max_pages: int | None, max_depth: int | None,
    delay: int | None, no_wait: bool,
) -> None:
    """사이트 전체 아카이브를 등록하고 (기본) 완료될 때까지 실행한다.

    같은 시작 URL 의 크롤이 이미 진행 중이면 새로 만들지 않고 그 크롤에
    병합된다 (이번에 넘긴 옵션은 무시).
    """
    from . import crawler
    if _warn_if_migrating():
        return
    try:
        row, merged = crawler.start_crawl(
            url, max_pages=max_pages, max_depth=max_depth,
            delay_seconds=delay, source="cli",
        )
    except ValueError as e:
        raise click.ClickException(str(e))
    if merged:
        click.echo(
            f"같은 시작 URL 의 크롤 #{row['id']} 이(가) 진행 중 — 병합합니다 "
            f"(기존 옵션 유지: 최대 {row['max_pages']}페이지 · "
            f"깊이 {row['max_depth']} · 간격 {row['delay_seconds']}s)"
        )
    else:
        click.echo(
            f"크롤 #{row['id']} 등록: {row['start_url']} "
            f"(범위 {row['scope_host']}{row['scope_path']}, "
            f"최대 {row['max_pages']}페이지 · 깊이 {row['max_depth']} · "
            f"간격 {row['delay_seconds']}s)"
        )
    if no_wait:
        return

    def _echo(step: crawler.CrawlStep) -> None:
        label = _CRAWL_STATUS_LABELS.get(step.status, step.status)
        line = f"  {step.url} — {label}"
        if step.enqueued:
            line += f" (+링크 {step.enqueued}개)"
        if step.error:
            line += f" ({step.error})"
        click.echo(line)

    result = crawler.run_crawl(row["id"], on_step=_echo)
    with db.connect() as conn:
        counts = db.crawl_page_counts(conn, row["id"])
    click.echo(
        f"크롤 종료 [{result['status']}]: 완료 {counts['done']}개 · "
        f"실패 {counts['failed']}개 · 전체 {counts['total']}개"
    )


@crawl.command("list")
def crawl_list() -> None:
    """크롤 목록 (최신 순)."""
    with db.connect() as conn:
        rows = db.list_crawls(conn)
    if not rows:
        click.echo("등록된 크롤이 없습니다.")
        return
    click.echo(f"{'ID':>4}  {'상태':<10}  {'완료':>5}  {'실패':>5}  {'대기':>5}  시작 URL")
    for r in rows:
        click.echo(
            f"{r['id']:>4}  {r['status']:<10}  {r['done_count']:>5}  "
            f"{r['failed_count']:>5}  {r['pending_count']:>5}  {r['start_url']}"
        )


def _echo_crawl_schedule_steps(steps: list[crawler.ScheduleStep]) -> int:
    """크롤 스케줄 실행 결과 출력 — 등록된 크롤 수 반환 (deferred 는 조용히 넘어간다)."""
    started = 0
    for s in steps:
        if s.status == "started":
            started += 1
            click.echo(f"크롤 스케줄 실행: {s.start_url} → 크롤 #{s.crawl_id}")
        elif s.status == "error":
            click.echo(f"크롤 스케줄 실패: {s.start_url} ({s.error})")
    return started


@crawl.command("run")
def crawl_run() -> None:
    """기한이 된 크롤 페이지를 처리하고 종료 (cron 용 — serve 중에는 자동 실행됨).

    기한이 된 크롤 스케줄도 함께 새 크롤로 등록한다. 크롤마다 페이지 간
    간격이 강제되므로 한 번 실행에 크롤당 한 페이지꼴로 처리된다. 간격보다
    짧은 주기의 cron 으로 돌리면 큐가 계속 소비된다.
    """
    from . import crawler
    if _warn_if_migrating():
        return
    ran = _echo_crawl_schedule_steps(crawler.run_due_schedules())
    while True:
        step = crawler.process_next()
        if step is None:
            break
        ran += 1
        label = _CRAWL_STATUS_LABELS.get(step.status, step.status)
        click.echo(f"[#{step.crawl_id}] {step.url} — {label}"
                   + (f" ({step.error})" if step.error else ""))
        if step.crawl_done:
            click.echo(f"크롤 #{step.crawl_id} 완료")
    if ran == 0:
        click.echo("처리할 크롤 페이지가 없습니다.")


@crawl.group("schedule")
def crawl_schedule() -> None:
    """사이트 전체 아카이브의 주기적 재실행 관리 (최소 1시간 ~ 최대 1개월)."""


@crawl_schedule.command("add")
@click.argument("url")
@click.option(
    "--every", required=True,
    help="반복 주기 — 1h ~ 1mo (예: 1h, 90m, 12h, 3d, 1w, 1mo)",
)
@click.option(
    "--at", "at_time", default=None,
    help="실행 시각 HH:MM (서버 로컬 시간) — 1일 단위 주기에서만",
)
@_crawl_options
def crawl_schedule_add(
    url: str, every: str, at_time: str | None,
    max_pages: int | None, max_depth: int | None, delay: int | None,
) -> None:
    """시작 URL에 주기적 사이트 아카이브를 등록/변경한다. 다음 실행은 지금 + 주기."""
    from . import crawler, scheduler
    try:
        seconds = scheduler.parse_interval(every)
        row = crawler.set_crawl_schedule(
            url, seconds, run_at=at_time,
            max_pages=max_pages, max_depth=max_depth, delay_seconds=delay,
        )
    except ValueError as e:
        raise click.ClickException(str(e))
    click.echo(
        f"크롤 스케줄 등록: {row['start_url']} — "
        f"{scheduler.format_schedule(seconds, at_time)} 주기 "
        f"(최대 {row['max_pages']}페이지 · 깊이 {row['max_depth']} · "
        f"간격 {row['delay_seconds']}s), 다음 실행 {row['next_run_at']}"
    )


@crawl_schedule.command("list")
def crawl_schedule_list() -> None:
    """등록된 크롤 스케줄 목록 (다음 실행이 가까운 순)."""
    from . import scheduler
    with db.connect() as conn:
        rows = db.list_crawl_schedules(conn)
    if not rows:
        click.echo("등록된 크롤 스케줄이 없습니다.")
        return
    click.echo(
        f"{'주기':<14}  {'다음 실행':<25}  {'마지막 실행':<25}  "
        f"{'옵션(페이지·깊이·간격)':<22}  시작 URL"
    )
    for r in rows:
        label = scheduler.format_schedule(r["interval_seconds"], r["run_at_time"])
        options = f"{r['max_pages']}·{r['max_depth']}·{r['delay_seconds']}s"
        click.echo(
            f"{label:<14}  {r['next_run_at']:<25}  {r['last_run_at'] or '-':<25}  "
            f"{options:<22}  {r['start_url']}"
        )


@crawl_schedule.command("remove")
@click.argument("url")
def crawl_schedule_remove(url: str) -> None:
    """시작 URL의 크롤 스케줄을 해제한다."""
    from . import crawler
    try:
        removed = crawler.remove_crawl_schedule(url)
    except ValueError as e:
        raise click.ClickException(str(e))
    if not removed:
        raise click.ClickException("등록된 크롤 스케줄이 없는 URL 입니다")
    click.echo("크롤 스케줄 해제됨")


def _fmt_mb(n: int) -> str:
    return f"{n / 1048576:.1f}MB"


@main.command()
@click.option("--yes", is_flag=True, help="확인 없이 진행")
def compact(yes: bool) -> None:
    """저장공간 최적화 — 압축 변환 + 인라인 스타일 추출 + 자원 참조 백필 + 고아 자원 정리.

    구형 스냅샷을 압축 저장 형태(공유 자원 추출 + HTML gzip + 스크린샷 WebP
    + 문서 CAS 이전)로 변환하고, 큰 인라인 <style>(사이트 공통 CSS)을 공유
    자원으로 추출하고, 자원 참조(snapshot_resources)가 없는 스냅샷을 스캔해
    인덱스한 뒤, 어떤 스냅샷도 참조하지 않는 공유 자원을 삭제한다. 내용
    보존이라 스냅샷이 담는 정보는 그대로다 (불변 원칙의 유일한 예외).
    멱등 — 여러 번 실행해도 안전하다.
    """
    from . import optimize
    compactable, css_pending, unindexed = optimize.pending_counts()
    if compactable == 0 and css_pending == 0 and unindexed == 0:
        click.echo("최적화할 항목이 없습니다 — 스냅샷이 모두 압축·인덱스 형태입니다.")
        return
    if not yes:
        click.confirm(
            f"스냅샷 {compactable}개를 압축 저장 형태(page.html.gz·"
            "raw.html.gz·screenshot.webp + 공유 자원 + 문서 CAS)로 변환하고, "
            f"{css_pending}개 스냅샷의 인라인 스타일을 공유 자원으로 추출하고, "
            f"{unindexed}개의 자원 참조를 인덱스한 뒤 참조 없는 공유 자원을 "
            "정리합니다. 계속할까요?",
            abort=True,
        )

    result = optimize.run()
    c = result.compact
    click.echo(
        f"변환 {c.converted}/{c.total}개 · "
        f"공유 자원 {c.externalized}개 추출 · "
        f"문서 {c.documents}개 이전 · "
        f"{_fmt_mb(c.before_bytes)} → {_fmt_mb(c.after_bytes)} "
        f"({_fmt_mb(c.saved_bytes)} 절약)"
    )
    click.echo(
        f"인라인 스타일 추출: 스냅샷 {result.styles_snapshots}개에서 "
        f"공유 스타일 {result.styles_extracted}개 "
        f"({_fmt_mb(result.styles_saved_bytes)} 절약)"
    )
    click.echo(f"자원 참조 백필: 스냅샷 {result.indexed}개")
    if result.sweep_skipped:
        click.echo("고아 자원 정리는 건너뛰었습니다 — 참조 미기록 스냅샷이 남아 있습니다.")
    else:
        click.echo(
            f"고아 자원 정리: {result.swept}개 삭제 ({_fmt_mb(result.swept_bytes)})"
        )


class _SearchGroup(click.Group):
    """`wccg search <검색어>` 와 하위 명령(reindex/status)을 함께 지원.

    첫 인자가 알려진 하위 명령이 아니고 옵션도 아니면 검색어로 보고 숨김
    'query' 명령으로 넘긴다 (variadic 인자와 하위 명령이 충돌하지 않게).
    """

    def parse_args(self, ctx, args):
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = ["query"] + args
        return super().parse_args(ctx, args)


@main.group("storage")
def storage_group() -> None:
    """blob 저장 백엔드 상태 — wccg storage status (읽기 전용)."""


def _fmt_bytes(n: int) -> str:
    """사람이 읽는 용량 표기 (CLI 표시용)."""
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024 or unit == "TB":
            return f"{val:.0f}{unit}" if unit == "B" else f"{val:.1f}{unit}"
        val /= 1024
    return f"{val:.1f}TB"


@storage_group.command("status")
@click.option("--scan", is_flag=True, help="S3 사용량을 지금 스캔(전 객체 조회 — 부하 주의)")
def storage_status_cmd(scan: bool) -> None:
    """활성 백엔드·S3 가용성(env)·마이그레이션 상태·정리 대기·사용량 표시 (읽기 전용).

    사용량은 캐시만 읽는다(요청 경로 S3 미호출) — 갱신은 --scan.
    """
    from . import config, db, storage, storage_usage

    if scan:
        with db.connect() as conn:
            is_s3 = db.storage_backend(conn) == "s3"
        if is_s3:
            try:
                storage_usage.scan_s3_usage()
            except Exception as e:  # noqa: BLE001 — 스캔 실패는 안내만 (비밀값 미출력)
                click.echo(f"사용량 스캔 실패: {type(e).__name__}")
        else:
            click.echo("--scan 은 S3 모드에서만 의미가 있습니다 (현재 로컬).")

    with db.connect() as conn:
        active = db.storage_backend(conn)
        paused = db.writes_paused(conn)
        summary = db.storage_migration_summary(conn)
        s3_usage = db.s3_usage(conn)
    click.echo(f"활성 백엔드: {active}")
    # S3 자격증명은 유무만 표시 — 비밀값(access key/secret)은 절대 출력하지 않는다
    env_ok = bool(
        config.S3_BUCKET and config.S3_ACCESS_KEY_ID and config.S3_SECRET_ACCESS_KEY
    )
    click.echo(f"S3 자격증명(env): {'완전' if env_ok else '미설정/불완전'}")
    if config.S3_BUCKET:
        loc = f"s3://{config.S3_BUCKET}"
        if config.S3_PREFIX:
            loc += f"/{config.S3_PREFIX}"
        click.echo(f"S3 위치: {loc}")
    click.echo(f"쓰기 일시중지: {'예' if paused else '아니오'}")
    if summary:
        click.echo(
            f"마이그레이션: {summary.get('status')} (방향 {summary.get('direction')})"
        )
        if summary.get("cleanup_pending"):
            click.echo(
                f"  ⚠ 원본 정리 대기 — 원본 위치: {summary.get('source_location')}"
            )
    else:
        click.echo("마이그레이션: 이력 없음")
    # 사용량
    if active == "s3":
        local = storage.local_usage()
        click.echo(
            f"로컬 사용량: DB {_fmt_bytes(local['db'])}, 캐시 {_fmt_bytes(local['cache'])}, "
            f"read-through {_fmt_bytes(local['blobcache'])}"
        )
        if s3_usage:
            cats = s3_usage.get("categories", {})
            parts = ", ".join(
                f"{k} {_fmt_bytes(v)}" for k, v in cats.items() if v
            ) or "(빈 버킷)"
            click.echo(
                f"Object Storage 사용량 [{s3_usage.get('scanned_at', '?')}]: {parts} "
                f"(총 {_fmt_bytes(s3_usage.get('total', 0))})"
            )
        else:
            click.echo("Object Storage 사용량: 미조회 (--scan 으로 갱신)")
    else:
        usage = storage.archive_disk_usage()
        click.echo(
            "로컬 사용량: " + ", ".join(f"{k} {_fmt_bytes(v)}" for k, v in usage.items())
        )


@main.group("db-backup", invoke_without_command=True)
@click.pass_context
def db_backup_group(ctx: click.Context) -> None:
    """S3 DB 백업 — wccg db-backup (즉시 1회) | status (읽기 전용)."""
    if ctx.invoked_subcommand is not None:
        return
    from . import db_backup

    try:
        meta = db_backup.run_blocking()
    except RuntimeError as e:
        raise click.ClickException(str(e))
    except Exception:
        raise click.ClickException("DB 백업에 실패했습니다.")
    click.echo(f"백업 완료: {meta['last_key']} ({meta['last_bytes']} bytes)")


@db_backup_group.command("status")
def db_backup_status_cmd() -> None:
    """마지막 백업 시각·결과·보존 설정·백업 개수를 표시 (읽기 전용)."""
    from . import db_backup

    st = db_backup.status()
    if not st["s3_mode"]:
        click.echo("DB 백업: S3 모드에서만 사용 가능 (현재 로컬)")
        return
    click.echo(f"주기: {st['interval_hours']}시간 / 보존: {st['keep']}개")
    if st.get("last_at"):
        click.echo(f"마지막 백업: {st['last_at']} ({st.get('last_status')})")
        if st.get("last_error"):
            click.echo(f"  마지막 오류: {st['last_error']}")
    else:
        click.echo("마지막 백업: 없음")
    click.echo(f"S3 백업 개수: {st.get('count', 0)}")
    if st.get("list_error"):
        click.echo(f"  목록 조회 오류: {st['list_error']}")


@main.group(cls=_SearchGroup)
def search() -> None:
    """아카이브 전문 검색 — wccg search <검색어> | reindex | status.

    한국어는 부분문자열(3글자 이상)로 찾고, 1~2글자는 부분일치로 폴백한다.
    """


@search.command("query", hidden=True)
@click.argument("query", nargs=-1)
@click.option("--domain", "-d", default=None, help="도메인 한정 (예: example.com)")
@click.option("--limit", "-n", type=int, default=20, show_default=True, help="결과 수")
@click.option("--latest", is_flag=True, help="URL 당 최신 스냅샷 1건만")
def search_query(
    query: tuple[str, ...],
    domain: str | None,
    limit: int,
    latest: bool,
) -> None:
    """검색 수행 (wccg search <검색어> 의 실제 동작)."""
    text = " ".join(query).strip()
    if not text:
        click.echo("검색어를 입력하세요 — 예: wccg search 헌법")
        return
    if not searchindex.available():
        raise click.ClickException(
            "검색 인덱스를 쓸 수 없습니다 — 이 SQLite 빌드에 FTS5 가 없습니다."
        )
    results = searchindex.search(text, domain=domain, latest_only=latest, limit=limit)
    if results.mode == "empty":
        click.echo("검색어가 비어 있습니다.")
        return
    if not results.hits:
        click.echo("일치하는 결과가 없습니다.")
        return
    mode_note = " (부분일치 폴백)" if results.mode == "like" else ""
    click.echo(f"총 {results.total}건{mode_note} — 상위 {len(results.hits)}건:\n")
    for hit in results.hits:
        when = hit.taken_at[:19].replace("T", " ")
        click.echo(f"  {when}  {hit.page_url}")
        if hit.title:
            click.echo(f"    {hit.title}")
        if hit.snippet:
            click.echo(f"    {hit.snippet}")
        click.echo(f"    snapshot #{hit.snapshot_id}")
        click.echo("")


@search.command("reindex")
@click.option("--all", "rebuild", is_flag=True, help="전체 재색인 (인덱스 비우고 다시 빌드)")
def search_reindex(rebuild: bool) -> None:
    """미색인 스냅샷을 검색 인덱스에 백필 (--all 은 전체 재색인)."""
    if not searchindex.available():
        raise click.ClickException(
            "검색 인덱스를 쓸 수 없습니다 — 이 SQLite 빌드에 FTS5 가 없습니다."
        )
    if rebuild:
        count = searchindex.reindex_all()
        click.echo(f"전체 재색인 완료 — 스냅샷 {count}개")
    else:
        pending = searchindex.pending_count()
        if pending == 0:
            click.echo("색인할 스냅샷이 없습니다 — 모두 최신 상태입니다.")
            return
        count = searchindex.backfill_all()
        click.echo(f"검색 인덱스 백필 완료 — 스냅샷 {count}개")


@search.command("status")
def search_status() -> None:
    """검색 인덱스 상태 — 가용 여부와 미색인 스냅샷 수."""
    if not searchindex.available():
        click.echo("검색 인덱스: 비활성 (이 SQLite 빌드에 FTS5 없음)")
        return
    pending = searchindex.pending_count()
    if pending:
        click.echo(f"검색 인덱스: 활성 · 미색인 스냅샷 {pending}개 (wccg search reindex 로 색인)")
    else:
        click.echo("검색 인덱스: 활성 · 모든 스냅샷 색인됨")


@search.command("verify")
@click.option("--repair", is_flag=True, help="발견한 불일치를 교정 (orphan 삭제 + 누락 재색인)")
def search_verify(repair: bool) -> None:
    """검색 인덱스 정합성 점검 — 플래그와 실제 FTS 행의 불일치를 찾는다.

    과소 색인(색인됨으로 표시됐지만 FTS 행 없음)·orphan(스냅샷 없는 FTS 행)을
    보고한다 — 미색인 카운트만으로는 못 잡는 부류다. --repair 로 교정한다.
    """
    if not searchindex.available():
        click.echo("검색 인덱스: 비활성 (이 SQLite 빌드에 FTS5 없음)")
        return
    report = searchindex.verify()
    click.echo(
        f"색인됨 {report.indexed} · 미색인(pending) {report.pending} · "
        f"FTS 행 {report.fts_rows}"
    )
    click.echo(f"과소 색인 {report.missing} · orphan {report.orphan}")
    if report.consistent:
        click.echo("정합성 정상 — 플래그와 FTS 행이 일치합니다.")
    elif not repair:
        click.echo("불일치 발견 — 'wccg search verify --repair' 로 교정하세요.")
    if repair:
        result = searchindex.repair()
        click.echo(
            f"교정 완료 — orphan {result.orphans_removed}개 삭제 · "
            f"재색인 {result.reindexed}개"
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
    """전체 백업 파일(.ccg.backup) 생성 — DB(인증 포함)·스냅샷 파일·rules.json.

    S3 모드에서는 비활성화된다 (blob 이 로컬에 없어 일관 백업 불가).
    """
    try:
        out = backup_mod.create_backup(dest)
    except RuntimeError as e:  # S3 모드 차단 안내
        raise click.ClickException(str(e))
    except OSError as e:
        raise click.ClickException(f"백업 실패: {e}")
    click.echo(f"백업 생성: {out}")


@main.command()
@click.argument("src", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--yes", is_flag=True, help="확인 없이 진행")
def restore(src: Path, yes: bool) -> None:
    """전체 백업에서 복원 — 현재 아카이브 루트를 백업 시점 상태로 교체.

    S3 모드에서는 비활성화된다 (전체 백업과 짝).
    """
    from . import config as _config

    if _config.active_backend() == "s3":
        raise click.ClickException(
            "전체 복원은 S3 모드에서 비활성화됩니다 — S3 DB백업 복원(첫 구동 setup)이나 "
            "내보내기/가져오기를 사용하세요."
        )
    if not backup_mod.is_backup_filename(src.name):
        raise click.ClickException(
            f"복원은 {backup_mod.BACKUP_SUFFIX} 확장자 파일만 받습니다: {src.name}"
        )
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
    """아카이브 데이터만 내보내기 — 페이지·스냅샷·확인 기록·크롤 회차·인증서·
    아카이브 로그 + 파일 (인증 데이터 제외)."""
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
    if not backup_mod.is_export_filename(src.name):
        raise click.ClickException(
            f"가져오기는 {backup_mod.EXPORT_SUFFIX} 확장자 파일만 받습니다: {src.name}"
        )
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
        f"확인 기록 +{result.checks_added}, 크롤 +{result.crawls_added}, "
        f"인증서 +{result.certificates_added}, 로그 +{result.logs_added}"
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


@main.command()
@click.option(
    "--workers", "crawl_workers", default=None, type=int,
    help=f"크롤 스레드 수 = 동시 진행 크롤 수 "
         f"(기본 WCCG_CRAWL_WORKERS 또는 {config.CRAWL_WORKERS}, "
         f"1~{config.CRAWL_WORKERS_LIMIT})",
)
def worker(crawl_workers: int | None) -> None:
    """아카이빙 워커 — 스케줄·크롤 큐를 이 프로세스에서 소비 (종료는 Ctrl-C).

    대시보드(serve)와 분리해 돌리면 아카이빙 부하가 UI 응답에 영향을 주지
    않는다. 이때 serve 쪽 내장 폴링은 WCCG_SCHEDULER=off 로 끌 것 —
    켜 두면 페이지 스케줄이 양쪽에서 중복 실행될 수 있다.
    """
    import signal
    import threading

    from . import worker as worker_mod

    n = crawl_workers if crawl_workers is not None else config.CRAWL_WORKERS
    if not (1 <= n <= config.CRAWL_WORKERS_LIMIT):
        raise click.ClickException(
            f"크롤 스레드 수는 1 이상 {config.CRAWL_WORKERS_LIMIT} 이하여야 "
            f"합니다 (현재 {n})"
        )

    stop = threading.Event()

    def _request_stop(signum, frame) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    click.echo(f"워커 시작 — 크롤 스레드 {n}개 (종료: Ctrl-C)")
    worker_mod.run(stop, crawl_workers=n)
    click.echo("워커 종료")


if __name__ == "__main__":
    main()
