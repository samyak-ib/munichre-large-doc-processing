"""The `extract` batch loop: parallel documents, error isolation, exit code.

The API is stubbed out entirely — `run_document` itself is replaced, since
these tests are about the batch loop in `_cmd_extract`, not the pipeline it
drives (that's `test_pipeline.py`).
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from lossrun import cli
from lossrun.config import ApiConfig, ChunkingConfig, Config, ReasoningConfig
from lossrun.pipeline import RunOutcome


def make_config(concurrency: int) -> Config:
    return Config(
        api=ApiConfig(
            base_url="https://example.invalid/api/v1",
            poll_interval_s=0,
            poll_deadline_s=10,
            request_timeout_s=10,
            max_concurrent_calls=1,
            max_retries=0,
            max_concurrent_documents=concurrency,
        ),
        primary_model="model-a",
        chunking=ChunkingConfig(
            max_pages_per_chunk=2,
            overlap_pages=1,
            header_pages=1,
            overlap_anchor_rows=5,
            max_request_bytes=20 * 1024 * 1024,
            max_resumes=2,
        ),
        model_overrides={},
        reasoning=ReasoningConfig(),
        pricing={},
        token="test-token",
    )


def make_args(tmp_path, paths, *, concurrency=None) -> argparse.Namespace:
    return argparse.Namespace(
        inputs=paths,
        config=None,
        schema=None,
        out=tmp_path / "out",
        model=None,
        qa_model=None,
        base_url=None,
        golden=None,
        effort=None,
        provider=None,
        results_out=None,  # publishing is exercised in test_pipeline.py/test_report.py
        qa=None,
        concurrency=concurrency,
    )


def fake_outcome(path: Path) -> RunOutcome:
    return RunOutcome(
        run_dir=Path("/fake") / path.stem,
        workbook=Path("/fake") / path.stem / "workbook.xlsx",
        ledger=Path("/fake/ledger.xlsx"),
        rows=1,
        qa_findings=0,
        qa_applied=0,
        unverified=0,
        cost_usd=0.01,
        route="direct",
        chunks=1,
    )


def test_documents_run_concurrently_not_sequentially(tmp_path, monkeypatch):
    """3 documents, each taking ~0.2s, with concurrency 3 must overlap — total
    wall time should look like one document's worth of work, not three's."""
    paths = [tmp_path / f"doc{i}.pdf" for i in range(3)]
    for path in paths:
        path.write_bytes(b"")

    def slow_run_document(path, **_kwargs):
        time.sleep(0.2)
        return fake_outcome(path)

    monkeypatch.setattr(cli, "run_document", slow_run_document)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: make_config(3))

    started = time.monotonic()
    exit_code = cli._cmd_extract(make_args(tmp_path, paths))
    elapsed = time.monotonic() - started

    assert exit_code == 0
    assert elapsed < 0.5, f"took {elapsed:.2f}s — documents do not appear to have run concurrently"


def test_one_failing_document_does_not_stop_the_others(tmp_path, monkeypatch):
    paths = [tmp_path / f"doc{i}.pdf" for i in range(3)]
    for path in paths:
        path.write_bytes(b"")

    processed: list[str] = []

    def run_document_maybe_failing(path, **_kwargs):
        if path.stem == "doc1":
            raise RuntimeError("boom")
        processed.append(path.stem)
        return fake_outcome(path)

    monkeypatch.setattr(cli, "run_document", run_document_maybe_failing)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: make_config(3))

    exit_code = cli._cmd_extract(make_args(tmp_path, paths))

    assert exit_code == 1, "one failed document must still fail the batch"
    assert sorted(processed) == ["doc0", "doc2"], "the other two must still complete"


def test_concurrency_flag_overrides_the_config_default(tmp_path, monkeypatch):
    paths = [tmp_path / "doc0.pdf"]
    paths[0].write_bytes(b"")
    seen_concurrency = []

    real_executor = cli.ThreadPoolExecutor

    def spying_executor(max_workers, *a, **k):
        seen_concurrency.append(max_workers)
        return real_executor(max_workers, *a, **k)

    monkeypatch.setattr(cli, "run_document", lambda path, **_kwargs: fake_outcome(path))
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: make_config(3))
    monkeypatch.setattr(cli, "ThreadPoolExecutor", spying_executor)

    cli._cmd_extract(make_args(tmp_path, paths, concurrency=7))

    assert seen_concurrency == [7]
