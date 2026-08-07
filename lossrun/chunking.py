"""Page-window chunking for documents that exceed a model's page ceiling.

Windows overlap so a claim row split across a boundary appears whole in at least
one chunk. A window whose encoded payload would exceed the request cap is halved
recursively rather than failing the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fitz

from .config import ChunkingConfig


@dataclass(frozen=True)
class Chunk:
    index: int
    total: int
    start_page: int  # 1-based, inclusive
    end_page: int  # 1-based, inclusive
    data: bytes

    @property
    def label(self) -> str:
        return f"chunk {self.index}/{self.total}"

    @property
    def pages(self) -> str:
        return f"{self.start_page}-{self.end_page}"


def page_windows(total_pages: int, max_pages: int, overlap: int) -> list[tuple[int, int]]:
    """Overlapping 1-based inclusive page windows covering the whole document.

    A 120-page document at 50 pages with 2 of overlap yields
    (1, 50), (49, 98), (97, 120).
    """
    if total_pages <= 0:
        return []
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    if total_pages <= max_pages:
        return [(1, total_pages)]
    # An overlap at or above the window size would never advance.
    step_overlap = max(0, min(overlap, max_pages - 1))

    windows: list[tuple[int, int]] = []
    start = 1
    while start <= total_pages:
        end = min(start + max_pages - 1, total_pages)
        windows.append((start, end))
        if end >= total_pages:
            break
        start = end - step_overlap + 1
    return windows


def slice_pdf(path: Path, start_page: int, end_page: int) -> bytes:
    """Extract a 1-based inclusive page range as a standalone PDF."""
    with fitz.open(path) as src:
        out = fitz.open()
        try:
            out.insert_pdf(src, from_page=start_page - 1, to_page=end_page - 1)
            return out.tobytes(garbage=3, deflate=True)
        finally:
            out.close()


def build_chunks(path: Path, total_pages: int, cfg: ChunkingConfig) -> list[Chunk]:
    """Slice a PDF into request-sized chunks, halving any window that is too large."""
    windows = page_windows(total_pages, cfg.max_pages_per_chunk, cfg.overlap_pages)
    sized: list[tuple[int, int, bytes]] = []
    for start, end in windows:
        sized.extend(
            _fit_window(path, start, end, cfg.max_request_bytes, cfg.overlap_pages)
        )

    total = len(sized)
    return [
        Chunk(index=i + 1, total=total, start_page=s, end_page=e, data=data)
        for i, (s, e, data) in enumerate(sized)
    ]


def _fit_window(
    path: Path, start: int, end: int, max_bytes: int, overlap: int
) -> list[tuple[int, int, bytes]]:
    """Split a window until each half fits the request cap, keeping the overlap.

    A size split must overlap for the same reason a page window does: a claim row
    straddling the split point has to appear whole in one of the two halves.
    """
    data = slice_pdf(path, start, end)
    # base64 inflates by 4/3; compare the encoded size against the cap.
    if len(data) * 4 // 3 <= max_bytes or start == end:
        return [(start, end, data)]
    mid = start + (end - start) // 2
    # start + 1 at minimum, so the second half is always strictly smaller than
    # the window being split and the recursion terminates.
    second_start = max(mid + 1 - overlap, start + 1)
    return _fit_window(path, start, mid, max_bytes, overlap) + _fit_window(
        path, second_start, end, max_bytes, overlap
    )
