"""Cost arithmetic, the schema contract, and reasoning-effort resolution."""

from __future__ import annotations

import pytest

from lossrun.config import Pricing
from lossrun.schema_loader import (
    BACKFILL_FROM_LAYOUT,
    DOC_LEVEL_COLUMNS,
    KEY_COLUMNS,
    load_schema,
)
from lossrun.telemetry import Telemetry

SCHEMA = load_schema()


def make_telemetry() -> Telemetry:
    return Telemetry(
        run_id="t",
        document="doc.pdf",
        pricing={"m": Pricing(input=0.20, output=0.80)},
    )


def record(telemetry: Telemetry, model: str, tokens_in: int, tokens_out: int):
    return telemetry.record(
        stage="extract",
        model=model,
        label="chunk 1/1",
        pages="1-10",
        attempt=1,
        response_id="resp_1",
        status="completed",
        latency_s=12.0,
        poll_count=4,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
    )


def test_cost_is_tokens_times_price_per_million():
    telemetry = make_telemetry()
    call = record(telemetry, "m", 1_000_000, 500_000)
    assert call.cost_input_usd == 0.20
    assert call.cost_output_usd == 0.40
    assert call.cost_total_usd == 0.60
    assert call.total_tokens == 1_500_000


def test_costs_accumulate_across_calls():
    telemetry = make_telemetry()
    record(telemetry, "m", 100_000, 10_000)
    record(telemetry, "m", 100_000, 10_000)
    assert telemetry.total_cost_usd == round(2 * (0.02 + 0.008), 6)
    assert telemetry.summary()["calls"] == 2
    assert telemetry.summary()["total_tokens"] == 220_000


def test_an_unpriced_model_costs_zero_rather_than_raising():
    telemetry = make_telemetry()
    call = record(telemetry, "unpriced/model", 1_000_000, 1_000_000)
    assert call.cost_total_usd == 0.0
    assert call.usage_missing is False


def test_a_completed_call_with_no_token_counts_is_flagged():
    # Some providers return an empty `usage`. Costing that at $0 silently
    # understates the run; it has to read as unknown instead.
    telemetry = make_telemetry()
    record(telemetry, "m", 1000, 100)
    call = record(telemetry, "m", 0, 0)
    assert call.usage_missing is True
    assert telemetry.calls_without_usage == 1
    assert telemetry.summary()["calls_without_usage"] == 1


def test_a_failed_call_with_no_tokens_is_not_a_usage_gap():
    telemetry = make_telemetry()
    call = telemetry.record(
        stage="extract",
        model="m",
        label="chunk 1/1",
        pages="1-10",
        attempt=1,
        response_id="",
        status="error",
        latency_s=1.0,
        poll_count=0,
        input_tokens=0,
        output_tokens=0,
        error_code="503",
    )
    assert call.usage_missing is False
    assert telemetry.calls_without_usage == 0


def test_summary_extras_override_defaults():
    summary = make_telemetry().summary(rows=42, status="no_rows")
    assert summary["rows"] == 42
    assert summary["status"] == "no_rows"


def test_schema_resolves_the_documented_column_contract():
    names = SCHEMA.names
    assert len(names) == 25
    assert len(set(names)) == 25
    for key in KEY_COLUMNS:
        assert key in names
    assert {c.name for c in SCHEMA.doc_columns} == set(DOC_LEVEL_COLUMNS)
    assert len(SCHEMA.row_columns) == 24


def test_valuation_date_is_extracted_per_row_not_stamped_once():
    """A bundle of loss runs carries one as-of date per carrier section, so the
    extraction pass must be asked for it rather than told it."""
    assert "Valuation Date" in {c.name for c in SCHEMA.row_columns}
    assert "Valuation Date" not in {c.name for c in SCHEMA.doc_columns}
    assert "Valuation Date" in BACKFILL_FROM_LAYOUT, "layout still fills the gaps"


def test_every_column_carries_its_prompt_from_the_schema():
    assert all(c.prompt for c in SCHEMA.columns)
    assert 10_000 < SCHEMA.prompt_bytes() < 64 * 1024


def test_stage_defaults_stay_within_the_universally_supported_levels():
    """A stage default applies to every model, so it cannot be OpenAI-only.

    An unsupported effort is a hard 400 from the API, not a clamp. `xhigh` and
    `max` therefore belong in a per-model override, never in a stage default.
    """
    from lossrun.config import ReasoningConfig

    defaults = ReasoningConfig()
    assert defaults.extract is None, "extraction sends no preference by default"
    assert defaults.layout in {"low", "medium", "high"}


def test_the_shipped_config_puts_max_on_an_openai_pin_only():
    """Guards the Luna Max setup against being widened to a Gemini pin."""
    from lossrun.config import VALID_EFFORTS, load_config

    config = load_config(require_token=False)
    for model in config.routed_models:
        effort = config.extract_effort_for(model)
        if effort in {"xhigh", "max"}:
            assert model.startswith("openai/"), f"{effort} is OpenAI-only, not valid for {model}"
        assert effort is None or effort in VALID_EFFORTS


def test_an_invalid_effort_is_rejected_locally():
    from lossrun.config import _effort

    assert _effort("LOW") == "low"
    assert _effort("default") is None
    assert _effort(None) is None
    with pytest.raises(ValueError, match="not one of"):
        _effort("maximum")


def _config_with(overrides, *, extract=None, forced=None, layout="medium"):
    from lossrun.config import ApiConfig, ChunkingConfig, Config, ReasoningConfig

    return Config(
        api=ApiConfig("https://x/api/v1", 0, 10, 10, 1, 0),
        primary_model="openai/gpt-5.6-luna",
        chunking=ChunkingConfig(50, 2, 5, 12, 1, 3),
        model_overrides=overrides,
        pricing={},
        reasoning=ReasoningConfig(extract=extract, layout=layout),
        token="t",
        forced_extract_effort=forced,
    )


def test_effort_can_be_set_on_one_model_only():
    """max is OpenAI-only, so Luna Max cannot be a global setting.

    Applying it to a Gemini pin is a hard 400 that kills that model's pass.
    """
    config = _config_with({"openai/gpt-5.6-luna": {"effort": "max"}})
    assert config.extract_effort_for("openai/gpt-5.6-luna") == "max"
    assert config.extract_effort_for("gemini/gemini-3.6-flash") is None


def test_a_model_without_an_override_uses_the_stage_default():
    config = _config_with({"openai/gpt-5.6-luna": {"effort": "max"}}, extract="medium")
    assert config.extract_effort_for("gemini/gemini-3.6-flash") == "medium"


def test_the_cli_flag_outranks_a_per_model_override():
    config = _config_with({"openai/gpt-5.6-luna": {"effort": "max"}}, forced="low")
    assert config.extract_effort_for("openai/gpt-5.6-luna") == "low"
    assert config.extract_effort_for("gemini/gemini-3.6-flash") == "low"


def test_the_cli_can_force_no_preference_at_all():
    config = _config_with({"openai/gpt-5.6-luna": {"effort": "max"}}, forced="")
    assert config.extract_effort_for("openai/gpt-5.6-luna") is None


def test_an_effort_override_does_not_leak_into_chunking():
    config = _config_with({"openai/gpt-5.6-luna": {"effort": "max", "max_pages_per_chunk": 40}})
    chunking = config.chunking_for("openai/gpt-5.6-luna")
    assert chunking.max_pages_per_chunk == 40
    assert not hasattr(chunking, "effort")


def test_an_unknown_override_key_still_fails_loudly():
    config = _config_with({"openai/gpt-5.6-luna": {"max_pages": 40}})
    with pytest.raises(ValueError, match="unknown override keys"):
        config.chunking_for("openai/gpt-5.6-luna")


def test_an_invalid_per_model_effort_is_rejected():
    config = _config_with({"openai/gpt-5.6-luna": {"effort": "maximum"}})
    with pytest.raises(ValueError, match="not one of"):
        config.extract_effort_for("openai/gpt-5.6-luna")


def test_the_shipped_config_puts_max_layout_effort_on_an_openai_pin_only():
    """Mirrors the extract-effort guard: max/xhigh are OpenAI-only, so Luna's
    layout_effort override must not have widened to a Gemini pin."""
    from lossrun.config import VALID_EFFORTS, load_config

    config = load_config(require_token=False)
    for model in config.routed_models:
        effort = config.layout_effort_for(model)
        if effort in {"xhigh", "max"}:
            assert model.startswith("openai/"), f"{effort} is OpenAI-only, not valid for {model}"
        assert effort is None or effort in VALID_EFFORTS


def test_layout_effort_can_be_set_on_one_model_only():
    config = _config_with({"openai/gpt-5.6-luna": {"layout_effort": "max"}})
    assert config.layout_effort_for("openai/gpt-5.6-luna") == "max"
    assert config.layout_effort_for("gemini/gemini-3.6-flash") == "medium"


def test_a_model_without_a_layout_override_uses_the_layout_default():
    config = _config_with({"openai/gpt-5.6-luna": {"layout_effort": "max"}}, layout="low")
    assert config.layout_effort_for("gemini/gemini-3.6-flash") == "low"


def test_layout_effort_is_independent_of_extract_effort():
    """A model with an extract-effort override but no layout_effort override
    keeps the layout stage's own default rather than inheriting extract's."""
    config = _config_with({"openai/gpt-5.6-luna": {"effort": "max"}}, extract="low", layout="medium")
    assert config.extract_effort_for("openai/gpt-5.6-luna") == "max"
    assert config.layout_effort_for("openai/gpt-5.6-luna") == "medium"


def test_an_invalid_per_model_layout_effort_is_rejected():
    config = _config_with({"openai/gpt-5.6-luna": {"layout_effort": "maximum"}})
    with pytest.raises(ValueError, match="not one of"):
        config.layout_effort_for("openai/gpt-5.6-luna")


def test_layout_effort_override_does_not_leak_into_chunking():
    config = _config_with(
        {"openai/gpt-5.6-luna": {"layout_effort": "max", "max_pages_per_chunk": 40}}
    )
    chunking = config.chunking_for("openai/gpt-5.6-luna")
    assert chunking.max_pages_per_chunk == 40
    assert not hasattr(chunking, "layout_effort")
