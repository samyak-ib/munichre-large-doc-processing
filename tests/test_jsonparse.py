"""Payload parsing: clean JSON, fenced JSON, prose-wrapped JSON, and truncation.

Truncation is the failure that drops middle rows, so a cut-off response must
salvage every complete row and report itself as truncated rather than raising.
"""

from __future__ import annotations

import pytest

from lossrun.jsonparse import PayloadError, parse_table_payload

CLEAN = """{"columns": ["Policy Number", "Claim Number"],
 "rows": [["P-1", "C003"], ["P-1", "C004"]],
 "truncated": false,
 "last_row_key": ["P-1", "C004", "Acme"]}"""


def test_clean_payload():
    payload = parse_table_payload(CLEAN)
    assert payload.columns == ["Policy Number", "Claim Number"]
    assert payload.rows == [["P-1", "C003"], ["P-1", "C004"]]
    assert payload.truncated is False
    assert payload.repaired is False
    assert payload.last_row_key == ["P-1", "C004", "Acme"]


def test_fenced_payload():
    payload = parse_table_payload(f"```json\n{CLEAN}\n```")
    assert len(payload.rows) == 2


def test_prose_wrapped_payload():
    payload = parse_table_payload(f"Here is the table you asked for:\n\n{CLEAN}\n\nLet me know.")
    assert len(payload.rows) == 2


def test_truncated_payload_keeps_complete_rows():
    cut = '{"columns": ["Policy Number", "Claim Number"], "rows": [["P-1", "C003"], ["P-1", "C0'
    payload = parse_table_payload(cut)
    assert payload.rows == [["P-1", "C003"]]
    assert payload.truncated is True
    assert payload.repaired is True
    assert payload.columns == ["Policy Number", "Claim Number"]


def test_truncated_payload_with_many_rows_keeps_all_whole_ones():
    rows = ", ".join(f'["P-1", "C{i:03d}"]' for i in range(40))
    cut = f'{{"columns": ["Policy Number", "Claim Number"], "rows": [{rows}, ["P-1", "C04'
    payload = parse_table_payload(cut)
    assert len(payload.rows) == 40
    assert payload.rows[-1] == ["P-1", "C039"]
    assert payload.truncated is True


def test_model_declared_truncation_is_honoured_without_repair():
    payload = parse_table_payload(
        '{"columns": ["a"], "rows": [["1"]], "truncated": true, "last_row_key": ["1"]}'
    )
    assert payload.truncated is True
    assert payload.repaired is False


def test_brace_inside_a_string_does_not_end_the_object():
    payload = parse_table_payload(
        '{"columns": ["Description"], "rows": [["fell on aisle {3} near door"]]}'
    )
    assert payload.rows == [["fell on aisle {3} near door"]]


def test_nulls_and_numbers_become_text():
    payload = parse_table_payload('{"columns": ["a", "b"], "rows": [[null, 1234.5]]}')
    assert payload.rows == [["N/A", "1234.5"]]


def test_unusable_responses_raise():
    with pytest.raises(PayloadError):
        parse_table_payload("")
    with pytest.raises(PayloadError):
        parse_table_payload("I could not find a table in this document.")
