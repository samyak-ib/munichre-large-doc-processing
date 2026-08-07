"""Per-chunk extraction with seam anchors and truncation resume.

All columns come back from one call per chunk, which is what keeps rows aligned —
extracting column groups separately is what makes row counts diverge.

Chunks run sequentially within a model because each one is handed the previous
chunk's trailing row keys as seam anchors. Models run concurrently instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .chunking import Chunk, build_chunks
from .config import ChunkingConfig
from .docprofile import DocProfile, Layout
from .jsonparse import PayloadError, TablePayload, parse_table_payload
from .prompts import extraction_instructions, extraction_prompt, text_extraction_prompt
from .schema_loader import KEY_COLUMNS, TableSchema
from .superapp_client import MAX_TEXT_BYTES, Attachment, SuperAppClient, SuperAppError

# Slack left for the prompt's own framing around a clipped text source.
TEXT_PROMPT_HEADROOM = 2048


@dataclass
class RawRow:
    """One extracted row plus where it came from."""

    values: dict[str, str]
    model: str
    chunk: str
    pages: str


@dataclass
class ExtractionEvent:
    level: str  # "info" | "warning" | "error"
    stage: str
    detail: str
    chunk: str = ""
    pages: str = ""


@dataclass
class ExtractResult:
    rows: list[RawRow] = field(default_factory=list)
    events: list[ExtractionEvent] = field(default_factory=list)
    chunks: int = 0

    def warn(self, stage: str, detail: str, chunk: str = "", pages: str = "") -> None:
        self.events.append(
            ExtractionEvent(level="warning", stage=stage, detail=detail, chunk=chunk, pages=pages)
        )


def _save_raw_response(debug_dir: Path | None, model: str, label: str, text: str) -> None:
    """Keep the raw response of a chunk that produced no rows.

    A chunk that parses to nothing is undiagnosable without the text the model
    actually sent, and re-running one costs a full chunk's tokens.
    """
    if debug_dir is None:
        return
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{model}-{label}").strip("_")
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / f"raw-{safe}.txt").write_text(text)


def extract_pdf(
    client: SuperAppClient,
    *,
    model: str,
    path: Path,
    schema: TableSchema,
    profile: DocProfile,
    layout: Layout,
    cfg: ChunkingConfig,
    debug_dir: Path | None = None,
    effort: str | None = None,
) -> ExtractResult:
    """Extract every claim row from a PDF, chunking when the profile calls for it."""
    result = ExtractResult()
    instructions = extraction_instructions(schema, layout.data)
    row_columns = [c.name for c in schema.row_columns]

    if profile.route == "single_shot":
        chunks = [
            Chunk(
                index=1,
                total=1,
                start_page=1,
                end_page=profile.pages,
                data=path.read_bytes(),
            )
        ]
    else:
        chunks = build_chunks(path, profile.pages, cfg)
    result.chunks = len(chunks)

    anchors: list[list[str]] = []
    for chunk in chunks:
        rows = _extract_chunk(
            client,
            model=model,
            chunk=chunk,
            total_pages=profile.pages,
            instructions=instructions,
            row_columns=row_columns,
            anchors=anchors,
            cfg=cfg,
            result=result,
            debug_dir=debug_dir,
            effort=effort,
        )
        result.rows.extend(rows)
        anchors = _trailing_anchors(rows, cfg.overlap_anchor_rows)

    return result


def extract_text(
    client: SuperAppClient,
    *,
    model: str,
    text: str,
    label: str,
    schema: TableSchema,
    layout: Layout,
    effort: str | None = None,
) -> ExtractResult:
    """Extract from a text source (email body, spreadsheet) in a single call."""
    result = ExtractResult(chunks=1)
    row_columns = [c.name for c in schema.row_columns]
    instructions = extraction_instructions(schema, layout.data)

    prompt = text_extraction_prompt(text, label)
    budget = MAX_TEXT_BYTES - len(instructions.encode()) - TEXT_PROMPT_HEADROOM
    if len(prompt.encode()) > budget:
        kept = _clip_to_bytes(text, max(budget, 0))
        result.warn(
            "extract",
            f"text source is larger than the {MAX_TEXT_BYTES // 1024} KiB request "
            f"budget; extracted the first {len(kept)} of {len(text)} characters",
            label,
            "text",
        )
        prompt = text_extraction_prompt(kept, label)

    try:
        run = client.run(
            model=model,
            instructions=instructions,
            prompt=prompt,
            stage="extract",
            label=label,
            pages="text",
            effort=effort,
        )
    except SuperAppError as exc:
        result.events.append(
            ExtractionEvent(level="error", stage="extract", detail=str(exc), chunk=label)
        )
        return result

    payload = _payload_or_none(run.output_text, result, label, "text")
    if payload is None:
        return result
    result.rows.extend(_to_rows(payload, row_columns, model, label, "text"))
    return result


def _extract_chunk(
    client: SuperAppClient,
    *,
    model: str,
    chunk: Chunk,
    total_pages: int,
    instructions: str,
    row_columns: list[str],
    anchors: list[list[str]],
    cfg: ChunkingConfig,
    result: ExtractResult,
    debug_dir: Path | None = None,
    effort: str | None = None,
) -> list[RawRow]:
    """Run one chunk to completion, resuming through fresh calls on truncation."""
    rows: list[RawRow] = []
    resume_after: list[str] | None = None

    for attempt in range(cfg.max_resumes + 1):
        prompt = extraction_prompt(
            chunk_index=chunk.index,
            chunk_total=chunk.total,
            start_page=chunk.start_page,
            end_page=chunk.end_page,
            total_pages=total_pages,
            anchors=anchors,
            resume_after=resume_after,
        )
        label = chunk.label if attempt == 0 else f"{chunk.label} resume {attempt}"
        try:
            run = client.run(
                model=model,
                instructions=instructions,
                prompt=prompt,
                attachments=[
                    Attachment(
                        filename=f"pages-{chunk.start_page}-{chunk.end_page}.pdf",
                        data=chunk.data,
                    )
                ],
                stage="extract",
                label=label,
                pages=chunk.pages,
                effort=effort,
            )
        except SuperAppError as exc:
            result.events.append(
                ExtractionEvent(
                    level="error",
                    stage="extract",
                    detail=str(exc),
                    chunk=label,
                    pages=chunk.pages,
                )
            )
            return rows

        if not run.output_text.strip():
            # A completed run with empty output is a silent agent route, not a
            # document with no claims. Retry rather than losing the chunk: it
            # cost a full upload and returning nothing here loses every row.
            if attempt < cfg.max_resumes:
                result.warn(
                    "extract",
                    "response carried no output; retrying the chunk",
                    label,
                    chunk.pages,
                )
                continue
            result.warn(
                "extract",
                f"response carried no output after {cfg.max_resumes + 1} attempts",
                label,
                chunk.pages,
            )
            return rows

        payload = _payload_or_none(run.output_text, result, label, chunk.pages)
        if payload is None:
            _save_raw_response(debug_dir, model, label, run.output_text)
            return rows

        new_rows = _to_rows(payload, row_columns, model, label, chunk.pages)
        rows.extend(new_rows)

        if payload.repaired:
            result.warn(
                "extract",
                f"output was cut off mid-JSON; salvaged {len(new_rows)} complete rows",
                label,
                chunk.pages,
            )
        if not payload.truncated:
            return rows
        if not new_rows:
            _save_raw_response(debug_dir, model, label, run.output_text)
            result.warn(
                "extract",
                f"truncated with no complete rows ({run.output_tokens:,} output tokens "
                f"produced nothing parseable); raw response saved",
                label,
                chunk.pages,
            )
            return rows

        resume_after = payload.last_row_key or _row_key_values(new_rows[-1])
        if attempt == cfg.max_resumes:
            result.warn(
                "extract",
                f"still truncated after {cfg.max_resumes} resumes; rows may be missing",
                label,
                chunk.pages,
            )

    return rows


def _payload_or_none(
    text: str, result: ExtractResult, label: str, pages: str
) -> TablePayload | None:
    try:
        return parse_table_payload(text)
    except PayloadError as exc:
        result.events.append(
            ExtractionEvent(
                level="error",
                stage="parse",
                detail=f"unparseable response: {exc}",
                chunk=label,
                pages=pages,
            )
        )
        return None


def _to_rows(
    payload: TablePayload, row_columns: list[str], model: str, chunk: str, pages: str
) -> list[RawRow]:
    """Map positional row arrays onto column names.

    The model's declared `columns` list drives the mapping whenever it names any
    column we asked for, so a reordered or partial response still lands in the
    right cells. Only a response that declares no usable header falls back to
    positional mapping against the requested order.
    """
    expected = set(row_columns)
    declared = [c for c in payload.columns if c in expected]
    columns = payload.columns if declared else row_columns

    rows: list[RawRow] = []
    for values in payload.rows:
        mapped = {name: "N/A" for name in row_columns}
        for index, name in enumerate(columns):
            if name not in expected or index >= len(values):
                continue
            value = values[index].strip() if values[index] else ""
            mapped[name] = value or "N/A"
        rows.append(RawRow(values=mapped, model=model, chunk=chunk, pages=pages))
    return rows


def _clip_to_bytes(text: str, budget: int) -> str:
    """Trim text to a UTF-8 byte budget without splitting a character."""
    encoded = text.encode()
    if len(encoded) <= budget:
        return text
    return encoded[:budget].decode(errors="ignore")


def _row_key_values(row: RawRow) -> list[str]:
    return [row.values.get(name, "N/A") for name in KEY_COLUMNS]


def _trailing_anchors(rows: list[RawRow], limit: int) -> list[list[str]]:
    return [_row_key_values(r) for r in rows[-limit:]] if rows else []
