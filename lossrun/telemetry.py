"""Per-call cost and latency accounting.

The API returns token counts, not dollars, so cost is computed locally from the
operator-maintained `pricing` block in config.yaml.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import Pricing

CALL_COLUMNS = (
    "run_id",
    "document",
    "stage",
    "model",
    "provider",
    "label",
    "pages",
    "attempt",
    "effort",
    "response_id",
    "status",
    "latency_s",
    "poll_count",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_input_usd",
    "cost_output_usd",
    "cost_total_usd",
    "usage_missing",
    "error_code",
    "error_message",
)

SUMMARY_COLUMNS = (
    "batch_id",
    "run_id",
    "started_at",
    "document",
    "models",
    "providers",
    "route",
    "effort",
    "pages",
    "chunks",
    "rows",
    "conflicts",
    "unverified_keys",
    "agreement_pct",
    "calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_total_usd",
    "calls_without_usage",
    "wall_clock_s",
    "status",
)


@dataclass
class CallRecord:
    run_id: str
    document: str
    stage: str
    model: str
    # Which transport served the call: superapp, openai or gemini. The same
    # model pin is reachable through more than one, and they do not cost the same.
    provider: str
    label: str
    pages: str
    attempt: int
    effort: str
    response_id: str
    status: str
    latency_s: float
    poll_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_input_usd: float
    cost_output_usd: float
    cost_total_usd: float
    # A completed call that reported no token counts. Its real cost is unknown,
    # not zero — some providers return an empty `usage` object.
    usage_missing: bool = False
    error_code: str = ""
    error_message: str = ""

    def as_row(self) -> list[Any]:
        data = asdict(self)
        return [data[c] for c in CALL_COLUMNS]


@dataclass
class Telemetry:
    run_id: str
    document: str
    pricing: dict[str, Pricing]
    batch_id: str = ""
    started_at: float = field(default_factory=time.time)
    calls: list[CallRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(
        self,
        *,
        stage: str,
        model: str,
        label: str,
        pages: str,
        attempt: int,
        response_id: str,
        provider: str = "",
        effort: str = "",
        status: str,
        latency_s: float,
        poll_count: int,
        input_tokens: int,
        output_tokens: int,
        error_code: str = "",
        error_message: str = "",
    ) -> CallRecord:
        price = self.pricing.get(model, Pricing(0.0, 0.0))
        cost_in = input_tokens / 1_000_000 * price.input
        cost_out = output_tokens / 1_000_000 * price.output
        record = CallRecord(
            run_id=self.run_id,
            document=self.document,
            stage=stage,
            model=model,
            provider=provider,
            label=label,
            pages=pages,
            attempt=attempt,
            effort=effort,
            response_id=response_id,
            status=status,
            latency_s=round(latency_s, 2),
            poll_count=poll_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_input_usd=round(cost_in, 6),
            cost_output_usd=round(cost_out, 6),
            cost_total_usd=round(cost_in + cost_out, 6),
            usage_missing=(status == "completed" and input_tokens == 0 and output_tokens == 0),
            error_code=error_code,
            error_message=error_message[:500],
        )
        with self._lock:
            self.calls.append(record)
        return record

    @property
    def total_cost_usd(self) -> float:
        return round(sum(c.cost_total_usd for c in self.calls), 6)

    @property
    def calls_without_usage(self) -> int:
        """Completed calls the API gave no token counts for.

        Their cost is unknown rather than zero, so a non-zero count means the
        reported total is a lower bound.
        """
        return sum(1 for c in self.calls if c.usage_missing)

    def summary(self, **extra: Any) -> dict[str, Any]:
        row = {
            "batch_id": self.batch_id,
            "run_id": self.run_id,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at)),
            "document": self.document,
            "models": "",
            "providers": "",
            "route": "",
            "effort": "",
            "pages": 0,
            "chunks": 0,
            "rows": 0,
            "conflicts": 0,
            "unverified_keys": 0,
            "agreement_pct": "",
            "calls": len(self.calls),
            "input_tokens": sum(c.input_tokens for c in self.calls),
            "output_tokens": sum(c.output_tokens for c in self.calls),
            "total_tokens": sum(c.total_tokens for c in self.calls),
            "cost_total_usd": self.total_cost_usd,
            "calls_without_usage": self.calls_without_usage,
            "wall_clock_s": round(time.time() - self.started_at, 1),
            "status": "ok",
        }
        row.update(extra)
        return row
