"""Third-opinion resolution of cells the two models read differently.

The adjudicator is a selector, not an extractor: the tests that matter are the
ones proving it cannot introduce a value neither model read, and that declining
to choose leaves the table exactly as consensus left it.
"""

from __future__ import annotations

from lossrun.adjudicate import (
    SOURCE_WINDOW_CHARS,
    Adjudication,
    _source_window,
    adjudicate,
)
from lossrun.adjudicate import apply as apply_adjudications
from lossrun.consensus import CellConflict
from lossrun.merge import normalize_key
from lossrun.schema_loader import load_schema
from lossrun.superapp_client import RunResult

SCHEMA = load_schema()
PAGE = (
    "LOSS RUN DETAIL\n"
    "AUPD 05/02/2022 CA10007718700 C00327611-01 JACKIE RODRIGUEZ 09/20/2022 CLOSED\n"
    "COLLISION WITH MOTOR VEHICLE $25.50\n"
)


def row(claimant="JACKIE RODRIGUEZ") -> dict[str, str]:
    return {
        "Policy Number": "CA10007718700",
        "Claim Number": "C00327611-01",
        "Claimant Name": claimant,
    }


def conflict(column="Claimant Name", primary="JACKIE RODRIGUEZ", other="TINA THOMAS"):
    # The key is whatever consensus.compare produces, so `apply` is exercised
    # against the real identity rather than a hand-written one.
    return CellConflict(
        key=normalize_key(row()),
        column=column,
        primary_value=primary,
        other_value=other,
        primary_model="openai/gpt-5.6-luna",
        other_model="gemini/gemini-3.6-flash",
    )


class FakeClient:
    """Records what it was asked and replies with a canned decision."""

    def __init__(self, *replies: str, ok: bool = True):
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.instructions: list[str] = []
        self.stages: list[str] = []
        self.attachments_seen = 0
        self.ok = ok

    def run(self, *, model, prompt, instructions="", attachments=None, stage="", label="", **_):
        self.prompts.append(prompt)
        self.instructions.append(instructions)
        self.stages.append(stage)
        if attachments:
            self.attachments_seen += len(attachments)
        body = self.replies.pop(0) if self.replies else '{"choice": "neither"}'
        return RunResult(
            response_id="r",
            status="completed" if self.ok else "failed",
            output_text=body,
            input_tokens=10,
            output_tokens=5,
            error_code="" if self.ok else "server_error",
        )


def run_one(reply: str, *, column="Claimant Name") -> Adjudication:
    client = FakeClient(reply)
    return adjudicate(
        [conflict(column=column)],
        client=client,
        model="openai/gpt-5.6-luna",
        schema=SCHEMA,
        page_texts=[PAGE],
        log=lambda *_: None,
    )[0]


def test_choosing_b_overturns_the_primary_value():
    decision = run_one('{"choice": "B", "reason": "the row reads TINA THOMAS"}')
    assert decision.choice == "B"
    assert decision.resolved

    rows = [row()]
    assert apply_adjudications(rows, [decision]) == 1
    assert rows[0]["Claimant Name"] == "TINA THOMAS"


def test_choosing_a_leaves_the_table_untouched():
    decision = run_one('{"choice": "A", "reason": "the document says JACKIE"}')
    rows = [row()]
    assert apply_adjudications(rows, [decision]) == 0
    assert rows[0]["Claimant Name"] == "JACKIE RODRIGUEZ"


def test_declining_keeps_the_primary_value():
    """`neither` is a real answer — it must not silently pick one anyway."""
    decision = run_one('{"choice": "neither", "reason": "the text does not settle it"}')
    assert not decision.resolved
    rows = [row()]
    assert apply_adjudications(rows, [decision]) == 0


def test_an_invented_value_is_ignored():
    """The contract is a choice. A model that answers with its own reading is
    treated as having declined, so adjudication can never add a third value."""
    decision = run_one('{"choice": "C", "value": "JACQUELINE RODRIGUEZ"}')
    assert decision.choice == "neither"
    assert decision.candidate_a == "JACKIE RODRIGUEZ"
    assert decision.candidate_b == "TINA THOMAS"


def test_unparseable_output_declines_rather_than_guessing():
    decision = run_one("I think the first one is probably right.")
    assert decision.choice == "neither"


def test_a_failed_call_declines_and_says_why():
    client = FakeClient("", ok=False)
    decision = adjudicate(
        [conflict()],
        client=client,
        model="m",
        schema=SCHEMA,
        page_texts=[PAGE],
        log=lambda *_: None,
    )[0]
    assert decision.choice == "neither"
    assert "failed" in decision.reason


def test_the_call_carries_both_candidates_the_spec_and_the_source_text():
    client = FakeClient('{"choice": "A"}')
    adjudicate(
        [conflict(column="Description")],
        client=client,
        model="m",
        schema=SCHEMA,
        page_texts=[PAGE],
        log=lambda *_: None,
    )
    prompt = client.prompts[0]
    assert "JACKIE RODRIGUEZ" in prompt and "TINA THOMAS" in prompt
    assert "openai/gpt-5.6-luna" in prompt and "gemini/gemini-3.6-flash" in prompt
    assert "COLLISION WITH MOTOR VEHICLE" in prompt, "source text travels with the call"
    assert "CLAIM DESCRIPTION EXTRACTION INSTRUCTIONS" in prompt, "column spec travels too"
    assert client.stages == ["adjudicate"]


def test_adjudication_sends_no_attachment():
    """What makes the call cheap is that the PDF does not go with it."""
    client = FakeClient('{"choice": "A"}')
    adjudicate(
        [conflict()],
        client=client,
        model="m",
        schema=SCHEMA,
        page_texts=[PAGE],
        log=lambda *_: None,
    )
    assert client.attachments_seen == 0


def test_a_claim_absent_from_the_text_layer_is_told_so():
    client = FakeClient('{"choice": "neither"}')
    adjudicate(
        [conflict()],
        client=client,
        model="m",
        schema=SCHEMA,
        page_texts=["nothing relevant here"],
        log=lambda *_: None,
    )
    assert "no source text is available" in client.prompts[0]


def test_no_conflicts_makes_no_calls():
    client = FakeClient()
    assert adjudicate([], client=client, model="m", schema=SCHEMA, page_texts=[]) == []
    assert client.prompts == []


def test_the_source_window_is_found_despite_pdf_line_wrapping():
    """A key the table holds as `001-WC19A-78355` can appear in the text layer
    split across a line break; the window has to survive that."""
    text = "header\n001-\nWC19A-78355 CLOSED $1,234\ntrailer"
    window = _source_window(text, ("001-WC19A-78355", "", ""))
    assert "CLOSED" in window


def test_the_source_window_is_bounded():
    text = "x" * 5000 + "C00327611-01" + "y" * 5000
    window = _source_window(text, ("", "C00327611-01", ""))
    assert len(window) <= 2 * SOURCE_WINDOW_CHARS + len("C00327611-01")
