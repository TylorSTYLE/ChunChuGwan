"""스냅샷 간 비교. 텍스트 diff(M3) + 스크린샷 픽셀 diff(M5)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class TextDiff:
    added: int                    # 추가된 줄 수
    removed: int                  # 삭제된 줄 수
    unified: str                  # unified diff 전문 (CLI 출력용)
    rows: list[tuple[str, str, str]]  # (tag, left, right) side-by-side 용
    identical: bool


def diff_text(old: str, new: str, context: int = 3) -> TextDiff:
    """정규화된 content.md 두 개를 비교.

    TODO(M3): difflib.unified_diff + SequenceMatcher.get_opcodes 로
    side-by-side rows 생성. tag ∈ {"equal","insert","delete","replace"}.
    """
    raise NotImplementedError


def diff_screenshots(old_png: Path, new_png: Path, out_png: Path) -> float:
    """스크린샷 픽셀 비교. 변경 픽셀 비율(0.0~1.0) 반환, 하이라이트 이미지 저장.

    TODO(M5): Pillow ImageChops.difference 기반. 크기 다르면 큰 쪽에 맞춰
    패딩 후 비교.
    """
    raise NotImplementedError
