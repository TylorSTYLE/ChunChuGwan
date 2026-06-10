"""diff 테스트. M3에서 구현과 함께 통과시킬 것."""
from archiver import differ


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
