"""Normalizes an input file into the PDFs and text the pipeline can extract from.

Attachments are the claim source. An email body supplies document-level hints only
and never contributes rows unless the email carries no attachment at all.
"""

from __future__ import annotations

import email
import email.policy
from dataclasses import dataclass, field
from pathlib import Path

import fitz

PDF_SUFFIXES = {".pdf"}
TEXT_SUFFIXES = {".txt", ".csv", ".md"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff"}
SKIP_SUFFIXES = {".p7s", ".p7m", ".asc", ".ics", ".vcf"}


@dataclass
class SourceDoc:
    """One extractable artifact: a PDF on disk, or text pulled out of a file."""

    path: Path
    kind: str  # "pdf" | "text" | "image"
    origin: str  # how it arrived, for the audit trail
    pages: int = 0
    text: str = ""

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class IngestResult:
    docs: list[SourceDoc] = field(default_factory=list)
    context_text: str = ""
    notes: list[str] = field(default_factory=list)


def ingest(path: Path, work_dir: Path) -> IngestResult:
    """Turn an input file into a list of extractable documents."""
    if path.is_dir():
        result = IngestResult()
        for child in sorted(path.iterdir()):
            if child.is_file():
                child_result = ingest(child, work_dir)
                result.docs.extend(child_result.docs)
                result.notes.extend(child_result.notes)
        return result

    suffix = path.suffix.lower()
    if suffix in PDF_SUFFIXES:
        return IngestResult(docs=[_pdf_doc(path, origin="input")])
    if suffix == ".eml":
        return _ingest_eml(path, work_dir)
    if suffix == ".msg":
        return _ingest_msg(path, work_dir)
    if suffix in TEXT_SUFFIXES:
        return IngestResult(docs=[_text_doc(path, path.read_text(errors="replace"), "input")])
    if suffix == ".docx":
        return IngestResult(docs=[_text_doc(path, _docx_text(path), "input")])
    if suffix == ".xlsx":
        return IngestResult(docs=[_text_doc(path, _xlsx_text(path), "input")])
    if suffix in IMAGE_SUFFIXES:
        return IngestResult(docs=[SourceDoc(path=path, kind="image", origin="input")])

    raise ValueError(f"unsupported input type {suffix or path.name!r}")


def _pdf_doc(path: Path, *, origin: str) -> SourceDoc:
    with fitz.open(path) as doc:
        pages = doc.page_count
    return SourceDoc(path=path, kind="pdf", origin=origin, pages=pages)


def _text_doc(path: Path, text: str, origin: str) -> SourceDoc:
    return SourceDoc(path=path, kind="text", origin=origin, text=text)


def _ingest_eml(path: Path, work_dir: Path) -> IngestResult:
    message = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    body = message.get_body(preferencelist=("plain", "html"))
    context = body.get_content() if body is not None else ""

    out_dir = work_dir / path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    result = IngestResult(context_text=_email_context(message, context))
    for part in message.iter_attachments():
        filename = part.get_filename() or "attachment"
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        doc = _attachment_doc(out_dir, filename, payload, origin=f"eml:{path.name}")
        if doc is None:
            result.notes.append(f"skipped attachment {filename}")
            continue
        result.docs.append(doc)

    if not result.docs and context.strip():
        inline = out_dir / f"{path.stem}-body.txt"
        inline.write_text(context)
        result.docs.append(_text_doc(inline, context, origin=f"eml-body:{path.name}"))
        result.notes.append("no attachments; extracting from the email body")
    return result


def _ingest_msg(path: Path, work_dir: Path) -> IngestResult:
    try:
        import extract_msg
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise ValueError(
            ".msg input needs the extract-msg package (uv sync installs it)"
        ) from exc

    out_dir = work_dir / path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    with extract_msg.openMsg(str(path)) as message:
        context = "\n".join(
            filter(None, [f"Subject: {message.subject}", f"From: {message.sender}", message.body])
        )
        result = IngestResult(context_text=context)
        for attachment in message.attachments:
            filename = attachment.getFilename() or "attachment"
            payload = attachment.data
            if not isinstance(payload, bytes):
                result.notes.append(f"skipped non-binary attachment {filename}")
                continue
            doc = _attachment_doc(out_dir, filename, payload, origin=f"msg:{path.name}")
            if doc is None:
                result.notes.append(f"skipped attachment {filename}")
                continue
            result.docs.append(doc)

    if not result.docs and result.context_text.strip():
        inline = out_dir / f"{path.stem}-body.txt"
        inline.write_text(result.context_text)
        result.docs.append(_text_doc(inline, result.context_text, origin=f"msg-body:{path.name}"))
        result.notes.append("no attachments; extracting from the email body")
    return result


def _attachment_doc(
    out_dir: Path, filename: str, payload: bytes, *, origin: str
) -> SourceDoc | None:
    safe_name = Path(filename).name or "attachment"
    suffix = Path(safe_name).suffix.lower()
    if suffix in SKIP_SUFFIXES:
        return None

    target = out_dir / safe_name
    target.write_bytes(payload)

    if suffix in PDF_SUFFIXES:
        return _pdf_doc(target, origin=origin)
    if suffix in TEXT_SUFFIXES:
        return _text_doc(target, target.read_text(errors="replace"), origin)
    if suffix == ".docx":
        return _text_doc(target, _docx_text(target), origin)
    if suffix == ".xlsx":
        return _text_doc(target, _xlsx_text(target), origin)
    if suffix in IMAGE_SUFFIXES:
        return SourceDoc(path=target, kind="image", origin=origin)
    return None


def _email_context(message: email.message.Message, body: str) -> str:
    headers = [
        f"Subject: {message.get('Subject', '')}",
        f"From: {message.get('From', '')}",
        f"Date: {message.get('Date', '')}",
    ]
    return "\n".join(headers + ["", body.strip()])


def _docx_text(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    lines = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            lines.append("\t".join(cell.text for cell in row.cells))
    return "\n".join(lines)


def _xlsx_text(path: Path) -> str:
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    lines: list[str] = []
    for sheet in workbook.worksheets:
        lines.append(f"# {sheet.title}")
        for row in sheet.iter_rows(values_only=True):
            lines.append("\t".join("" if v is None else str(v) for v in row))
    workbook.close()
    return "\n".join(lines)
