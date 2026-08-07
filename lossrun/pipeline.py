"""Wires the stages together: ingest, profile, extract, merge, verify, compare."""

from __future__ import annotations

import concurrent.futures
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .accuracy import AccuracyResult, load_golden, score
from .adjudicate import Adjudication, adjudicate
from .adjudicate import apply as apply_adjudications
from .cleaning import clean_table
from .config import Config
from .consensus import ConsensusResult, compare
from .docprofile import DocProfile, Layout, discover_layout, page_texts, profile_pdf
from .extract import ExtractResult, RawRow, extract_pdf, extract_text
from .ingest import SourceDoc, ingest
from .merge import finalize_rows
from .report import append_ledger, write_workbook
from .schema_loader import TableSchema, load_schema
from .router import ModelRouter
from .telemetry import Telemetry
from .verify import NOT_FOUND, verify_keys

RAW_ROW_META = ("model", "chunk", "pages")

DEFAULT_GOLDEN_PATH = Path("goldens/Loss Runs GTs.xlsx")

# Loss-run filenames arrive with spaces, GUIDs and punctuation; keep them
# recognisable but path-safe, and short enough to stay clickable.
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_RUN_DIR_STEM = 80


@dataclass
class ModelRun:
    """One model's independent pass over the document."""

    model: str
    rows: list[dict[str, str]] = field(default_factory=list)
    raw: list[RawRow] = field(default_factory=list)
    result: ExtractResult | None = None
    profile: DocProfile | None = None
    layout: Layout | None = None
    error: str = ""


@dataclass
class RunOutcome:
    run_dir: Path
    workbook: Path
    ledger: Path
    rows: int
    conflicts: int
    unverified: int
    cost_usd: float
    agreement: float | None
    route: str
    chunks: int
    accuracy: AccuracyResult | None = None


def run_document(
    input_path: Path,
    *,
    config: Config,
    out_dir: Path,
    schema_path: Path | None = None,
    single_model: bool = False,
    golden_path: Path | None = None,
    batch_id: str = "",
    log=print,
) -> RunOutcome:
    """Extract one input file end to end and write its workbook."""
    schema = load_schema(schema_path)
    run_id = uuid.uuid4().hex[:12]
    telemetry = Telemetry(
        run_id=run_id, document=input_path.name, pricing=config.pricing, batch_id=batch_id
    )

    models = [config.primary_model] if single_model else list(config.consensus_models)
    # De-duplicate while preserving order: a config may list the same model twice.
    models = list(dict.fromkeys(models))

    # Each run gets its own directory so re-running a document never overwrites
    # the previous result — comparing two model configurations is the point. Runs
    # are grouped by route because the routes are not measuring the same thing:
    # SuperApp totals carry agent-loop tokens a direct call never pays for, so
    # mixing them in one folder invites a comparison that is not valid.
    run_dir = (
        out_dir
        / f"{config.route_class(models)}_calls"
        / f"{_safe_stem(input_path)}_{time.strftime('%Y%m%d-%H%M%S')}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)

    work_dir = run_dir / "source"
    ingested = ingest(input_path, work_dir)
    if not ingested.docs:
        raise ValueError(f"nothing extractable found in {input_path.name}")
    for note in ingested.notes:
        log(f"  note: {note}")

    source = _primary_source(ingested.docs)
    log(f"  source: {source.name} ({source.kind}, {source.pages or '—'} pages)")

    with ModelRouter(config=config, telemetry=telemetry) as client:
        runs = _run_models(
            client,
            models=models,
            source=source,
            schema=schema,
            config=config,
            context_text=ingested.context_text,
            debug_dir=run_dir / "raw",
            log=log,
        )

    primary = runs[0]
    if primary.error:
        raise RuntimeError(f"primary model {primary.model} failed: {primary.error}")

    issues: list[dict[str, Any]] = []

    # The primary model can come back empty — a silent agent route, or a chunk
    # that never parsed. Shipping an empty table while a second model holds a
    # full one throws away work already paid for, so the table falls back.
    if not primary.rows:
        replacement = next((r for r in runs[1:] if not r.error and r.rows), None)
        if replacement is not None:
            log(
                f"  primary {primary.model} returned no rows; "
                f"falling back to {replacement.model} ({len(replacement.rows)} rows)"
            )
            issues.append(
                {
                    "severity": "warning",
                    "category": "primary_model_empty",
                    "detail": (
                        f"{primary.model} produced no rows; the final table comes "
                        f"from {replacement.model}"
                    ),
                    "source": primary.model,
                }
            )
            runs = [replacement] + [r for r in runs if r is not replacement]
            primary = replacement
    _collect_extraction_issues(runs, issues)

    consensus: ConsensusResult | None = None
    others = [r for r in runs[1:] if not r.error and r.rows]
    if others:
        other = others[0]
        consensus = compare(
            primary.rows,
            other.rows,
            schema=schema,
            primary_model=primary.model,
            other_model=other.model,
        )
        _collect_consensus_issues(consensus, issues)
        log(
            f"  consensus: {consensus.compared_rows} shared rows, "
            f"{len(consensus.conflicts)} cell conflicts, "
            f"{consensus.overall_agreement:.1f}% agreement"
        )
        if config.adjudicate and consensus.conflicts:
            with ModelRouter(config=config, telemetry=telemetry) as judge:
                decisions = adjudicate(
                    consensus.conflicts,
                    client=judge,
                    model=config.adjudicator_model,
                    schema=schema,
                    page_texts=page_texts(source.path) if source.kind == "pdf" else [],
                    max_workers=config.api.max_concurrent_calls,
                    log=log,
                )
            overturned = apply_adjudications(primary.rows, decisions)
            # The adjudicated value comes from the other model's table, which was
            # cleaned before the diff — re-clean so a replacement cannot slip a
            # different date or money format into the final table.
            primary.rows = clean_table(primary.rows, schema)
            consensus.rows = primary.rows
            _collect_adjudication_issues(decisions, issues)
            log(f"  adjudication: {overturned} cells taken from {other.model}")

    golden = load_golden(golden_path or DEFAULT_GOLDEN_PATH, input_path.name)
    accuracy_rows: list[dict[str, Any]] = []
    column_rows: list[dict[str, Any]] = []
    mismatch_rows: list[dict[str, Any]] = []
    primary_accuracy: AccuracyResult | None = None
    if golden:
        # Page count travels with every accuracy row so a score can be read
        # against document size — the scanned 38-page file and the 2-page one
        # are not comparable without it.
        pages = primary.profile.pages if primary.profile else 0
        context = {
            "batch_id": batch_id,
            "run_id": run_id,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(telemetry.started_at)),
            "document": input_path.name,
            "pages": pages,
        }
        for run in runs:
            if run.error or not run.rows:
                continue
            scored = score(run.rows, golden, schema, run.model)
            if run.model == primary.model:
                primary_accuracy = scored
            accuracy_rows.append(
                scored.summary_row(**context, effort=config.extract_effort_for(run.model) or "default")
            )
            column_rows.extend(
                {
                    "batch_id": batch_id,
                    "run_id": run_id,
                    "document": input_path.name,
                    "pages": pages,
                    "model": run.model,
                    "column": name,
                    "compared": c.compared,
                    "correct": c.correct,
                    "accuracy_pct": round(c.accuracy, 1),
                }
                for name, c in scored.columns.items()
            )
            mismatch_rows.extend(scored.mismatches)
            log(
                f"  [{run.model}] accuracy vs golden: "
                f"{scored.rows_matched}/{scored.rows_golden} rows matched, "
                f"{scored.cell_accuracy:.1f}% cells correct, "
                f"{scored.exact_row_rate:.1f}% rows fully correct"
            )
    else:
        log("  accuracy: no golden entry for this document")

    unverified = 0
    if source.kind == "pdf":
        key_issues, checked = verify_keys(primary.rows, page_texts(source.path))
        unverified = sum(1 for i in key_issues if i.verdict == NOT_FOUND)
        if checked:
            log(
                f"  key check: {checked} key values checked against the text layer, "
                f"{unverified} not found verbatim"
            )
            _collect_key_issues(key_issues, primary.rows, issues)
        else:
            log("  key check: skipped, no text layer (scanned document)")
            issues.append(
                {
                    "severity": "warning",
                    "category": "verification",
                    "detail": "no text layer; keys could not be verified verbatim",
                    "source": source.name,
                }
            )

    workbook_path = run_dir / "extraction.xlsx"
    # One ledger per route, beside that route's runs. It accumulates across runs
    # so model and configuration experiments are comparable, while keeping runs
    # measured a different way out of the file entirely: a SuperApp total carries
    # agent-loop tokens a direct call never pays for, so the two do not belong in
    # one population. Comparing the routes means comparing two ledgers.
    ledger_path = run_dir.parent / "telemetry.xlsx"

    summary = telemetry.summary(
        models=", ".join(models),
        providers=", ".join(
            f"{m.rsplit('/', 1)[-1]}->{config.provider_for(m)}" for m in models
        ),
        route=primary.profile.route if primary.profile else "text",
        effort=", ".join(
            f"{m.rsplit('/', 1)[-1]}={config.extract_effort_for(m) or 'default'}" for m in models
        ),
        pages=primary.profile.pages if primary.profile else 0,
        chunks=primary.result.chunks if primary.result else 0,
        rows=len(primary.rows),
        conflicts=len(consensus.conflicts) if consensus else 0,
        unverified_keys=unverified,
        agreement_pct=(round(consensus.overall_agreement, 1) if consensus else ""),
        status="ok" if primary.rows else "no_rows",
    )

    # Keep each model's discovered layout beside the workbook: when a column comes
    # back wrong, the layout is usually where it went wrong.
    for run in runs:
        if run.layout and run.layout.data:
            layout_file = run_dir / f"layout-{run.model.replace('/', '_')}.json"
            layout_file.write_text(json.dumps(run.layout.data, indent=2))

    write_workbook(
        workbook_path,
        final_rows=primary.rows,
        final_columns=schema.names,
        raw_rows=[{**r.values, **{k: getattr(r, k) for k in RAW_ROW_META}} for r in _all_raw(runs)],
        raw_columns=list(RAW_ROW_META) + list(schema.names),
        issues=issues,
        calls=telemetry.calls,
        summary=summary,
        accuracy=accuracy_rows,
        column_scores=column_rows,
        mismatches=mismatch_rows,
    )
    append_ledger(ledger_path, summary, telemetry.calls, accuracy_rows, column_rows)

    if telemetry.calls_without_usage:
        log(
            f"  cost warning: {telemetry.calls_without_usage} of {len(telemetry.calls)} "
            f"calls reported no token counts; ${telemetry.total_cost_usd:.4f} is a lower bound"
        )

    return RunOutcome(
        run_dir=run_dir,
        workbook=workbook_path,
        ledger=ledger_path,
        rows=len(primary.rows),
        conflicts=len(consensus.conflicts) if consensus else 0,
        unverified=unverified,
        cost_usd=telemetry.total_cost_usd,
        agreement=consensus.overall_agreement if consensus else None,
        route=primary.profile.route if primary.profile else "text",
        chunks=primary.result.chunks if primary.result else 0,
        accuracy=primary_accuracy,
    )


def _run_models(
    client: SuperAppClient,
    *,
    models: list[str],
    source: SourceDoc,
    schema: TableSchema,
    config: Config,
    context_text: str,
    debug_dir: Path,
    log,
) -> list[ModelRun]:
    """Run each model's full pass. Chunks within a model always run in order.

    Models run concurrently only up to `api.max_concurrent_calls`. Two models
    uploading the same PDF at the same moment is enough to make the endpoint
    time out reading one of the bodies, so setting that to 1 serializes them and
    trades wall clock for a run that finishes.
    """
    if len(models) == 1:
        return [
            _run_model(client, models[0], source, schema, config, context_text, debug_dir, log)
        ]

    workers = max(1, min(len(models), config.api.max_concurrent_calls))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _run_model, client, model, source, schema, config, context_text, debug_dir, log
            ): model
            for model in models
        }
        by_model = {}
        for future in concurrent.futures.as_completed(futures):
            model = futures[future]
            try:
                by_model[model] = future.result()
            except Exception as exc:  # surfaced per model, never fatal for the run
                by_model[model] = ModelRun(model=model, error=str(exc))
    return [by_model[m] for m in models]


def _run_model(
    client: SuperAppClient,
    model: str,
    source: SourceDoc,
    schema: TableSchema,
    config: Config,
    context_text: str,
    debug_dir: Path,
    log,
) -> ModelRun:
    run = ModelRun(model=model)
    chunk_cfg = config.chunking_for(model)

    if source.kind == "text":
        run.layout = Layout(data={})
        run.result = extract_text(
            client,
            model=model,
            text=source.text,
            label=source.name,
            schema=schema,
            layout=run.layout,
            effort=config.extract_effort_for(model),
        )
    else:
        run.profile = profile_pdf(source.path, chunk_cfg)
        log(
            f"  [{model}] profile: {run.profile.pages} pages, "
            f"{run.profile.text_coverage:.0%} with a text layer -> {run.profile.route}"
        )
        run.layout = discover_layout(
            client,
            model=model,
            path=source.path,
            schema=schema,
            profile=run.profile,
            cfg=chunk_cfg,
            context_text=context_text,
            effort=config.reasoning.layout,
        )
        run.result = extract_pdf(
            client,
            model=model,
            path=source.path,
            schema=schema,
            profile=run.profile,
            layout=run.layout,
            cfg=chunk_cfg,
            debug_dir=debug_dir,
            effort=config.extract_effort_for(model),
        )

    run.raw = run.result.rows
    rows = finalize_rows(
        run.raw,
        schema,
        document_values=run.layout.document_values if run.layout else {},
        block_header_policy=bool(
            run.layout and run.layout.policy_number_placement == "block_header"
        ),
        log=lambda message: log(f"  [{model}]{message}"),
    )
    # Render into golden's representation before scoring or writing the sheet.
    run.rows = clean_table(rows, schema)
    log(f"  [{model}] {len(run.raw)} raw rows -> {len(rows)} merged rows")
    return run


def _safe_stem(path: Path) -> str:
    """A path-safe, readable form of the input filename for the run directory."""
    stem = _UNSAFE_NAME_RE.sub("_", path.stem).strip("_")
    return (stem[:MAX_RUN_DIR_STEM].rstrip("_") or "document")


def _primary_source(docs: list[SourceDoc]) -> SourceDoc:
    """Prefer the largest PDF; fall back to the first text artifact."""
    pdfs = [d for d in docs if d.kind == "pdf"]
    if pdfs:
        return max(pdfs, key=lambda d: d.pages)
    return docs[0]


def _all_raw(runs: list[ModelRun]) -> list[RawRow]:
    rows: list[RawRow] = []
    for run in runs:
        rows.extend(run.raw)
    return rows


def _collect_extraction_issues(runs: list[ModelRun], issues: list[dict[str, Any]]) -> None:
    for run in runs:
        if run.error:
            issues.append(
                {
                    "severity": "error",
                    "category": "model_run",
                    "detail": run.error,
                    "source": run.model,
                }
            )
        for event in run.result.events if run.result else []:
            issues.append(
                {
                    "severity": event.level,
                    "category": event.stage,
                    "detail": event.detail,
                    "source": f"{run.model} {event.chunk}".strip(),
                    "value": event.pages,
                }
            )


def _collect_consensus_issues(
    consensus: ConsensusResult, issues: list[dict[str, Any]]
) -> None:
    for conflict in consensus.conflicts:
        issues.append(
            {
                "severity": "warning",
                "category": "model_disagreement",
                "detail": (
                    f"{conflict.primary_model}={conflict.primary_value!r} vs "
                    f"{conflict.other_model}={conflict.other_value!r}"
                ),
                "row_key": " | ".join(conflict.key),
                "column": conflict.column,
                "value": conflict.primary_value,
                "source": conflict.primary_model,
            }
        )
    for key in consensus.only_primary:
        issues.append(
            {
                "severity": "warning",
                "category": "row_only_in_primary",
                "detail": "the second model did not return this row",
                "row_key": " | ".join(key),
            }
        )
    for key in consensus.only_other:
        issues.append(
            {
                "severity": "warning",
                "category": "row_only_in_secondary",
                "detail": "the primary model did not return this row; it is NOT in the final table",
                "row_key": " | ".join(key),
            }
        )


def _collect_adjudication_issues(
    decisions: list[Adjudication], issues: list[dict[str, Any]]
) -> None:
    """Record every tie-break, including the ones that declined to break a tie.

    A conflict the adjudicator would not settle is the one a human should read,
    so it stays a warning; a settled one drops to info as an audit trail.
    """
    for decision in decisions:
        settled = decision.resolved
        winner = decision.candidate_b if decision.choice == "B" else decision.candidate_a
        issues.append(
            {
                "severity": "info" if settled else "warning",
                "category": "adjudicated" if settled else "adjudication_declined",
                "detail": (
                    f"chose {decision.choice}: {decision.reason}"
                    if settled
                    else f"unresolved, kept the primary value: {decision.reason}"
                ),
                "row_key": " | ".join(decision.key),
                "column": decision.column,
                "value": winner,
                "source": "adjudicator",
            }
        )


def _collect_key_issues(key_issues, rows, issues: list[dict[str, Any]]) -> None:
    from .merge import normalize_key

    for issue in key_issues:
        row = rows[issue.row_index] if issue.row_index < len(rows) else {}
        issues.append(
            {
                "severity": "error" if issue.verdict == NOT_FOUND else "warning",
                "category": f"key_{issue.verdict}",
                "detail": (
                    "value does not appear verbatim in the document text — "
                    "possible hallucination"
                    if issue.verdict == NOT_FOUND
                    else "value matches the document only when case is ignored"
                ),
                "row_key": " | ".join(normalize_key(row)) if row else "",
                "column": issue.column,
                "value": issue.value,
            }
        )
