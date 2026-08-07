"""Page windowing: full coverage, correct overlap, and no infinite advance."""

from __future__ import annotations

import pytest

from lossrun.chunking import page_windows


def test_document_under_the_ceiling_is_one_window():
    assert page_windows(10, 50, 2) == [(1, 10)]
    assert page_windows(50, 50, 2) == [(1, 50)]


def test_long_document_windows_overlap_by_the_configured_pages():
    assert page_windows(120, 50, 2) == [(1, 50), (49, 98), (97, 120)]


def test_every_page_appears_in_at_least_one_window():
    for total in (1, 7, 49, 51, 99, 100, 251):
        covered: set[int] = set()
        for start, end in page_windows(total, 50, 2):
            covered.update(range(start, end + 1))
        assert covered == set(range(1, total + 1)), f"gap at total={total}"


def test_consecutive_windows_share_exactly_the_overlap():
    windows = page_windows(200, 50, 3)
    for (_, prev_end), (next_start, _) in zip(windows, windows[1:]):
        assert next_start == prev_end - 2


def test_zero_overlap_is_allowed():
    assert page_windows(100, 50, 0) == [(1, 50), (51, 100)]


def test_overlap_at_or_above_window_size_still_advances():
    # A 50-page overlap on a 50-page window would loop forever if taken literally.
    windows = page_windows(120, 50, 50)
    assert windows[0] == (1, 50)
    assert windows[1][0] > 1
    assert windows[-1][1] == 120
    assert len(windows) < 120


def test_empty_and_invalid_inputs():
    assert page_windows(0, 50, 2) == []
    with pytest.raises(ValueError):
        page_windows(10, 0, 2)
