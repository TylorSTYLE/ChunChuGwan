"""CLI. 모든 쓰기 작업의 유일한 진입점(대시보드도 내부적으로 이 레이어 호출)."""

from __future__ import annotations

import click


@click.group()
def main() -> None:
    """개인 웹 아카이빙 시스템."""


@main.command()
@click.argument("url")
@click.option("--force", is_flag=True, help="콘텐츠가 동일해도 스냅샷 강제 저장")
def add(url: str, force: bool) -> None:
    """URL을 아카이빙한다.

    TODO(M2): 흐름 —
    1. storage.normalize_url → db.get_or_create_page
    2. capture.capture → extract.extract_text → extract.normalize → 해시
    3. 직전 스냅샷과 해시 동일 && not force → db.insert_check 후
       "변경 없음 (마지막 스냅샷 {시각})" 출력하고 종료. 단, 이때 생성한
       임시 스냅샷 디렉토리는 정리할 것 (해시 계산 전에 디렉토리를 만들지
       않는 순서로 구현하는 게 더 깔끔함)
    4. 다르면 스냅샷 디렉토리 확정, content.md/meta.json 기록,
       db.insert_snapshot(changed=직전 존재 여부 기준)
    """
    raise click.ClickException("미구현 (M2)")


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
