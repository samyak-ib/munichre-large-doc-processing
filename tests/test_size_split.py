"""Splitting an oversized window must keep the overlap.

A page window that exceeds the request cap gets halved. If the halves meet
edge-to-edge, a claim row straddling the split appears whole in neither — the
same failure the page overlap exists to prevent.

Thresholds are derived from the fixture's real encoded size rather than
hardcoded, so PDF compression changes cannot silently stop exercising the split.
"""

from __future__ import annotations

import fitz
import pytest

from lossrun.chunking import build_chunks, slice_pdf
from lossrun.config import ChunkingConfig

PAGES = 20


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "sample.pdf"
    doc = fitz.open()
    for index in range(PAGES):
        doc.new_page().insert_text((40, 60), f"page {index + 1} " + "x9Q7z" * 4000, fontsize=6)
    doc.save(path)
    doc.close()
    return path


def encoded_size(path, start: int, end: int) -> int:
    return len(slice_pdf(path, start, end)) * 4 // 3


def cfg(max_bytes: int) -> ChunkingConfig:
    return ChunkingConfig(
        max_pages_per_chunk=PAGES,
        overlap_pages=2,
        header_pages=2,
        overlap_anchor_rows=5,
        max_request_bytes=max_bytes,
        max_resumes=1,
    )


def test_a_window_within_the_cap_is_not_split(pdf):
    generous = encoded_size(pdf, 1, PAGES) * 2
    assert len(build_chunks(pdf, PAGES, cfg(generous))) == 1


def test_a_window_over_the_cap_is_split(pdf):
    tight = encoded_size(pdf, 1, PAGES) // 2
    assert len(build_chunks(pdf, PAGES, cfg(tight))) > 1


def test_size_split_halves_still_overlap(pdf):
    tight = encoded_size(pdf, 1, PAGES) // 2
    chunks = build_chunks(pdf, PAGES, cfg(tight))
    assert len(chunks) > 1
    for previous, following in zip(chunks, chunks[1:]):
        assert following.start_page <= previous.end_page, (
            f"pages {previous.pages} and {following.pages} meet without overlap"
        )


def test_size_split_still_covers_every_page(pdf):
    tight = encoded_size(pdf, 1, PAGES) // 2
    covered: set[int] = set()
    for chunk in build_chunks(pdf, PAGES, cfg(tight)):
        covered.update(range(chunk.start_page, chunk.end_page + 1))
    assert covered == set(range(1, PAGES + 1))


def test_split_terminates_when_one_page_alone_exceeds_the_cap(pdf):
    """The degenerate case must terminate rather than recurse forever.

    A single page cannot be split further, so it is emitted oversized and the
    client reports the limit if it is truly too big. Every split point keeps its
    own overlap, so pages repeat across branches here — wasteful at this extreme,
    but it only arises when one page alone busts the request cap.
    """
    impossible = encoded_size(pdf, 1, 1) // 2
    chunks = build_chunks(pdf, PAGES, cfg(impossible))

    assert all(c.start_page == c.end_page for c in chunks)
    covered = {c.start_page for c in chunks}
    assert covered == set(range(1, PAGES + 1))


def test_chunk_indices_are_renumbered_after_a_split(pdf):
    tight = encoded_size(pdf, 1, PAGES) // 2
    chunks = build_chunks(pdf, PAGES, cfg(tight))
    assert [c.index for c in chunks] == list(range(1, len(chunks) + 1))
    assert all(c.total == len(chunks) for c in chunks)
