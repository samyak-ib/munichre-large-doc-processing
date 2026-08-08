"""Scoring an extraction against golden data.

Agreement between models says two models read a document the same way. This
says whether they read it *correctly*, which is the only number that answers the
question the project exists to answer.

Every model in a run is scored separately, so Luna and Gemini are directly
comparable rather than being collapsed into the primary model's result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from .cleaning import normalize_dashes
from .merge import is_empty
from .schema_loader import DATE_COLUMNS, MONEY_COLUMNS, TableSchema

# Golden header -> schema column. Anything unmapped is ignored on both sides;
# `Policy Year` has no golden counterpart and is reported as unscored.
GOLDEN_TO_SCHEMA = {
    "Insured Name": "Insured",
    "Carrier or Provider of Loss Run": "Insurer Loss Run",
    "Valuation Date (MM/DD/YYYY)": "Valuation Date",
    "Policy Number": "Policy Number",
    "Policy Effective Date (MM/DD/YYYY)": "Policy Effective Date",
    "Policy Expiration Date (MM/DD/YYYY)": "Policy Expiration Date",
    "Line of Business (As-is)": "Line of Business",
    "Claim ID": "Claim Number",
    "Occurrence ID": "Occurrence ID",
    "Claimant Name": "Claimant Name",
    "Accident Date (MM/DD/YYYY)": "Accident Date",
    "Reported Date (MM/DD/YYYY)": "Report Date",
    "Claim Status": "Claim Status",
    "Closed Date (MM/DD/YYYY)": "Closed Date",
    "Loss Description": "Description",
    "Expense Paid": "Expense Paid",
    "Expense Reserved": "Expense Reserved",
    "Indemnity Paid": "Indemnity Paid",
    "Indemnity Reserve": "Indemnity Reserve",
    "Recovery (Deductible)": "Recovery Deductible",
    "Recovery (Salvage, Subro, Reins)": "Recovery Salvage Subro Reins",
    "Loss State": "Loss State",
    "Claim Total": "Claim Total",
    "Policy Total": "Policy Total",
}

FILENAME_COLUMN = "Filename"
MATCH_COLUMN = "Claim Number"
MONEY_TOLERANCE = 0.01

# Two descriptions this similar are the same description. Calibrated against the
# recorded mismatches: it accepts golden's 50-character truncations and refuses
# narrative-versus-taxonomy pairs, which are different facts rather than
# different wordings.
DESCRIPTION_SIMILARITY = 0.80


@dataclass(frozen=True)
class ScoringPolicy:
    """Which equivalences the scorer honours.

    Every flag is an assumption about the golden data rather than about the
    extraction, so each one can be switched off to measure what it contributes.
    """

    # Cells where golden holds nothing are counted and reported, never scored.
    ignore_golden_blank: bool = True
    # A blank money cell and an explicit 0 assert the same fact.
    money_empty_is_zero: bool = True
    # Excel coerced numeric claim ids to floats, dropping their leading zeros.
    leading_zero_tolerant_ids: bool = True
    # CLOSED / C / "Settled / Closed" are one status.
    status_synonyms: bool = True
    # TX and Texas are one state.
    state_synonyms: bool = True
    # Descriptions that say the same thing in different words are equal.
    semantic_description: bool = True

    def without(self, flag: str) -> ScoringPolicy:
        """This policy with one flag switched off, for leave-one-out scoring."""
        return replace(self, **{flag: False})


DEFAULT_POLICY = ScoringPolicy()

# The order these are reported in, with the label each carries in the write-up.
POLICY_FLAGS = (
    ("ignore_golden_blank", "Cells where golden is blank are not scored"),
    ("money_empty_is_zero", "An empty money cell equals 0"),
    ("leading_zero_tolerant_ids", "Identifiers may differ by leading zeros"),
    ("status_synonyms", "Claim status synonyms are equal"),
    ("state_synonyms", "Loss state names and codes are equal"),
    ("semantic_description", "Descriptions that mean the same are equal"),
)

ACCURACY_COLUMNS = (
    "batch_id",
    "run_id",
    "started_at",
    "document",
    "pages",
    "model",
    "effort",
    "rows_golden",
    "rows_extracted",
    "rows_matched",
    "rows_missing",
    "rows_extra",
    "row_recall_pct",
    "row_precision_pct",
    "cells_compared",
    "cells_correct",
    "cell_accuracy_pct",
    # Mean per-row cell accuracy: each row counts once, regardless of how many
    # scorable columns it carries. `_matched` covers the rows we found;
    # `_overall` charges a row we missed as 0%.
    "row_accuracy_matched_pct",
    "row_accuracy_overall_pct",
    "cells_unscored_golden_blank",
    "exact_row_pct",
)

COLUMN_SCORE_COLUMNS = (
    "batch_id",
    "run_id",
    "document",
    "pages",
    "model",
    "column",
    "compared",
    "correct",
    "accuracy_pct",
)

MISMATCH_COLUMNS = ("model", "claim_number", "column", "extracted", "golden")

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^a-z0-9]+")
_MONEY_RE = re.compile(r"[^0-9.\-]")
# `%d-%b-%y` and its siblings are here for the same reason as in `cleaning`: a
# document that prints `13-Jul-17` throughout would otherwise score zero on every
# date column, measuring this parser rather than the extraction.
_DATE_FORMATS = (
    "%m/%d/%Y",
    "%Y-%m-%d",
    "%m/%d/%y",
    "%d/%m/%Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d-%b-%Y",
    "%d-%B-%Y",
    "%d-%b-%y",
    "%d-%B-%y",
    "%d %b %Y",
    "%d %B %Y",
)


@dataclass
class ColumnScore:
    column: str
    compared: int = 0
    correct: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.compared * 100 if self.compared else 0.0


@dataclass
class AccuracyResult:
    model: str
    rows_golden: int = 0
    rows_extracted: int = 0
    rows_matched: int = 0
    missing_keys: list[str] = field(default_factory=list)
    extra_keys: list[str] = field(default_factory=list)
    columns: dict[str, ColumnScore] = field(default_factory=dict)
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    exact_rows: int = 0
    # Cells we filled where golden is blank. Reported, never scored.
    spurious: int = 0
    # Per matched row, that row's own cell accuracy. Kept per row rather than
    # pooled because the pooled number is dominated by whichever rows happen to
    # carry the most scorable cells.
    row_accuracies: list[float] = field(default_factory=list)

    @property
    def row_recall(self) -> float:
        return self.rows_matched / self.rows_golden * 100 if self.rows_golden else 0.0

    @property
    def row_precision(self) -> float:
        return self.rows_matched / self.rows_extracted * 100 if self.rows_extracted else 0.0

    @property
    def cells_compared(self) -> int:
        return sum(c.compared for c in self.columns.values())

    @property
    def cells_correct(self) -> int:
        return sum(c.correct for c in self.columns.values())

    @property
    def cell_accuracy(self) -> float:
        return self.cells_correct / self.cells_compared * 100 if self.cells_compared else 0.0

    @property
    def exact_row_rate(self) -> float:
        """Rows where every compared cell is right — the end-to-end number."""
        return self.exact_rows / self.rows_golden * 100 if self.rows_golden else 0.0

    @property
    def row_accuracy(self) -> float:
        """Mean per-row cell accuracy over the rows that were found.

        "How correct is the data we returned" — each row counts once, so a
        20-cell row and a 3-cell row weigh the same. That is the difference from
        `cell_accuracy`, which pools every cell and is therefore pulled toward
        whichever rows carry the most scorable columns.
        """
        if not self.row_accuracies:
            return 0.0
        return sum(self.row_accuracies) / len(self.row_accuracies)

    @property
    def row_accuracy_overall(self) -> float:
        """The same, but a golden row we never found scores zero rather than
        being left out — recall and correctness in one number."""
        if not self.rows_golden:
            return 0.0
        return sum(self.row_accuracies) / self.rows_golden

    def summary_row(self, **context: Any) -> dict[str, Any]:
        row = {
            "model": self.model,
            "rows_golden": self.rows_golden,
            "rows_extracted": self.rows_extracted,
            "rows_matched": self.rows_matched,
            "rows_missing": len(self.missing_keys),
            "rows_extra": len(self.extra_keys),
            "row_recall_pct": round(self.row_recall, 1),
            "row_precision_pct": round(self.row_precision, 1),
            "cells_compared": self.cells_compared,
            "cells_correct": self.cells_correct,
            "cell_accuracy_pct": round(self.cell_accuracy, 1),
            "row_accuracy_matched_pct": round(self.row_accuracy, 1),
            "row_accuracy_overall_pct": round(self.row_accuracy_overall, 1),
            "cells_unscored_golden_blank": self.spurious,
            "exact_row_pct": round(self.exact_row_rate, 1),
        }
        row.update(context)
        return row


def load_golden(path: Path, document_name: str) -> list[dict[str, str]] | None:
    """Golden rows for one document, or None when the file has no entry for it.

    Every sheet is read. The golden workbook grew a second sheet when the sample
    set was extended, and taking only the first silently scores the new documents
    as having no golden at all.
    """
    if not path.exists():
        return None
    by_document = _golden_index(path)
    filename = _resolve_golden_filename(list(by_document), document_name)
    if filename is None:
        return None
    return [
        {
            schema_column: _as_text(record.get(golden_column))
            for golden_column, schema_column in GOLDEN_TO_SCHEMA.items()
        }
        for record in by_document[filename]
    ]


def _golden_index(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Every golden row in the workbook, grouped by the filename it belongs to."""
    workbook = load_workbook(path, data_only=True)
    grouped: dict[str, list[dict[str, Any]]] = {}
    try:
        for name in workbook.sheetnames:
            sheet = workbook[name]
            header = [c.value for c in sheet[1]]
            if FILENAME_COLUMN not in header:
                continue
            for raw in sheet.iter_rows(min_row=2, values_only=True):
                if not any(v is not None for v in raw):
                    continue
                record = dict(zip(header, raw))
                document = str(record.get(FILENAME_COLUMN, "") or "").strip()
                if document:
                    grouped.setdefault(document, []).append(record)
    finally:
        workbook.close()
    return grouped


def _resolve_golden_filename(candidates: list[str], document_name: str) -> str | None:
    """Which golden filename this document is, or None.

    An exact match always wins. Loose containment exists because a document is
    often renamed after the golden set was built — but with 29 documents whose
    names are substrings of one another it is dangerous on its own: `Loss
    Runs.pdf` is contained in `WC Loss Runs.pdf`, `GLI Loss Runs.PDF` and six
    more, and a containment-first rule scores it against 155 rows from eight
    different documents. So containment is the fallback, and when it is
    ambiguous the closest name by similarity wins rather than all of them.
    """
    wanted = _normalize_filename(document_name)
    if not wanted:
        return None

    exact = [c for c in candidates if _normalize_filename(c) == wanted]
    if exact:
        return exact[0]

    # A golden entry that carries the whole document name inside a longer one is
    # the renamed-original case this fallback exists for, and it is much stronger
    # evidence than the reverse. `LRs_Application_CAU CPP MAR PKG WCO Loss
    # Runs.PDF` appears in golden under a GUID-prefixed name that contains it in
    # full, while a *different* golden entry named `CAU CPP MAR PKG WCO Loss
    # Runs.PDF` is merely contained in it — they are two transcriptions of one
    # document and they disagree, so which one is chosen has to be deliberate.
    superset = [c for c in candidates if wanted and wanted in _normalize_filename(c)]
    subset = [c for c in candidates if _normalize_filename(c) and _normalize_filename(c) in wanted]
    for group in (superset, subset):
        if len(group) == 1:
            return group[0]
        if group:
            # Longest name wins: it is the most specific match. Sorted first so
            # the result never depends on the order the sheets were read in.
            return max(sorted(group), key=lambda c: len(_normalize_filename(c)))
    return None


def score(
    extracted: list[dict[str, str]],
    golden: list[dict[str, str]],
    schema: TableSchema,
    model: str,
    policy: ScoringPolicy = DEFAULT_POLICY,
) -> AccuracyResult:
    """Score one model's table against golden, matched on claim number."""
    scored_columns = [c for c in schema.names if c in set(GOLDEN_TO_SCHEMA.values())]
    result = AccuracyResult(
        model=model,
        rows_golden=len(golden),
        rows_extracted=len(extracted),
        columns={c: ColumnScore(c) for c in scored_columns},
    )

    golden_by_key = {_match_key(r): r for r in golden}
    extracted_by_key = {_match_key(r): r for r in extracted}
    # Excel stores a numeric-looking claim id as a number, so golden can hold
    # 40512146091 where the document prints 040512146091. Index the loose form
    # too, or a correct extraction scores zero for a spreadsheet's typing rule.
    loose_extracted = (
        {_loose_key(k): v for k, v in extracted_by_key.items() if _loose_key(k)}
        if policy.leading_zero_tolerant_ids
        else {}
    )

    matched_keys: set[str] = set()
    for key in golden_by_key:
        if key in extracted_by_key:
            matched_keys.add(key)
        elif _loose_key(key) in loose_extracted:
            matched_keys.add(key)

    result.missing_keys = sorted(k for k in golden_by_key if k and k not in matched_keys)
    result.extra_keys = sorted(
        k
        for k in extracted_by_key
        if k and k not in golden_by_key and _loose_key(k) not in {_loose_key(m) for m in matched_keys}
    )

    for key, golden_row in golden_by_key.items():
        found = extracted_by_key.get(key) or loose_extracted.get(_loose_key(key))
        if found is None:
            continue
        result.rows_matched += 1
        row_correct = True
        row_compared = row_correct_cells = 0
        for column in scored_columns:
            actual, expected = found.get(column, ""), golden_row.get(column, "")
            if is_empty(actual) and is_empty(expected):
                continue  # neither side claims a value; nothing to score
            if is_empty(expected):
                # Golden holds no value here. Whether the extraction is right is
                # unknowable from this data, so it is counted and reported but
                # never scored — it can neither help nor hurt the number.
                result.spurious += 1
                if policy.ignore_golden_blank:
                    continue
            score_entry = result.columns[column]
            score_entry.compared += 1
            row_compared += 1
            if values_match(column, actual, expected, policy):
                score_entry.correct += 1
                row_correct_cells += 1
            else:
                row_correct = False
                result.mismatches.append(
                    {
                        "model": model,
                        "claim_number": found.get(MATCH_COLUMN, ""),
                        "column": column,
                        "extracted": actual,
                        "golden": expected,
                    }
                )
        # A row with nothing scorable — golden blank across the board — would
        # otherwise enter the mean as 0% and read as a failure.
        if row_compared:
            result.row_accuracies.append(row_correct_cells / row_compared * 100)
        if row_correct:
            result.exact_rows += 1

    return result


# Golden records categoricals as-is, so the same fact appears in several forms:
# CLOSED / C / Closed / "C (Closed)" / "Settled / Closed", and TX / Texas. A
# model that writes one form where golden holds the other is right, not wrong,
# so equivalence lives here rather than being forced onto the data by cleaning.
_STATUS_CLASSES = {
    "closed": "closed",
    "c": "closed",
    "c (closed)": "closed",
    "settled / closed": "closed",
    "settled/closed": "closed",
    "open": "open",
    "o": "open",
    "reopened": "reopened",
    "re-opened": "reopened",
    "r": "reopened",
}

_US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}


def values_match(
    column: str, actual: str, expected: str, policy: ScoringPolicy = DEFAULT_POLICY
) -> bool:
    """Compare one cell, using the comparison the column's type deserves."""
    if is_empty(actual) and is_empty(expected):
        return True
    if column in MONEY_COLUMNS and policy.money_empty_is_zero:
        # A blank money cell and an explicit 0 are the same fact. The schema
        # tells the model to return N/A when it finds nothing; golden writes 0.
        # Scoring that disagreement as an error measures the two conventions,
        # not the extraction.
        if _is_zeroish(actual) and _is_zeroish(expected):
            return True
    if is_empty(actual) or is_empty(expected):
        return False
    if column in {"Claim Number", "Occurrence ID", "Policy Number"} and policy.leading_zero_tolerant_ids:
        # Same spreadsheet coercion as the match key: a purely numeric id that
        # lost its leading zero in golden is not an extraction error.
        if _identifiers_match(actual, expected):
            return True
    if column == "Claim Status" and policy.status_synonyms:
        left, right = _status_class(actual), _status_class(expected)
        if left and right:
            return left == right
    if column == "Loss State" and policy.state_synonyms:
        return _state_code(actual) == _state_code(expected)
    if column == "Description" and policy.semantic_description:
        return _descriptions_match(actual, expected)
    if column in MONEY_COLUMNS:
        left, right = _as_money(actual), _as_money(expected)
        if left is not None and right is not None:
            return abs(left - right) <= MONEY_TOLERANCE
    if column in DATE_COLUMNS:
        left, right = _as_date(actual), _as_date(expected)
        if left is not None and right is not None:
            return left == right
    return _as_comparable(actual) == _as_comparable(expected)


def _descriptions_match(actual: str, expected: str) -> bool:
    """True when two loss descriptions state the same thing.

    A description is prose, so punctuation, case and line breaks carry no
    meaning. Golden also clips the column at 50 characters, which is why one
    string being a prefix of the other counts as agreement.
    """
    left, right = _as_words(actual), _as_words(expected)
    if not left or not right:
        return False
    if left == right or left.startswith(right) or right.startswith(left):
        return True
    left_tokens, right_tokens = set(left.split()), set(right.split())
    jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    ratio = SequenceMatcher(None, left, right).ratio()
    return max(jaccard, ratio) >= DESCRIPTION_SIMILARITY


def _loose_key(key: str) -> str:
    """A claim id with leading zeros removed, for spreadsheet-coerced golden."""
    return key.lstrip("0")


def _identifiers_match(actual: str, expected: str) -> bool:
    """Compare two identifiers the way the row key already compares them.

    Whitespace is removed rather than collapsed, because a PDF wraps a long
    identifier mid-string: the text layer reads `001- WC19A-78355` where the
    table means `001-WC19A-78355`. `merge.normalize_key` settled that those are
    one claim; scoring them as two different values would contradict the match
    that put them in the same row.
    """
    left, right = _WS_RE.sub("", str(actual)).casefold(), _WS_RE.sub("", str(expected)).casefold()
    if left == right:
        return True
    if not (left.isdigit() and right.isdigit()):
        return False
    return left.lstrip("0") == right.lstrip("0")


def _is_zeroish(value: str) -> bool:
    """True for a money cell that asserts nothing, or asserts zero.

    `N/A`, blank, `0`, `0.00` and `$0.00` all say the same thing about a claim.
    """
    if is_empty(value):
        return True
    amount = _as_money(value)
    return amount is not None and amount == 0


def _status_class(value: str) -> str:
    return _STATUS_CLASSES.get(_as_comparable(value), "")


def _state_code(value: str) -> str:
    text = _as_comparable(value)
    return _US_STATES.get(text, text.upper())


def _match_key(row: dict[str, str]) -> str:
    """Claim number alone: it is unique per document, and unlike the merge key
    it does not depend on the claimant name the model may have got wrong."""
    value = row.get(MATCH_COLUMN, "")
    return "" if is_empty(value) else _WS_RE.sub("", str(value)).casefold()


def _as_comparable(value: str) -> str:
    return _WS_RE.sub(" ", str(value)).strip().casefold()


def _as_words(value: str) -> str:
    """Lower-case words separated by single spaces, punctuation dropped."""
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", str(value).casefold())).strip()


def _as_money(value: str) -> float | None:
    cleaned = _MONEY_RE.sub("", str(value).replace("(", "-").replace(")", ""))
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _as_date(value: str) -> date | None:
    """Parse a date written in any of the forms the two sides use.

    The whole string is tried before the first token, because `Jan 5, 2023`
    contains spaces that matter while `2023-01-05 00:00:00` has a time to drop.
    """
    text = normalize_dashes(str(value).strip())
    if not text:
        return None
    for candidate in (text, text.split(" ")[0]):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def _as_text(value: object) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, datetime):
        return value.strftime("%m/%d/%Y")
    if isinstance(value, date):
        return value.strftime("%m/%d/%Y")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return "N/A" if text.lower() in {"", "none", "nan"} else text


def _normalize_filename(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


def _filenames_match(golden: str, wanted: str) -> bool:
    if not golden or not wanted:
        return False
    return golden == wanted or wanted in golden or golden in wanted
