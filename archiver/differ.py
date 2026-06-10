"""스냅샷 간 비교. 텍스트 diff(M3) + 스크린샷 픽셀 diff(M5)."""

from __future__ import annotations

import difflib
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

    rows의 tag ∈ {"equal","insert","delete","replace"}. replace 구간은
    좌우 줄 수가 다르면 짧은 쪽을 빈 문자열로 채운다.
    """
    old_lines = old.splitlines()
    new_lines = new.splitlines()

    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    added = removed = 0
    rows: list[tuple[str, str, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append(("equal", old_lines[i1 + k], new_lines[j1 + k]))
        elif tag == "delete":
            removed += i2 - i1
            for line in old_lines[i1:i2]:
                rows.append(("delete", line, ""))
        elif tag == "insert":
            added += j2 - j1
            for line in new_lines[j1:j2]:
                rows.append(("insert", "", line))
        else:  # replace
            removed += i2 - i1
            added += j2 - j1
            left, right = old_lines[i1:i2], new_lines[j1:j2]
            for k in range(max(len(left), len(right))):
                rows.append((
                    "replace",
                    left[k] if k < len(left) else "",
                    right[k] if k < len(right) else "",
                ))

    identical = old_lines == new_lines
    unified = ""
    if not identical:
        unified = "\n".join(
            difflib.unified_diff(
                old_lines, new_lines, fromfile="old", tofile="new",
                n=context, lineterm="",
            )
        )
    return TextDiff(added=added, removed=removed, unified=unified, rows=rows,
                    identical=identical)


def diff_screenshots(old_png: Path, new_png: Path, out_png: Path) -> float:
    """스크린샷 픽셀 비교. 변경 픽셀 비율(0.0~1.0) 반환, 하이라이트 이미지 저장.

    TODO(M5): Pillow ImageChops.difference 기반. 크기 다르면 큰 쪽에 맞춰
    패딩 후 비교.
    """
    raise NotImplementedError
