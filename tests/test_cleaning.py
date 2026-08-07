"""Rendering the extracted table into the golden set's representation.

Golden stores dates and amounts canonically, so those are rewritten. Everything
else it records as-is — `CLOSED` and `C` both appear — so canonicalizing a
categorical value would turn a matching row into a mismatch.
"""

from __future__ import annotations

from datetime import datetime

from lossrun.cleaning import NA, clean_row, clean_table, clean_value
from lossrun.schema_loader import load_schema

SCHEMA = load_schema()


def test_dates_are_rendered_the_way_golden_stores_them():
    for written in ("Jan 5, 2023", "January 5, 2023", "2023-01-05", "01/05/2023", "5 Jan 2023"):
        assert clean_value("Accident Date", written) == "01/05/2023", written


def test_a_timestamp_loses_its_time_but_a_month_name_keeps_its_spaces():
    assert clean_value("Accident Date", "2023-01-05 00:00:00") == "01/05/2023"
    assert clean_value("Accident Date", "Jan 5, 2023") == "01/05/2023"


def test_a_datetime_object_is_accepted_directly():
    assert clean_value("Accident Date", datetime(2023, 1, 5)) == "01/05/2023"


def test_an_unparseable_date_is_kept_verbatim():
    """Dropping it would hide a real extraction from the score."""
    assert clean_value("Accident Date", "sometime in March") == "sometime in March"


def test_amounts_lose_their_currency_decoration():
    assert clean_value("Indemnity Paid", "$1,234.56") == "1234.56"
    assert clean_value("Indemnity Paid", "1,234.560") == "1234.56"
    assert clean_value("Indemnity Paid", "$0.00") == "0"
    assert clean_value("Claim Total", "19289.17") == "19289.17"


def test_parenthesised_amounts_are_negative():
    assert clean_value("Expense Paid", "(500.00)") == "-500"
    assert clean_value("Expense Paid", "(1,234.56)") == "-1234.56"


def test_unparseable_money_is_kept_verbatim():
    assert clean_value("Claim Total", "see note") == "see note"


def test_empty_markers_all_become_na():
    for marker in ("", "  ", "n/a", "None", "-", "null", "NaN"):
        assert clean_value("Loss State", marker) == NA


def test_categorical_values_are_left_exactly_as_extracted():
    # Golden records these as-is: CLOSED, C, Closed and "C (Closed)" all occur.
    for status in ("CLOSED", "C", "Closed", "C (Closed)"):
        assert clean_value("Claim Status", status) == status
    for state in ("TX", "Texas"):
        assert clean_value("Loss State", state) == state


def test_whitespace_is_collapsed_everywhere():
    assert clean_value("Claimant Name", "  McAllister,\n  John ") == "McAllister, John"


def test_cleaning_is_idempotent():
    once = clean_value("Indemnity Paid", "$1,234.56")
    assert clean_value("Indemnity Paid", once) == once
    assert clean_value("Accident Date", clean_value("Accident Date", "Jan 5, 2023")) == "01/05/2023"


def test_a_cleaned_row_carries_every_schema_column():
    cleaned = clean_row({"Claim Number": "C003"}, SCHEMA)
    assert set(cleaned) == set(SCHEMA.names)
    assert cleaned["Claim Number"] == "C003"
    assert cleaned["Loss State"] == NA


def test_clean_table_maps_every_row():
    rows = [{"Claim Number": "C1", "Indemnity Paid": "$5.00"}, {"Claim Number": "C2"}]
    cleaned = clean_table(rows, SCHEMA)
    assert [r["Claim Number"] for r in cleaned] == ["C1", "C2"]
    assert cleaned[0]["Indemnity Paid"] == "5"
