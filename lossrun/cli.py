"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from . import compare
from .accuracy import POLICY_FLAGS, load_golden
from .config import AUTO_PROVIDER, VALID_PROVIDERS, load_config
from .pipeline import DEFAULT_GOLDEN_PATH, run_document
from .report import batch_ids_for, read_batch_telemetry
from .rescore import load_run, policy_influence
from .results import BatchResults, batch_filename, write_results
from .schema_loader import load_schema
from .router import ModelRouter
from .superapp_client import SuperAppError, TokenExpired
from .telemetry import Telemetry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lossrun",
        description="Extract a loss-run claim table into Excel via the SuperApp Responses API.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract", help="extract one or more documents")
    extract.add_argument("inputs", nargs="+", type=Path, help="pdf/eml/msg files or a directory")
    extract.add_argument("--config", type=Path, help="path to config.yaml")
    extract.add_argument("--schema", type=Path, help="path to schema.json")
    extract.add_argument(
        "--out",
        type=Path,
        default=Path("out"),
        help="output directory; runs land in <out>/{direct,superapp,mixed}_calls/ "
        "and the cumulative ledger in <out>/ itself",
    )
    extract.add_argument("--model", help="override the primary model")
    extract.add_argument(
        "--qa-model",
        metavar="MODEL",
        help="model that reviews the extracted table (default: the primary model)",
    )
    extract.add_argument("--base-url", help="override the API base URL")
    extract.add_argument(
        "--golden",
        type=Path,
        help="golden workbook to score against (default: goldens/Loss Runs GTs (1).xlsx)",
    )
    extract.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh", "max", "default"],
        help="reasoning effort for extraction (config default: low). "
        "xhigh and max are OpenAI-only.",
    )
    extract.add_argument(
        "--provider",
        choices=[AUTO_PROVIDER, *VALID_PROVIDERS],
        help="force one route for every model, ignoring per-model overrides. "
        "auto (the default) calls each vendor directly where its API key is "
        "set and falls back to superapp where it is not.",
    )
    extract.add_argument(
        "--results-out",
        type=Path,
        default=Path("initial_results"),
        help="folder for the batch results workbook, rewritten after every "
        "document (pass an empty string to skip it)",
    )
    extract.add_argument(
        "--no-qa",
        dest="qa",
        action="store_false",
        default=None,
        help="ship the extracted table unreviewed, skipping the QA pass",
    )

    check = sub.add_parser("check", help="verify credentials and API reachability")
    check.add_argument("--config", type=Path, help="path to config.yaml")
    check.add_argument("--model", help="model to probe with")
    check.add_argument("--base-url", help="override the API base URL")
    check.add_argument(
        "--provider",
        choices=[AUTO_PROVIDER, *VALID_PROVIDERS],
        help="force the route to probe, instead of the configured one",
    )

    schema_cmd = sub.add_parser("schema", help="print the resolved column contract")
    schema_cmd.add_argument("--schema", type=Path, help="path to schema.json")

    score_cmd = sub.add_parser(
        "score",
        help="re-score finished runs from their workbooks, without any API calls",
    )
    score_cmd.add_argument("run_dirs", nargs="+", type=Path, help="out/<doc>_<timestamp> directories")
    score_cmd.add_argument("--schema", type=Path, help="path to schema.json")
    score_cmd.add_argument(
        "--golden",
        type=Path,
        default=DEFAULT_GOLDEN_PATH,
        help="golden workbook to score against",
    )
    score_cmd.add_argument(
        "--influence",
        action="store_true",
        help="also report what each scoring assumption is worth",
    )

    results_cmd = sub.add_parser(
        "results", help="write a shareable results workbook for a batch of runs"
    )
    results_cmd.add_argument("run_dirs", nargs="+", type=Path, help="out/<doc>_<timestamp> directories")
    results_cmd.add_argument("--schema", type=Path, help="path to schema.json")
    results_cmd.add_argument(
        "--golden", type=Path, default=DEFAULT_GOLDEN_PATH, help="golden workbook"
    )
    results_cmd.add_argument(
        "--out", type=Path, default=Path("initial_results"), help="where to write the workbook"
    )
    results_cmd.add_argument("--version", default="v1", help="filename prefix, e.g. v1")
    results_cmd.add_argument(
        "--ledger",
        type=Path,
        help="ledger to pull this batch's telemetry from; defaults to the "
        "telemetry.xlsx beside the given run directories",
    )
    results_cmd.add_argument(
        "--batch",
        help="comma-separated batch ids to pull telemetry for; inferred from the "
        "run directories when omitted",
    )

    compare_cmd = sub.add_parser(
        "compare",
        help="score two or more sets of runs side by side, for cost and accuracy",
    )
    compare_cmd.add_argument(
        "groups",
        nargs="+",
        metavar="LABEL=PATH",
        help="a labelled set of runs, e.g. consensus=out/direct_calls. PATH is a "
        "run directory or a folder of them; repeat the label to add more paths",
    )
    compare_cmd.add_argument("--schema", type=Path, help="path to schema.json")
    compare_cmd.add_argument(
        "--golden", type=Path, default=DEFAULT_GOLDEN_PATH, help="golden workbook"
    )
    compare_cmd.add_argument(
        "--out",
        type=Path,
        default=Path("comparisons/comparison.xlsx"),
        help="where to write the comparison workbook",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "schema":
            return _cmd_schema(args)
        if args.command == "check":
            return _cmd_check(args)
        if args.command == "score":
            return _cmd_score(args)
        if args.command == "results":
            return _cmd_results(args)
        if args.command == "compare":
            return _cmd_compare(args)
        return _cmd_extract(args)
    except TokenExpired as exc:
        print(f"\nauth: {exc}", file=sys.stderr)
        return 2
    except (SuperAppError, ValueError, RuntimeError) as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


def _cmd_schema(args: argparse.Namespace) -> int:
    schema = load_schema(args.schema)
    print(f"{len(schema.columns)} columns, {schema.prompt_bytes() / 1024:.1f} KiB of prompts\n")
    for column in schema.columns:
        scope = "document" if column.doc_level else "row"
        print(f"  {column.name:<32} {scope:<9} {len(column.prompt):>5} bytes of prompt")
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    """Re-score finished runs offline. No API calls, so scoring rules are free
    to change without paying for the extraction again."""
    schema = load_schema(args.schema)
    records = [load_run(d, schema) for d in args.run_dirs]

    total_cells = total_correct = total_matched = total_golden = 0
    for record in records:
        golden = load_golden(args.golden, record.document)
        if not golden:
            print(f"{record.document}: no golden entry")
            continue
        print(f"\n{record.document}  ({record.pages} pages, {len(golden)} golden rows)")
        for model, result in record.score_all(golden, schema).items():
            print(
                f"  {model:<28} recall {result.row_recall:5.1f}%  "
                f"cells {result.cell_accuracy:5.1f}%  "
                f"exact rows {result.exact_row_rate:5.1f}%"
            )
            total_cells += result.cells_compared
            total_correct += result.cells_correct
            total_matched += result.rows_matched
            total_golden += result.rows_golden

    if total_cells:
        print(
            f"\nOVERALL  row recall {total_matched / total_golden * 100:.1f}%  "
            f"cell accuracy {total_correct / total_cells * 100:.1f}%  "
            f"({total_correct}/{total_cells} cells)"
        )

    if args.influence:
        print("\nWHAT EACH SCORING ASSUMPTION IS WORTH")
        for row in policy_influence(records, args.golden, schema, POLICY_FLAGS):
            print(
                f"  {row['assumption']:<46} {row['accuracy_with_pct']:5.1f}% -> "
                f"{row['accuracy_without_pct']:5.1f}%  ({row['influence_pp']:+.1f} pp, "
                f"{row['rows_influence']:+d} rows matched)"
            )
    return 0


def _cmd_results(args: argparse.Namespace) -> int:
    schema = load_schema(args.schema)
    records = [load_run(d, schema) for d in args.run_dirs]
    # Each route keeps its own ledger beside its runs, so the run directories
    # name the ledger; a batch never spans two of them.
    ledger = args.ledger or args.run_dirs[0].parent / "telemetry.xlsx"
    batches = (
        {b.strip() for b in args.batch.split(",")}
        if args.batch
        else batch_ids_for(ledger, [r.run_dir for r in records])
    )
    ledger_rows, call_rows = read_batch_telemetry(ledger, batches)
    batch_id = ", ".join(sorted(batches))

    path = write_results(
        BatchResults(
            batch_id=batch_id,
            records=records,
            schema=schema,
            golden_path=args.golden,
            ledger_rows=ledger_rows,
            call_rows=call_rows,
        ),
        args.out,
        version=args.version,
    )
    print(f"wrote {path}")
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    """Score labelled sets of finished runs against each other. No API calls."""
    schema = load_schema(args.schema)
    grouped: dict[str, list[Path]] = {}
    for item in args.groups:
        if "=" not in item:
            raise ValueError(
                f"expected LABEL=PATH, got {item!r} — e.g. consensus=out/direct_calls"
            )
        label, _, path = item.partition("=")
        label = label.strip()
        if not label or not path.strip():
            raise ValueError(f"expected LABEL=PATH, got {item!r}")
        target = Path(path.strip())
        if not target.exists():
            raise ValueError(f"no such path: {target}")
        grouped.setdefault(label, []).append(target)

    groups = [
        compare.collect(label, paths, schema, args.golden)
        for label, paths in grouped.items()
    ]
    for group in groups:
        scored = sum(1 for d in group.documents if d.accuracy is not None)
        print(f"{group.label}: {len(group.documents)} runs, {scored} with a golden entry")
        if not group.documents:
            raise ValueError(f"{group.label}: no run directories found")

    for totals in (g.totals() for g in groups):
        print(
            f"\n{totals['label']}\n"
            f"  rows      {totals['rows_matched']}/{totals['rows_golden']} matched"
            f"  (recall {totals['row_recall_pct']}%, precision {totals['row_precision_pct']}%)\n"
            f"  row acc   {totals['row_accuracy_matched_pct']}% of what came back,"
            f" {totals['row_accuracy_overall_pct']}% over every golden row\n"
            f"  cell acc  {totals['cell_accuracy_pct']}%"
            f"  ({totals['cells_correct']}/{totals['cells_compared']} cells)\n"
            f"  cost      ${totals['cost_total_usd']:.4f}"
            f"  = ${totals['cost_input_usd']:.4f} in + ${totals['cost_output_usd']:.4f} out"
            f"  over {totals['calls']:.0f} calls, {totals['wall_clock_min']} min"
        )

    path = compare.write_comparison(groups, args.out)
    print(f"\nwrote {path}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    config = load_config(
        args.config,
        primary_model=args.model,
        base_url=args.base_url,
        provider=args.provider,
    )
    model = args.model or config.primary_model
    telemetry = Telemetry(run_id="check", document="-", pricing=config.pricing)
    print(f"provider={config.provider_for(model)}  model={model}")
    with ModelRouter(config=config, telemetry=telemetry) as client:
        result = client.run(
            model=model,
            prompt="Reply with exactly: ok",
            stage="check",
            label="connectivity",
        )
    call = telemetry.calls[-1] if telemetry.calls else None
    print(f"  status   {result.status}")
    print(f"  output   {result.output_text.strip()[:80]!r}")
    if call:
        print(
            f"  tokens   {call.input_tokens} in / {call.output_tokens} out"
            f"   cost ${call.cost_total_usd:.6f}   {call.latency_s}s"
        )
    return 0 if result.ok else 1


def _cmd_extract(args: argparse.Namespace) -> int:
    config = load_config(
        args.config,
        primary_model=args.model,
        base_url=args.base_url,
        extract_effort=args.effort,
        qa=args.qa,
        qa_model=args.qa_model,
        provider=args.provider,
    )
    paths = _expand_inputs(args.inputs)
    if not paths:
        raise ValueError("no input files found")

    model = config.primary_model
    print(f"model: {model}")
    print(
        f"effort: layout={config.reasoning.layout or 'default'}  "
        f"extract={config.extract_effort_for(model) or 'default'}"
    )
    routes = ", ".join(
        f"{m.rsplit('/', 1)[-1]}->{config.provider_for(m)}" for m in config.routed_models
    )
    print(f"routing: {routes}")
    print(f"output: {args.out / (config.route_class([model]) + '_calls')}/")
    print(f"qa: {'on, ' + config.qa_model if config.qa_enabled else 'off'}")

    # One id for this invocation, so a multi-document run can be pulled back out
    # of the cumulative ledger as a single experiment.
    batch_id = uuid.uuid4().hex[:8]
    print(f"batch: {batch_id}")

    # Named once so every publish rewrites the same workbook. A long batch is
    # then readable while it is still running, rather than only at the end.
    results_name = batch_filename()
    completed: list[Path] = []

    failures = 0
    for path in paths:
        print(f"\n{path.name}")
        try:
            outcome = run_document(
                path,
                config=config,
                out_dir=args.out,
                schema_path=args.schema,
                golden_path=args.golden,
                batch_id=batch_id,
                log=print,
            )
        except (SuperAppError, ValueError, RuntimeError) as exc:
            failures += 1
            print(f"  failed: {exc}", file=sys.stderr)
            continue

        print(
            f"  done: {outcome.rows} rows  route={outcome.route}  chunks={outcome.chunks}  "
            f"qa_findings={outcome.qa_findings}  qa_applied={outcome.qa_applied}  "
            f"unverified_keys={outcome.unverified}  cost=${outcome.cost_usd:.4f}"
        )
        if outcome.accuracy:
            a = outcome.accuracy
            print(
                f"  ACCURACY ({a.model}): {a.cell_accuracy:.1f}% cells  "
                f"{a.exact_row_rate:.1f}% rows fully correct  "
                f"recall {a.row_recall:.1f}%  precision {a.row_precision:.1f}%"
            )
        print(f"  wrote  {outcome.run_dir}/")
        print(f"  ledger {outcome.ledger}")

        completed.append(outcome.run_dir)
        if args.results_out:
            published = _publish(
                completed, batch_id, args, results_name, outcome.ledger
            )
            if published:
                print(f"  results {published}")

    return 1 if failures else 0


def _publish(
    run_dirs: list[Path],
    batch_id: str,
    args: argparse.Namespace,
    filename: str,
    ledger: Path,
) -> Path | None:
    """Rewrite the batch results workbook with every document finished so far.

    A publishing failure must not lose an extraction that has already been paid
    for, so this reports the problem and lets the batch carry on.
    """
    try:
        schema = load_schema(args.schema)
        records = [load_run(d, schema) for d in run_dirs]
        ledger_rows, call_rows = read_batch_telemetry(ledger, batch_id)
        return write_results(
            BatchResults(
                batch_id=batch_id,
                records=records,
                schema=schema,
                golden_path=args.golden or DEFAULT_GOLDEN_PATH,
                ledger_rows=ledger_rows,
                call_rows=call_rows,
            ),
            args.results_out,
            filename=filename,
        )
    except (ValueError, OSError, KeyError) as exc:
        print(f"  results: not written ({exc})", file=sys.stderr)
        return None


def _expand_inputs(inputs: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for item in inputs:
        if item.is_dir():
            paths.extend(sorted(p for p in item.iterdir() if p.is_file() and not p.name.startswith(".")))
        elif item.is_file():
            paths.append(item)
        else:
            raise ValueError(f"no such file: {item}")
    return paths


if __name__ == "__main__":
    raise SystemExit(main())
