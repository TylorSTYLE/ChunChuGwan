"""스냅샷 간 비교. 텍스트 diff(M3) + 스크린샷 픽셀 diff(M5)."""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops

from . import config


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


# 안티앨리어싱 미세 차이를 변경으로 치지 않기 위한 채널 차이 임계값
_PIXEL_THRESHOLD = 16


def _pad(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """size 보다 작으면 흰색 캔버스 좌상단에 붙여 확장."""
    if img.size == size:
        return img
    canvas = Image.new("RGB", size, (255, 255, 255))
    canvas.paste(img, (0, 0))
    return canvas


def diff_screenshots(old_png: Path, new_png: Path, out_png: Path) -> float:
    """스크린샷 픽셀 비교. 변경 픽셀 비율(0.0~1.0) 반환, 하이라이트 이미지 저장.

    크기가 다르면 큰 쪽에 맞춰 흰색 패딩 후 비교. 하이라이트는 새 스크린샷
    위에 변경 픽셀을 적색으로 칠한 이미지.
    """
    old_img = Image.open(old_png).convert("RGB")
    new_img = Image.open(new_png).convert("RGB")
    size = (max(old_img.width, new_img.width), max(old_img.height, new_img.height))
    old_img, new_img = _pad(old_img, size), _pad(new_img, size)

    mask = (
        ImageChops.difference(old_img, new_img)
        .convert("L")
        .point(lambda v: 255 if v > _PIXEL_THRESHOLD else 0)
    )
    changed = mask.histogram()[255]
    ratio = changed / (size[0] * size[1])

    red = Image.new("RGB", size, (220, 38, 38))
    highlight = Image.composite(red, new_img, mask)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    highlight.save(out_png)
    return ratio


def cached_screenshot_diff(old_png: Path, new_png: Path, cache_key: str) -> tuple[float, Path]:
    """픽셀 diff 결과를 config.CACHE_DIR 에 캐시. (변경 비율, 하이라이트 경로) 반환.

    스냅샷은 불변이므로 같은 cache_key 의 결과는 영구히 유효하다.
    """
    out_png = config.CACHE_DIR / f"{cache_key}.png"
    meta_path = config.CACHE_DIR / f"{cache_key}.json"
    if out_png.is_file() and meta_path.is_file():
        return json.loads(meta_path.read_text(encoding="utf-8"))["ratio"], out_png
    ratio = diff_screenshots(old_png, new_png, out_png)
    meta_path.write_text(json.dumps({"ratio": ratio}), encoding="utf-8")
    return ratio, out_png
