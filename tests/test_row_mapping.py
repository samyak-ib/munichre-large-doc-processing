"""Mapping a model's positional row arrays onto column names.

A response that declares its own header must be read by name. Reading it
positionally against the requested order shifts every value into the wrong
column — silently, since the output still looks like a full table.
"""

from __future__ import annotations

from lossrun.extract import _to_rows
from lossrun.jsonparse import TablePayload

REQUESTED = ["Claim Number", "Claimant Name", "Policy Number", "Loss State"]


def build(columns: list[str], rows: list[list[str]]) -> list[dict[str, str]]:
    payload = TablePayload(columns=columns, rows=rows)
    return [r.values for r in _to_rows(payload, REQUESTED, "m", "c1", "1-2")]


def test_declared_header_in_a_different_order_is_honoured():
    values = build(
        ["Policy Number", "Claim Number", "Claimant Name", "Loss State"],
        [["P-100", "C003", "McAllister", "CA"]],
    )[0]
    assert values["Claim Number"] == "C003"
    assert values["Policy Number"] == "P-100"
    assert values["Claimant Name"] == "McAllister"
    assert values["Loss State"] == "CA"


def test_partial_header_fills_the_rest_with_na():
    values = build(["Claim Number", "Claimant Name"], [["C003", "McAllister"]])[0]
    assert values["Claim Number"] == "C003"
    assert values["Policy Number"] == "N/A"
    assert set(values) == set(REQUESTED)


def test_unknown_declared_columns_are_dropped_without_shifting_the_rest():
    values = build(
        ["Row #", "Claim Number", "Internal Ref", "Claimant Name"],
        [["1", "C003", "xyz", "McAllister"]],
    )[0]
    assert values["Claim Number"] == "C003"
    assert values["Claimant Name"] == "McAllister"
    assert "Row #" not in values


def test_missing_header_falls_back_to_the_requested_order():
    values = build([], [["C003", "McAllister", "P-100", "CA"]])[0]
    assert values["Claim Number"] == "C003"
    assert values["Loss State"] == "CA"


def test_short_and_empty_cells_become_na():
    values = build(REQUESTED, [["C003", "", "P-100"]])[0]
    assert values["Claimant Name"] == "N/A"
    assert values["Loss State"] == "N/A"


def test_every_row_carries_every_requested_column():
    rows = build(["Claim Number"], [["C1"], ["C2"], ["C3"]])
    assert len(rows) == 3
    assert all(set(r) == set(REQUESTED) for r in rows)
