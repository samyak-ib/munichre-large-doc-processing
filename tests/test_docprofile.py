"""`Layout.reported_row_count` and the page-count framing in `layout_prompt`."""

from __future__ import annotations

from lossrun.docprofile import Layout
from lossrun.prompts import layout_prompt


def test_reported_row_count_reads_a_plain_integer():
    assert Layout(data={"reported_row_count": 20}).reported_row_count == 20


def test_reported_row_count_is_none_when_absent():
    assert Layout(data={}).reported_row_count is None


def test_reported_row_count_is_none_when_explicitly_null():
    assert Layout(data={"reported_row_count": None}).reported_row_count is None


def test_reported_row_count_rejects_non_numeric_junk():
    assert Layout(data={"reported_row_count": "about 20"}).reported_row_count is None


def test_reported_row_count_rejects_a_negative_number():
    assert Layout(data={"reported_row_count": -1}).reported_row_count is None


def test_reported_row_count_rejects_a_bool():
    """`isinstance(True, int)` is True in Python — guard against a stray
    boolean surviving JSON parsing and being read as 0 or 1 rows."""
    assert Layout(data={"reported_row_count": True}).reported_row_count is None


def test_layout_prompt_tells_the_model_it_can_count_directly_when_pages_cover_the_whole_document():
    prompt = layout_prompt(5, 5, document_label="loss run", row_label="claim")
    assert "complete document" in prompt
    assert "claim rows directly" in prompt


def test_layout_prompt_tells_the_model_to_return_null_rather_than_guess_on_a_longer_document():
    prompt = layout_prompt(5, 40, document_label="loss run", row_label="claim")
    assert "opening pages of a longer document" in prompt
    assert "return null rather than" in prompt
