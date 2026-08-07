"""The QA reviewer's guard rails.

The reviewer, unlike the adjudicator it replaces, can propose a value of its own.
What makes that safe is `apply`: a correction reaches the table only when the
document's text layer carries the value it proposes. The tests that matter are
therefore the ones about what does NOT get written.
"""

from __future__ import annotations

import json

import pytest

from lossrun import qa
from lossrun.chunking import Chunk
from lossrun.extract import RawRow
from lossrun.qa import APPLIED, NO_CHANGE, UNMATCHED, UNVERIFIED, QAFinding
from lossrun.schema_loader import load_schema
from lossrun.superapp_client import RunResult

SCHEMA = load_schema()

PAGE_TEXT = (
    "ACME LOGISTICS INC - LOSS RUN\n"
    "P-100 | C003 | McAllister, John | $1,200.00 | TX\n"
    "P-100 | C004 | Okonkwo, Ada | $2,450.75 | CA\n"
)


def row(claim="C003", claimant="McAllister, John", **extra):
    values = {
        "Policy Number": "P-100",
        "Claim Number": claim,
        "Claimant Name": claimant,
        "Indemnity Paid": "1200",
        "Loss State": "TX",
    }
    values.update(extra)
    return values


def finding(column="Claimant Name", proposed="McAllister, John", claimant="MacAllister, John"):
    return QAFinding(
        key=qa.normalize_key(row(claimant=claimant)),
        column=column,
        current_value="",
        proposed_value=proposed,
        reason="reads that way on the page",
    )


class FakeClient:
    """Records what each review call carried, and replays a canned answer."""

    def __init__(self, payload=None, *, ok=True):
        self.payload = payload if payload is not None else {"findings": [], "missing_rows": []}
        self.ok = ok
        self.calls: list[dict] = []

    def run(self, *, model, prompt, instructions="", attachments=None, stage="", label="",
            pages="", effort=None):
        self.calls.append(
            {
                "stage": stage,
                "prompt": prompt,
                "pages": pages,
                "attachments": list(attachments or []),
                "effort": effort,
            }
        )
        return RunResult(
            response_id="r",
            status="completed" if self.ok else "failed",
            output_text=json.dumps(self.payload) if self.ok else "",
            input_tokens=10,
            output_tokens=5,
        )


def chunk(index=1, total=1, start=1, end=2) -> Chunk:
    return Chunk(index=index, total=total, start_page=start, end_page=end, data=b"%PDF-fake")


# --- what reaches the table ---------------------------------------------------


def test_a_correction_the_text_layer_carries_is_written():
    rows = [row(claimant="MacAllister, John")]
    decisions = [finding()]
    assert qa.apply(rows, decisions, [PAGE_TEXT], SCHEMA) == 1
    assert rows[0]["Claimant Name"] == "McAllister, John"
    assert decisions[0].verdict == APPLIED
    assert decisions[0].current_value == "MacAllister, John", "the replaced value is recorded"


def test_a_correction_the_document_never_states_is_refused():
    rows = [row(claimant="MacAllister, John")]
    decisions = [finding(proposed="Nobody, Invented")]
    assert qa.apply(rows, decisions, [PAGE_TEXT], SCHEMA) == 0
    assert rows[0]["Claimant Name"] == "MacAllister, John"
    assert decisions[0].verdict == UNVERIFIED


def test_a_document_with_no_text_layer_can_confirm_nothing():
    """The cost of the guard, stated plainly: a scan gets a review it cannot act on."""
    rows = [row(claimant="MacAllister, John")]
    decisions = [finding()]
    assert qa.apply(rows, decisions, [], SCHEMA) == 0
    assert decisions[0].verdict == UNVERIFIED


def test_clearing_a_cell_needs_no_proof():
    """`N/A` takes a value away rather than adding one, so it cannot invent."""
    rows = [row()]
    decisions = [finding(column="Indemnity Paid", proposed="N/A", claimant="McAllister, John")]
    assert qa.apply(rows, decisions, [PAGE_TEXT], SCHEMA) == 1
    assert rows[0]["Indemnity Paid"] == "N/A"
    assert decisions[0].verdict == APPLIED


def test_a_correction_naming_an_unknown_row_changes_nothing():
    rows = [row()]
    decisions = [finding(claimant="Someone Else Entirely")]
    assert qa.apply(rows, decisions, [PAGE_TEXT], SCHEMA) == 0
    assert decisions[0].verdict == UNMATCHED


def test_a_correction_naming_a_column_outside_the_schema_changes_nothing():
    rows = [row()]
    decisions = [finding(column="Adjuster Notes", claimant="McAllister, John")]
    assert qa.apply(rows, decisions, [PAGE_TEXT], SCHEMA) == 0
    assert decisions[0].verdict == UNMATCHED
    assert "Adjuster Notes" not in rows[0]


def test_a_formatting_only_correction_is_not_a_change():
    """The table holds `1200`; the page prints `$1,200.00`. Both are the same cell."""
    rows = [row()]
    decisions = [
        finding(column="Indemnity Paid", proposed="$1,200.00", claimant="McAllister, John")
    ]
    assert qa.apply(rows, decisions, [PAGE_TEXT], SCHEMA) == 0
    assert rows[0]["Indemnity Paid"] == "1200"
    assert decisions[0].verdict == NO_CHANGE


def test_an_applied_value_is_rendered_the_way_the_table_stores_it():
    rows = [row(**{"Indemnity Paid": "9999"})]
    decisions = [
        finding(column="Indemnity Paid", proposed="$2,450.75", claimant="McAllister, John")
    ]
    assert qa.apply(rows, decisions, [PAGE_TEXT], SCHEMA) == 1
    assert rows[0]["Indemnity Paid"] == "2450.75", "cleaned, not pasted in verbatim"


def test_case_alone_does_not_make_a_value_unverifiable():
    rows = [row(**{"Loss State": "XX"})]
    decisions = [finding(column="Loss State", proposed="tx", claimant="McAllister, John")]
    assert qa.apply(rows, decisions, [PAGE_TEXT], SCHEMA) == 1
    assert rows[0]["Loss State"] == "tx"


# --- what the reviewer is asked ----------------------------------------------


def review(client, rows, row_pages, chunks, **kwargs):
    return qa.review(
        rows,
        row_pages,
        client=client,
        model="m",
        schema=SCHEMA,
        chunks=chunks,
        log=lambda *_: None,
        **kwargs,
    )


def test_an_empty_table_costs_no_calls():
    client = FakeClient()
    assert review(client, [], [], [chunk()]).findings == []
    assert client.calls == []


def test_the_review_re_attaches_the_pages_the_rows_came_from():
    client = FakeClient()
    result = review(client, [row()], ["1-2"], [chunk()])

    assert result.calls == 1
    assert client.calls[0]["stage"] == "qa"
    assert client.calls[0]["pages"] == "1-2"
    assert len(client.calls[0]["attachments"]) == 1, "the reviewer must see the page"
    assert client.calls[0]["attachments"][0].data == b"%PDF-fake"


def test_each_row_is_reviewed_against_its_own_chunk():
    client = FakeClient()
    chunks = [chunk(1, 2, 1, 2), chunk(2, 2, 3, 4)]
    rows = [row(claim="C003"), row(claim="C004")]

    review(client, rows, ["1-2", "3-4"], chunks)

    assert len(client.calls) == 2
    first = next(c for c in client.calls if c["pages"] == "1-2")
    second = next(c for c in client.calls if c["pages"] == "3-4")
    assert "C003" in first["prompt"] and "C004" not in first["prompt"]
    assert "C004" in second["prompt"] and "C003" not in second["prompt"]


def test_a_chunk_with_no_rows_is_not_reviewed():
    client = FakeClient()
    chunks = [chunk(1, 2, 1, 2), chunk(2, 2, 3, 4)]
    review(client, [row()], ["1-2"], chunks)
    assert [c["pages"] for c in client.calls] == ["1-2"]


def test_rows_too_large_for_one_request_are_split_across_calls(monkeypatch):
    """A 50-page chunk can hold more rows than the text budget allows.

    Silently reviewing the first N would report the rest as clean when nobody
    looked at them, so the chunk is split instead.
    """
    monkeypatch.setattr(qa, "MAX_TEXT_BYTES", 0)
    client = FakeClient()
    rows = [row(claim=f"C{i:03d}") for i in range(4)]

    review(client, rows, ["1-2"] * 4, [chunk()])

    assert len(client.calls) == 4
    reviewed = " ".join(c["prompt"] for c in client.calls)
    assert all(f"C{i:03d}" in reviewed for i in range(4)), "every row is still looked at"


def test_a_failed_review_call_is_reported_rather_than_read_as_clean():
    client = FakeClient(ok=False)
    result = review(client, [row()], ["1-2"], [chunk()])
    assert result.findings == []
    assert result.errors, "a chunk nobody reviewed must not pass for a clean one"


def test_an_unparseable_answer_is_reported():
    client = FakeClient()
    client.payload = None
    client.run = lambda **kw: RunResult(
        response_id="r", status="completed", output_text="I had a look and it seems fine.",
        input_tokens=1, output_tokens=1,
    )
    result = review(client, [row()], ["1-2"], [chunk()])
    assert result.errors


# --- reading the reviewer's answer -------------------------------------------


def test_findings_without_a_row_or_column_are_dropped():
    client = FakeClient(
        {
            "findings": [
                {"row": [], "column": "Loss State", "correct_value": "TX"},
                {"row": ["P-100", "C003", "McAllister, John"], "column": "", "correct_value": "TX"},
                {"row": ["P-100", "C003", "McAllister, John"], "column": "Loss State",
                 "correct_value": "TX", "reason": "column header says Loss State"},
            ],
            "missing_rows": [],
        }
    )
    result = review(client, [row()], ["1-2"], [chunk()])
    assert len(result.findings) == 1
    assert result.findings[0].column == "Loss State"
    assert result.findings[0].pages == "1-2"


def test_a_missing_row_the_table_already_holds_is_not_reported():
    """The reviewer re-listing a row it was given is not a recall failure."""
    client = FakeClient(
        {
            "findings": [],
            "missing_rows": [
                {"row": ["P-100", "C003", "McAllister, John"], "reason": "on page 1"},
                {"row": ["P-100", "C009", "Never Extracted"], "reason": "on page 2"},
            ],
        }
    )
    result = review(client, [row()], ["1-2"], [chunk()])
    assert [k[1] for k in (m.key for m in result.missing)] == ["c009"]


# --- lining the table up with the chunks it was read from ---------------------


def raw(claim, pages, policy="P-100"):
    return RawRow(
        values={"Policy Number": policy, "Claim Number": claim, "Claimant Name": "A"},
        model="m",
        chunk=f"chunk {pages}",
        pages=pages,
    )


def test_a_row_is_attributed_to_the_chunk_that_first_read_it():
    """Chunks overlap, so the same claim arrives more than once."""
    pages = qa.row_chunk_pages([raw("C1", "1-2"), raw("C2", "1-2"), raw("C2", "2-3"), raw("C3", "2-3")])
    assert pages == ["1-2", "1-2", "2-3"]


def test_the_attribution_is_positional_so_a_repaired_key_still_lines_up():
    """`fill_block_policy_numbers` rewrites Policy Number after the merge.

    Looking the chunk up by key afterwards would miss exactly the rows that were
    repaired, so the mapping travels by position instead.
    """
    raws = [raw("C1", "1-2", policy=""), raw("C2", "2-3")]
    pages = qa.row_chunk_pages(raws)
    assert len(pages) == 2
    assert pages[0] == "1-2"


@pytest.mark.parametrize("value", [None, [], "C003", 42])
def test_a_malformed_row_key_is_ignored(value):
    assert qa._key(value) is None
