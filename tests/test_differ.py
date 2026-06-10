"""diff 테스트. M3에서 구현과 함께 통과시킬 것."""
from archiver import differ


def test_identical():
    d = differ.diff_text("a\nb\n", "a\nb\n")
    assert d.identical and d.added == 0 and d.removed == 0


def test_added_removed_counts():
    d = differ.diff_text("a\nb\nc\n", "a\nx\nc\nd\n")
    assert not d.identical
    assert d.added == 2 and d.removed == 1
