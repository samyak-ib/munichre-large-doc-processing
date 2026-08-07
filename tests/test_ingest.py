"""Ingest: attachments are the claim source, the body is context."""

from __future__ import annotations

import email.message

import fitz
import pytest

from lossrun.ingest import ingest


def one_page_pdf() -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((50, 60), "Claim C003 McAllister $1,200")
    data = doc.tobytes()
    doc.close()
    return data


def build_eml(tmp_path, *, attachments: list[tuple[str, str, str, bytes]], body: str = "body text"):
    message = email.message.EmailMessage()
    message["Subject"] = "Loss run for ACME"
    message["From"] = "broker@example.com"
    message["Date"] = "Tue, 4 Aug 2026 08:31:00 -0700"
    message.set_content(body)
    for filename, maintype, subtype, data in attachments:
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    path = tmp_path / "message.eml"
    path.write_bytes(message.as_bytes())
    return path


def test_pdf_attachment_becomes_the_source_document(tmp_path):
    eml = build_eml(tmp_path, attachments=[("loss_run.pdf", "application", "pdf", one_page_pdf())])
    result = ingest(eml, tmp_path / "work")

    assert [d.name for d in result.docs] == ["loss_run.pdf"]
    assert result.docs[0].kind == "pdf"
    assert result.docs[0].pages == 1
    assert result.docs[0].origin == "eml:message.eml"


def test_email_headers_and_body_are_kept_as_context(tmp_path):
    eml = build_eml(
        tmp_path,
        attachments=[("loss_run.pdf", "application", "pdf", one_page_pdf())],
        body="Attached is the loss run. Valuation date 01/15/2026.",
    )
    context = ingest(eml, tmp_path / "work").context_text
    assert "Subject: Loss run for ACME" in context
    assert "Valuation date 01/15/2026" in context


def test_signature_attachments_are_skipped_with_a_note(tmp_path):
    eml = build_eml(
        tmp_path,
        attachments=[
            ("loss_run.pdf", "application", "pdf", one_page_pdf()),
            ("smime.p7s", "application", "pkcs7-signature", b"sig"),
        ],
    )
    result = ingest(eml, tmp_path / "work")
    assert [d.name for d in result.docs] == ["loss_run.pdf"]
    assert any("smime.p7s" in note for note in result.notes)


def test_email_without_attachments_falls_back_to_the_body(tmp_path):
    eml = build_eml(tmp_path, attachments=[], body="P-1 | C003 | McAllister | 1200")
    result = ingest(eml, tmp_path / "work")

    assert len(result.docs) == 1
    assert result.docs[0].kind == "text"
    assert "C003" in result.docs[0].text
    assert any("no attachments" in note for note in result.notes)


def test_csv_attachment_is_read_as_text(tmp_path):
    eml = build_eml(
        tmp_path,
        attachments=[("claims.csv", "text", "csv", b"policy,claim\nP-1,C003\n")],
    )
    doc = ingest(eml, tmp_path / "work").docs[0]
    assert doc.kind == "text"
    assert "C003" in doc.text


def test_plain_pdf_input_needs_no_unwrapping(tmp_path):
    pdf = tmp_path / "direct.pdf"
    pdf.write_bytes(one_page_pdf())
    result = ingest(pdf, tmp_path / "work")
    assert result.docs[0].kind == "pdf"
    assert result.docs[0].origin == "input"


def test_a_directory_yields_every_file_in_it(tmp_path):
    source = tmp_path / "batch"
    source.mkdir()
    for name in ("a.pdf", "b.pdf"):
        (source / name).write_bytes(one_page_pdf())
    result = ingest(source, tmp_path / "work")
    assert sorted(d.name for d in result.docs) == ["a.pdf", "b.pdf"]


def test_unsupported_input_is_rejected_clearly(tmp_path):
    bad = tmp_path / "archive.zip"
    bad.write_bytes(b"PK\x03\x04")
    with pytest.raises(ValueError, match="unsupported input type"):
        ingest(bad, tmp_path / "work")
