"""Document profiling and layout discovery — the metadata pass.

Profiling is local and free: page count, text-layer presence, size. It decides
whether the document goes through in a single shot or needs chunking. Layout
discovery is one LLM call over the opening pages, and its output travels with
every later chunk because continuation pages carry no table header.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import fitz

from .chunking import slice_pdf
from .config import ChunkingConfig
from .jsonparse import first_json_object, strip_fences
from .prompts import layout_instructions, layout_prompt
from .schema_loader import TableSchema
from .superapp_client import Attachment, SuperAppClient

# Rough bytes-per-token for the local estimate. Only used to report scale.
CHARS_PER_TOKEN = 4


@dataclass
class DocProfile:
    pages: int
    byte_size: int
    text_pages: int
    est_tokens: int
    route: str  # "single_shot" | "chunked"

    @property
    def has_text_layer(self) -> bool:
        return self.text_pages > 0

    @property
    def text_coverage(self) -> float:
        return self.text_pages / self.pages if self.pages else 0.0


@dataclass
class Layout:
    data: dict = field(default_factory=dict)

    @property
    def document_values(self) -> dict[str, str]:
        values = self.data.get("document_values")
        return values if isinstance(values, dict) else {}

    @property
    def policy_number_placement(self) -> str:
        return str(self.data.get("policy_number_placement", "column"))

    @property
    def reported_row_count(self) -> int | None:
        """The row count layout discovery stated, or None if it didn't.

        The model is told to return null rather than guess when it was only
        shown the opening pages of a longer document, so this is commonly
        absent — a QA-time comparison against it should treat None as "no
        expectation to check against," not as zero rows.
        """
        value = self.data.get("reported_row_count")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value) if value >= 0 else None


def profile_pdf(path: Path, cfg: ChunkingConfig) -> DocProfile:
    """Page count, text-layer coverage, and the single-shot/chunked decision."""
    with fitz.open(path) as doc:
        pages = doc.page_count
        text_pages = 0
        chars = 0
        for page in doc:
            text = page.get_text().strip()
            if text:
                text_pages += 1
                chars += len(text)

    byte_size = path.stat().st_size
    fits_pages = pages <= cfg.max_pages_per_chunk
    fits_bytes = byte_size * 4 // 3 <= cfg.max_request_bytes
    return DocProfile(
        pages=pages,
        byte_size=byte_size,
        text_pages=text_pages,
        est_tokens=chars // CHARS_PER_TOKEN,
        route="single_shot" if (fits_pages and fits_bytes) else "chunked",
    )


def page_texts(path: Path) -> list[str]:
    """Per-page text, used by the verbatim key check. Empty for scanned pages."""
    with fitz.open(path) as doc:
        return [page.get_text() for page in doc]


def discover_layout(
    client: SuperAppClient,
    *,
    model: str,
    path: Path,
    schema: TableSchema,
    profile: DocProfile,
    cfg: ChunkingConfig,
    context_text: str = "",
    effort: str | None = None,
) -> Layout:
    """One call over the opening pages, mapping the document's table structure."""
    header_pages = min(cfg.header_pages, profile.pages)
    data = slice_pdf(path, 1, header_pages)

    result = client.run(
        model=model,
        instructions=layout_instructions(schema),
        prompt=layout_prompt(
            header_pages,
            profile.pages,
            context_text,
            document_label=schema.document_label,
            row_label=schema.row_label,
        ),
        attachments=[Attachment(filename=f"{path.stem}-header.pdf", data=data)],
        stage="layout",
        label="layout discovery",
        effort=effort,
        pages=f"1-{header_pages}",
    )
    if not result.ok or not result.output_text.strip():
        # Layout is an optimization, not a hard dependency: extraction still has
        # the full column definitions and can read the header itself.
        return Layout(data={"notes": "layout discovery unavailable", "present_columns": {}})

    return Layout(data=_parse_layout(result.output_text))


def _parse_layout(text: str) -> dict:
    candidate = strip_fences(text)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        obj = first_json_object(candidate)
        if obj is None:
            return {"notes": "layout response was not JSON"}
        try:
            parsed = json.loads(obj)
        except json.JSONDecodeError:
            return {"notes": "layout response was not JSON"}
    return parsed if isinstance(parsed, dict) else {"notes": "layout response was not an object"}
