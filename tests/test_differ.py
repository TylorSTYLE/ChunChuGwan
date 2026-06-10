"""diff 테스트. M3에서 구현과 함께 통과시킬 것."""
import pytest
from PIL import Image

from chunchugwan import config, differ


def test_identical():
    d = differ.diff_text("a\nb\n", "a\nb\n")
    assert d.identical and d.added == 0 and d.removed == 0


def test_added_removed_counts():
    d = differ.diff_text("a\nb\nc\n", "a\nx\nc\nd\n")
    assert not d.identical
    assert d.added == 2 and d.removed == 1


def test_side_by_side_rows():
    d = differ.diff_text("a\nb\nc\n", "a\nx\nc\nd\n")
    assert d.rows == [
        ("equal", "a", "a"),
        ("replace", "b", "x"),
        ("equal", "c", "c"),
        ("insert", "", "d"),
    ]


def test_replace_pads_shorter_side():
    d = differ.diff_text("a\nb\n", "x\n")
    assert d.rows == [("replace", "a", "x"), ("replace", "b", "")]
    assert d.added == 1 and d.removed == 2


def test_unified_contains_markers():
    d = differ.diff_text("a\nb\n", "a\nc\n")
    assert "-b" in d.unified and "+c" in d.unified
    assert d.unified.startswith("--- old")


def _png(path, size=(10, 10), color=(255, 255, 255), box=None, box_color=(0, 0, 0)):
    img = Image.new("RGB", size, color)
    if box:
        for x in range(box[0], box[2]):
            for y in range(box[1], box[3]):
                img.putpixel((x, y), box_color)
    img.save(path)
    return path


def test_diff_screenshots_identical(tmp_path):
    a = _png(tmp_path / "a.png")
    b = _png(tmp_path / "b.png")
    out = tmp_path / "out.png"
    assert differ.diff_screenshots(a, b, out) == 0.0
    assert out.is_file()


def test_diff_screenshots_changed_ratio(tmp_path):
    a = _png(tmp_path / "a.png")
    b = _png(tmp_path / "b.png", box=(0, 0, 2, 2))  # 4/100 픽셀 변경
    ratio = differ.diff_screenshots(a, b, tmp_path / "out.png")
    assert ratio == pytest.approx(0.04)


def test_diff_screenshots_pads_different_sizes(tmp_path):
    a = _png(tmp_path / "a.png", size=(10, 10))
    b = _png(tmp_path / "b.png", size=(10, 20), color=(255, 255, 255))
    ratio = differ.diff_screenshots(a, b, tmp_path / "out.png")
    assert ratio == 0.0  # 패딩(흰색) == 확장분(흰색) → 변경 없음
    assert Image.open(tmp_path / "out.png").size == (10, 20)


def test_cached_screenshot_diff(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    a = _png(tmp_path / "a.png")
    b = _png(tmp_path / "b.png", box=(0, 0, 2, 2))

    ratio1, out1 = differ.cached_screenshot_diff(a, b, "k1")
    calls = []
    monkeypatch.setattr(differ, "diff_screenshots", lambda *args: calls.append(args) or 0.0)
    ratio2, out2 = differ.cached_screenshot_diff(a, b, "k1")

    assert ratio1 == ratio2 == pytest.approx(0.04)
    assert out1 == out2 and out1.is_file()
    assert calls == []  # 두 번째 호출은 캐시 사용
