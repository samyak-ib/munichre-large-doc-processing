"""Merge, normalization and the verbatim key check."""

from __future__ import annotations

from lossrun.extract import RawRow
from lossrun.merge import (
    fill_block_policy_numbers,
    is_empty,
    merge_rows,
    normalize_key,
    normalize_row,
    stamp_document_values,
)
from lossrun.schema_loader import load_schema
from lossrun.verify import CASE_MISMATCH, NOT_FOUND, verify_keys

SCHEMA = load_schema()


def row(**values: str) -> RawRow:
    return RawRow(values=values, model="m", chunk=values.pop("_chunk", "c1"), pages="1-2")


def test_key_ignores_case_and_whitespace_but_not_content():
    assert normalize_key({"Policy Number": "P-1", "Claim Number": "C003", "Claimant Name": "Acme"}) == (
        normalize_key({"Policy Number": " p-1 ", "Claim Number": "c003", "Claimant Name": "ACME"})
    )
    assert normalize_key({"Claim Number": "C003"}) != normalize_key({"Claim Number": "C0003"})


def test_overlapping_chunks_collapse_to_one_row():
    rows = [
        RawRow({"Policy Number": "P-1", "Claim Number": "C1", "Claimant Name": "A"}, "m", "c1", "1-50"),
        RawRow({"Policy Number": "P-1", "Claim Number": "C1", "Claimant Name": "A"}, "m", "c2", "49-98"),
    ]
    result = merge_rows(rows, SCHEMA)
    assert len(result.rows) == 1
    assert result.rows_merged == 1


def test_merge_fills_gaps_from_the_later_chunk():
    rows = [
        RawRow({"Claim Number": "C1", "Claimant Name": "A", "Loss State": "N/A"}, "m", "c1", "1-50"),
        RawRow({"Claim Number": "C1", "Claimant Name": "A", "Loss State": "CA"}, "m", "c2", "49-98"),
    ]
    result = merge_rows(rows, SCHEMA)
    assert result.rows[0]["Loss State"] == "CA"


def test_conflicting_values_are_kept_as_separate_rows():
    """Same claim number and claimant, but a real disagreement elsewhere (CA vs
    NY) means these are not treated as the same claim — they ship as two rows
    rather than silently collapsing to one."""
    rows = [
        RawRow({"Claim Number": "C1", "Claimant Name": "A", "Loss State": "CA"}, "m", "c1", "1-50"),
        RawRow({"Claim Number": "C1", "Claimant Name": "A", "Loss State": "NY"}, "m", "c2", "49-98"),
    ]
    result = merge_rows(rows, SCHEMA)
    assert len(result.rows) == 2
    assert [r["Loss State"] for r in result.rows] == ["CA", "NY"]


def test_distinct_claims_are_not_merged():
    rows = [
        RawRow({"Policy Number": "P", "Claim Number": "C1", "Claimant Name": "A"}, "m", "c1", "1"),
        RawRow({"Policy Number": "P", "Claim Number": "C2", "Claimant Name": "A"}, "m", "c1", "1"),
    ]
    assert len(merge_rows(rows, SCHEMA).rows) == 2


def test_same_key_but_different_claims_are_not_merged():
    """Two rows can share (Policy Number, Claim Number, Claimant Name) and
    still be distinct claims. A real disagreement elsewhere (different
    accident dates) must keep them separate rather than collapsing on the key."""
    rows = [
        RawRow(
            {"Policy Number": "P-1", "Claim Number": "C1", "Claimant Name": "A", "Accident Date": "01/01/2020"},
            "m", "c1", "1",
        ),
        RawRow(
            {"Policy Number": "P-1", "Claim Number": "C1", "Claimant Name": "A", "Accident Date": "02/02/2021"},
            "m", "c1", "1",
        ),
    ]
    assert len(merge_rows(rows, SCHEMA).rows) == 2


def test_blank_identity_columns_do_not_collapse_distinct_claims():
    """A document with no claim number and no claimant column, like
    `CAU Loss Runs 2016-2021.PDF` in docs/mistakes.md: every row normalizes to
    the same (mostly blank) key, but the rows are still distinct claims and
    must not collapse to one."""
    rows = [
        RawRow({"Policy Number": "P-1", "Accident Date": "01/01/2020", "Indemnity Paid": "92839"}, "m", "c1", "1"),
        RawRow({"Policy Number": "P-1", "Accident Date": "02/02/2020", "Indemnity Paid": "10906"}, "m", "c1", "1"),
        RawRow({"Policy Number": "P-1", "Accident Date": "03/03/2020", "Indemnity Paid": "500"}, "m", "c1", "1"),
    ]
    assert len(merge_rows(rows, SCHEMA).rows) == 3


def test_row_order_follows_first_appearance():
    rows = [
        RawRow({"Claim Number": c, "Claimant Name": "A"}, "m", "c1", "1") for c in ("C3", "C1", "C2")
    ]
    merged = merge_rows(rows, SCHEMA).rows
    assert [r["Claim Number"] for r in merged] == ["C3", "C1", "C2"]


def test_normalize_fills_every_schema_column():
    """Merge-time normalization fills the shape; formatting is cleaning's job."""
    normalized = normalize_row({"Claim Number": "C1", "Indemnity Paid": "$1,234.56"}, SCHEMA)
    assert set(normalized) == set(SCHEMA.names)
    assert normalized["Loss State"] == "N/A"
    assert normalized["Indemnity Paid"] == "$1,234.56", "left for cleaning to render"


def test_empty_markers_become_na():
    for marker in ("", "  ", "n/a", "None", "-"):
        assert is_empty(marker)
    assert normalize_row({"Loss State": "none"}, SCHEMA)["Loss State"] == "N/A"


def test_document_values_stamp_only_empty_cells():
    rows = [{"Insured": "N/A"}, {"Insured": "Already Set"}]
    stamp_document_values(rows, {"Insured": "Acme Corp", "Valuation Date": "01/01/2026"}, SCHEMA)
    assert rows[0]["Insured"] == "Acme Corp"
    assert rows[1]["Insured"] == "Already Set"
    assert rows[0]["Valuation Date"] == "01/01/2026"


def test_block_level_policy_numbers_fill_downwards():
    rows = [
        {"Policy Number": "P-1"},
        {"Policy Number": "N/A"},
        {"Policy Number": "N/A"},
        {"Policy Number": "P-2"},
        {"Policy Number": "N/A"},
    ]
    assert fill_block_policy_numbers(rows) == 3
    assert [r["Policy Number"] for r in rows] == ["P-1", "P-1", "P-1", "P-2", "P-2"]


def test_key_ignores_whitespace_a_line_wrap_introduced():
    # One model rejoins `001-\nWC19A-78355`, the other keeps the wrap as a space.
    # Treating those as different claims splits one row into two.
    assert normalize_key({"Policy Number": "001-WC19A-78355"}) == normalize_key(
        {"Policy Number": "001- WC19A-78355"}
    )


def test_wrapped_identifiers_merge_into_one_row():
    rows = [
        RawRow({"Policy Number": "001-WC19A-78355", "Claim Number": "C1", "Claimant Name": "A"}, "m1", "c1", "1"),
        RawRow({"Policy Number": "001- WC19A-78355", "Claim Number": "C1", "Claimant Name": "A"}, "m2", "c1", "1"),
    ]
    assert len(merge_rows(rows, SCHEMA).rows) == 1


def test_verification_covers_identifier_columns_beyond_the_row_key():
    issues, checked = verify_keys(
        [{"Claim Number": "C003", "Policy Number": "P-999", "Occurrence ID": "OCC-1"}],
        ["Claim C003 under policy P-999"],
    )
    assert checked == 3
    assert [(i.column, i.verdict) for i in issues] == [("Occurrence ID", NOT_FOUND)]


def test_the_carrier_is_not_verified_against_the_text_layer():
    """A carrier shown only in a letterhead logo is correct but unquotable.

    Golden data confirmed "FCCI" for a document whose text layer never spells
    it, so checking that column would flag a right answer as a hallucination.
    """
    issues, checked = verify_keys(
        [{"Claim Number": "C003", "Insurer Loss Run": "FCCI"}],
        ["Claim C003 for DREAMWORKS REMODELING LLC"],
    )
    assert checked == 1, "only the claim number is checkable here"
    assert issues == []


def test_verification_accepts_an_identifier_the_pdf_split_across_lines():
    issues, _ = verify_keys(
        [{"Policy Number": "001-WC20A-78355"}], ["Policy 001-\nWC20A-78355 term"]
    )
    assert issues == []


def test_key_verification_flags_a_hallucinated_claim_number():
    pages = ["Claim C003  McAllister, John   $1,200"]
    rows = [
        {"Claim Number": "C003", "Claimant Name": "McAllister, John"},
        {"Claim Number": "C0000003", "Claimant Name": "MacAllister, John"},
    ]
    issues, checked = verify_keys(rows, pages)
    assert checked == 4
    verdicts = {(i.column, i.value): i.verdict for i in issues}
    assert verdicts == {
        ("Claim Number", "C0000003"): NOT_FOUND,
        ("Claimant Name", "MacAllister, John"): NOT_FOUND,
    }


def test_key_verification_tolerates_a_line_break_inside_a_value():
    pages = ["Claimant: McAllister,\n   John"]
    issues, _ = verify_keys([{"Claimant Name": "McAllister, John"}], pages)
    assert issues == []


def test_case_only_differences_are_flagged_separately():
    issues, _ = verify_keys([{"Claim Number": "c003"}], ["Claim C003"])
    assert [i.verdict for i in issues] == [CASE_MISMATCH]


def test_no_text_layer_reports_zero_checked_rather_than_failures():
    issues, checked = verify_keys([{"Claim Number": "C1"}], ["", "  "])
    assert (issues, checked) == ([], 0)
