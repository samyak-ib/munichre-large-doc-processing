"""A reviewing pass over the extracted table, in place of a second extraction.

Consensus asked a second model to read the whole document again and treated
disagreement as the signal. That costs a full extraction pass to produce a set of
cells nobody has adjudicated yet, and it only ever sees what two readers happen
to differ on. QA asks one model to audit the table that already exists, with the
same pages in front of it: one call per chunk, and every cell is looked at rather
than only the contested ones.

The safety property that made adjudication trustworthy is kept, by a different
route. Adjudication could not invent a value because it could only choose between
two candidates. QA *can* propose a value, so instead the proposal is checked
against the document's own text layer before it is written: a correction lands
only when the value it proposes actually occurs in the document. Anything QA
cannot prove is reported and the extracted value stands.

The one carve-out is clearing a cell. A proposal of `N/A` removes a value rather
than introducing one, so it cannot be a hallucination and needs no proof — which
matters, because an invented figure is the failure this stage exists to catch.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .chunking import Chunk
from .cleaning import NA, clean_value
from .jsonparse import first_json_object, strip_fences
from .merge import is_empty, normalize_key
from .prompts import qa_instructions, qa_prompt
from .schema_loader import KEY_COLUMNS, TableSchema
from .superapp_client import MAX_TEXT_BYTES, Attachment, SuperAppError
from .verify import appears_verbatim, text_haystack

# What became of each proposed correction.
APPLIED = "applied"
UNVERIFIED = "unverified"  # not found in the text layer, so not written
UNMATCHED = "unmatched"  # names a row the final table does not hold
NO_CHANGE = "no_change"  # the table already reads that way

# Slack left for the prompt's own framing around the serialized rows.
PROMPT_HEADROOM = 2048


@dataclass
class QAFinding:
    key: tuple[str, ...]
    column: str
    current_value: str
    proposed_value: str
    reason: str
    pages: str = ""
    verdict: str = ""


@dataclass
class MissingRow:
    key: tuple[str, ...]
    reason: str
    pages: str = ""


@dataclass
class QAResult:
    findings: list[QAFinding] = field(default_factory=list)
    missing: list[MissingRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    calls: int = 0
    # No text layer means no correction can be proved, so none is applied. The
    # run has to say so rather than reporting a clean review.
    verifiable: bool = True

    @property
    def applied(self) -> int:
        return sum(1 for f in self.findings if f.verdict == APPLIED)


def review(
    rows: list[dict[str, str]],
    row_pages: list[str],
    *,
    client,
    model: str,
    schema: TableSchema,
    chunks: list[Chunk],
    effort: str | None = None,
    max_workers: int = 1,
    log=print,
) -> QAResult:
    """Audit each chunk's rows against that chunk's pages, one call apiece.

    `row_pages` gives the chunk each final row came from, positionally. Chunks
    carry no ordering dependency on one another the way extraction's seam anchors
    do, so they are only serialized as far as `max_workers` says — which is 1 by
    default, because two PDF uploads at once is what makes the endpoint time out.
    """
    result = QAResult()
    if not rows or not chunks:
        return result

    instructions = qa_instructions(schema)
    columns = [c.name for c in schema.row_columns]
    budget = MAX_TEXT_BYTES - len(instructions.encode()) - PROMPT_HEADROOM
    batches = _batches(rows, row_pages, chunks, columns, budget)
    if not batches:
        return result

    split = len(batches) - len({b.chunk.index for b in batches})
    if split:
        log(f"  [qa] {split} chunk(s) reviewed in parts to stay inside the text budget")

    def one(batch: _Batch) -> None:
        prompt = qa_prompt(
            chunk_index=batch.chunk.index,
            chunk_total=batch.chunk.total,
            start_page=batch.chunk.start_page,
            end_page=batch.chunk.end_page,
            total_pages=batch.total_pages,
            columns=columns,
            rows=[[row.get(c, "") for c in columns] for row in batch.rows],
            batch_index=batch.index,
            batch_total=batch.total,
        )
        label = batch.chunk.label if batch.total == 1 else f"{batch.chunk.label} part {batch.index}"
        try:
            run = client.run(
                model=model,
                instructions=instructions,
                prompt=prompt,
                attachments=[
                    Attachment(
                        filename=f"pages-{batch.chunk.start_page}-{batch.chunk.end_page}.pdf",
                        data=batch.chunk.data,
                    )
                ],
                stage="qa",
                label=label,
                pages=batch.chunk.pages,
                effort=effort,
            )
        except SuperAppError as exc:
            result.errors.append(f"{label}: {exc}")
            return
        if not run.ok:
            result.errors.append(f"{label}: {run.error_code or run.status}")
            return
        _collect(run.output_text, batch, result)

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        list(pool.map(one, batches))
    result.calls = len(batches)
    return result


def apply(
    rows: list[dict[str, str]],
    findings: list[QAFinding],
    page_texts: list[str],
    schema: TableSchema,
) -> int:
    """Write the corrections the document supports, in place. Returns how many.

    Every finding leaves with a verdict, including the ones that changed nothing:
    a correction the text layer would not confirm is the cell a human should read,
    so it has to survive into the Issues sheet rather than being dropped here.
    """
    haystack = text_haystack(page_texts)
    by_key = {normalize_key(row): row for row in rows}
    names = set(schema.names)
    applied = 0

    for finding in findings:
        if finding.column not in names:
            finding.verdict = UNMATCHED
            continue
        row = by_key.get(finding.key)
        if row is None:
            finding.verdict = UNMATCHED
            continue

        current = row.get(finding.column, "")
        if is_empty(finding.proposed_value):
            # Clearing a cell removes a value rather than introducing one, so it
            # cannot be a hallucination and needs no text-layer proof.
            value = NA
        elif appears_verbatim(finding.proposed_value, haystack):
            value = clean_value(finding.column, finding.proposed_value)
        else:
            finding.current_value = current
            finding.verdict = UNVERIFIED
            continue

        finding.current_value = current
        if value == current:
            finding.verdict = NO_CHANGE
            continue
        row[finding.column] = value
        finding.verdict = APPLIED
        applied += 1

    return applied


def row_chunk_pages(raw_rows) -> list[str]:
    """The chunk each merged row came from, in merge order.

    Merging keeps the first row seen per key and preserves that order, so walking
    the raw rows the same way lines this list up with the final table one for one.
    Positional rather than keyed, because filling a block-level policy number
    rewrites a key column after the merge — a lookup by key would miss exactly
    the rows that were repaired.
    """
    seen: set[tuple[str, ...]] = set()
    pages: list[str] = []
    for raw in raw_rows:
        key = normalize_key(raw.values)
        if key in seen:
            continue
        seen.add(key)
        pages.append(raw.pages)
    return pages


@dataclass
class _Batch:
    chunk: Chunk
    rows: list[dict[str, str]]
    total_pages: int
    index: int = 1
    total: int = 1


def _batches(
    rows: list[dict[str, str]],
    row_pages: list[str],
    chunks: list[Chunk],
    columns: list[str],
    budget: int,
) -> list[_Batch]:
    """One batch per chunk, split further when its rows outgrow the text budget."""
    by_pages = {chunk.pages: chunk for chunk in chunks}
    total_pages = max(chunk.end_page for chunk in chunks)

    grouped: dict[int, list[dict[str, str]]] = {}
    for index, row in enumerate(rows):
        pages = row_pages[index] if index < len(row_pages) else ""
        # A row whose provenance is missing still has to be reviewed; the first
        # chunk is the only defensible default and is right for a single-chunk
        # document, which is the case this can actually arise in.
        chunk = by_pages.get(pages, chunks[0])
        grouped.setdefault(chunk.index, []).append(row)

    batches: list[_Batch] = []
    for chunk in chunks:
        chunk_rows = grouped.get(chunk.index, [])
        if not chunk_rows:
            continue
        parts = _split_to_budget(chunk_rows, columns, budget)
        batches.extend(
            _Batch(
                chunk=chunk,
                rows=part,
                total_pages=total_pages,
                index=i + 1,
                total=len(parts),
            )
            for i, part in enumerate(parts)
        )
    return batches


def _split_to_budget(
    rows: list[dict[str, str]], columns: list[str], budget: int
) -> list[list[dict[str, str]]]:
    """Cut a chunk's rows into groups whose serialized form fits one request."""
    parts: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    size = 0
    for row in rows:
        encoded = len(json.dumps([row.get(c, "") for c in columns]).encode()) + 1
        if current and size + encoded > budget:
            parts.append(current)
            current, size = [], 0
        current.append(row)
        size += encoded
    if current:
        parts.append(current)
    return parts or [[]]


def _collect(text: str, batch: _Batch, result: QAResult) -> None:
    payload = _payload(text)
    if payload is None:
        result.errors.append(f"{batch.chunk.label}: response was not usable JSON")
        return

    known = {normalize_key(row): row for row in batch.rows}
    for raw in payload.get("findings") or []:
        finding = _finding(raw, batch.chunk.pages)
        if finding is not None:
            result.findings.append(finding)
    for raw in payload.get("missing_rows") or []:
        key = _key(raw.get("row"))
        # A key the batch already holds is the model re-reporting a row it was
        # given, not one that was missed.
        if key and key not in known:
            result.missing.append(
                MissingRow(key=key, reason=str(raw.get("reason", ""))[:200], pages=batch.chunk.pages)
            )


def _finding(raw: object, pages: str) -> QAFinding | None:
    if not isinstance(raw, dict):
        return None
    key = _key(raw.get("row"))
    column = str(raw.get("column", "")).strip()
    if not key or not column:
        return None
    return QAFinding(
        key=key,
        column=column,
        current_value="",
        proposed_value=str(raw.get("correct_value", "")).strip(),
        reason=str(raw.get("reason", ""))[:200],
        pages=pages,
    )


def _key(value: object) -> tuple[str, ...] | None:
    """Turn the model's `[policy, claim, claimant]` into a merge key."""
    if not isinstance(value, list) or not value:
        return None
    parts = [str(v) if v is not None else "" for v in value]
    return normalize_key(dict(zip(KEY_COLUMNS, parts)))


def _payload(text: str) -> dict | None:
    for candidate in (strip_fences(text), first_json_object(strip_fences(text))):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
