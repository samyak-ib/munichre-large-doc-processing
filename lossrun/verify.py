"""Verbatim key verification against the PDF text layer.

The primary failure mode is the model "correcting" a claim number or claimant
name, which breaks the key used to match against golden data. Checking that each
key appears literally in the page text catches that for free — no API call, no
OCR, no table detector.

Scanned pages have no text layer, so the check reports "unverifiable" rather than
"missing"; a document with no text at all is skipped entirely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .merge import is_empty

VERIFIED = "verified"
CASE_MISMATCH = "case_mismatch"
NOT_FOUND = "not_found"
UNVERIFIABLE = "unverifiable"

# Columns whose value must appear in the document exactly as printed. Restricted
# to identifiers and proper names: a column the schema asks the model to
# interpret (Loss State to a state name, Claim Status to Open/Closed, dates to a
# common format) would fail this check for doing its job correctly.
VERIFIED_COLUMNS = (
    "Claim Number",
    "Claimant Name",
    "Policy Number",
    "Occurrence ID",
)

# Insurer Loss Run is deliberately absent: the carrier is often shown only in a
# letterhead logo, so a correct answer legitimately has no text-layer match.
# Golden data confirmed "FCCI" for a document whose text layer never spells it.
_WS_RE = re.compile(r"\s+")


@dataclass
class KeyIssue:
    row_index: int
    column: str
    value: str
    verdict: str


def verify_keys(
    rows: list[dict[str, str]], page_texts: list[str]
) -> tuple[list[KeyIssue], int]:
    """Check every key value against the document text.

    Returns the issues found and how many values were checked. An empty document
    text yields no issues and a zero count, so callers can tell "clean" apart
    from "could not check".
    """
    haystack = _normalize(" \n ".join(page_texts))
    if not haystack.strip():
        return [], 0

    lowered = haystack.casefold()
    issues: list[KeyIssue] = []
    checked = 0

    for index, row in enumerate(rows):
        for column in VERIFIED_COLUMNS:
            value = row.get(column, "")
            if is_empty(value):
                continue
            checked += 1
            needle = _normalize(value)
            if needle in haystack:
                continue
            verdict = CASE_MISMATCH if needle.casefold() in lowered else NOT_FOUND
            issues.append(
                KeyIssue(row_index=index, column=column, value=value, verdict=verdict)
            )
    return issues, checked


def _normalize(text: str) -> str:
    """Remove whitespace so line wrapping cannot fake a mismatch.

    An identifier broken across lines reaches the text layer as `001-\\nWC20A`,
    and a model that correctly rejoins it would otherwise be flagged for the one
    thing it got right. Whitespace is dropped rather than collapsed for the same
    reason it is in the merge key.
    """
    return _WS_RE.sub("", text)
