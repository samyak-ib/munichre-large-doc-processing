"""Third-opinion resolution of cells the two models disagree on.

`consensus.compare` keeps the primary model's value and reports the conflict.
That is safe but wasteful: the other model is right about a fair share of them,
and the document itself already holds the answer. Adjudication asks a model to
pick between the two candidates, given the source text around that claim.

It is a selection, never a rewrite — the adjudicator may answer only `A`, `B` or
`neither`, so a tie-break cannot introduce a value no model read. Calls carry no
attachment, which is what keeps them cheap enough to make one per conflict.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .jsonparse import first_json_object, strip_fences
from .merge import normalize_key
from .schema_loader import TableSchema

# Characters of document text handed to the adjudicator on each side of the row.
# Enough to carry the whole claim row and its neighbours; small enough that the
# call stays inside the text budget with room for the column spec.
SOURCE_WINDOW_CHARS = 900

INSTRUCTIONS = """You are settling a disagreement between two independent readings of an
insurance loss-run document. Two extractions of the same claim row returned
different values for one column. You are given both candidates and the text of
the document around that claim.

Decide which candidate the document supports.

- Answer `A` or `B` when the document supports that candidate.
- Answer `neither` when the document supports neither, or when the text given to
  you does not settle it. Guessing is worse than declining.
- You may not supply a value of your own. This is a choice between what two
  readers actually read, not a re-extraction.

Return one JSON object and nothing else:

{"choice": "A" | "B" | "neither", "reason": "<one short sentence>"}"""


@dataclass
class Adjudication:
    key: tuple[str, ...]
    column: str
    candidate_a: str
    candidate_b: str
    choice: str
    reason: str

    @property
    def resolved(self) -> bool:
        return self.choice in {"A", "B"}


def adjudicate(
    conflicts,
    *,
    client,
    model: str,
    schema: TableSchema,
    page_texts: list[str],
    max_workers: int = 4,
    log=print,
) -> list[Adjudication]:
    """Ask `model` to break each conflict, one text-only call apiece."""
    if not conflicts:
        return []

    specs = {c.name: c.prompt for c in schema.columns}
    source = "\n".join(page_texts)

    def one(index_and_conflict) -> Adjudication:
        index, conflict = index_and_conflict
        prompt = _prompt(conflict, specs.get(conflict.column, ""), source)
        result = client.run(
            model=model,
            prompt=prompt,
            instructions=INSTRUCTIONS,
            stage="adjudicate",
            label=f"{conflict.column} {index + 1}/{len(conflicts)}",
        )
        return _read(conflict, result)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        decisions = list(pool.map(one, enumerate(conflicts)))

    resolved = sum(1 for d in decisions if d.resolved)
    to_other = sum(1 for d in decisions if d.choice == "B")
    log(
        f"  adjudicated {resolved}/{len(decisions)} conflicts "
        f"({to_other} went to the second model)"
    )
    return decisions


def apply(rows: list[dict[str, str]], decisions: list[Adjudication]) -> int:
    """Write every `B` decision into the table in place. Returns how many landed.

    `A` and `neither` both leave the primary model's value alone, so only a
    decision that overturns it changes anything.
    """
    by_key = {normalize_key(row): row for row in rows}
    changed = 0
    for decision in decisions:
        if decision.choice != "B":
            continue
        row = by_key.get(decision.key)
        if row is None:
            continue
        row[decision.column] = decision.candidate_b
        changed += 1
    return changed


def _prompt(conflict, column_spec: str, source: str) -> str:
    window = _source_window(source, conflict.key)
    parts = [
        f"Column in dispute: {conflict.column}",
        f"Claim row: {' | '.join(k for k in conflict.key if k)}",
        "",
        f"Candidate A, read by {conflict.primary_model}:\n{conflict.primary_value}",
        f"Candidate B, read by {conflict.other_model}:\n{conflict.other_value}",
    ]
    if column_spec:
        parts += ["", "What this column means:", column_spec]
    if window:
        parts += ["", "Document text around this claim:", "---", window, "---"]
    else:
        parts += [
            "",
            "This claim could not be located in the document's text layer, so no "
            "source text is available. Answer `neither` unless one candidate is "
            "plainly malformed.",
        ]
    return "\n".join(parts)


def _source_window(source: str, key: tuple[str, ...]) -> str:
    """Document text around the first key part that appears in it."""
    for part in key:
        if not part or len(part) < 3:
            continue
        position = _find(source, part)
        if position < 0:
            continue
        start = max(0, position - SOURCE_WINDOW_CHARS)
        return source[start : position + SOURCE_WINDOW_CHARS]
    return ""


def _find(source: str, needle: str) -> int:
    """Locate a key in the text layer, tolerating the line wrapping a PDF adds."""
    position = source.casefold().find(needle.casefold())
    if position >= 0:
        return position
    # `001- WC19A-78355` in the text layer is `001-WC19A-78355` in the table.
    pattern = r"\s*".join(re.escape(ch) for ch in needle if not ch.isspace())
    match = re.search(pattern, source, re.IGNORECASE)
    return match.start() if match else -1


def _decision(text: str) -> dict:
    """The adjudicator's JSON, or an empty dict when it did not return any."""
    for candidate in (strip_fences(text), first_json_object(strip_fences(text))):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _read(conflict, result) -> Adjudication:
    choice, reason = "neither", ""
    if result.ok:
        payload = _decision(result.output_text)
        raw = str(payload.get("choice", "")).strip().upper()
        choice = raw if raw in {"A", "B"} else "neither"
        reason = str(payload.get("reason", ""))[:200]
    else:
        reason = f"adjudication call failed: {result.error_code or result.status}"
    return Adjudication(
        key=conflict.key,
        column=conflict.column,
        candidate_a=conflict.primary_value,
        candidate_b=conflict.other_value,
        choice=choice,
        reason=reason,
    )
