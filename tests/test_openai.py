"""Direct OpenAI client: transport, credential handling, and route resolution.

The client inherits SuperApp's create/poll/retry loop, so what needs pinning here
is everything the subclass changes — where the request goes, what authenticates
it, which model id reaches the wire — plus the `auto` resolution that decides
whether it is built at all.
"""

from __future__ import annotations

import json

import httpx
import pytest

from lossrun.config import (
    ApiConfig,
    ChunkingConfig,
    Config,
    Pricing,
    ReasoningConfig,
    load_config,
)
from lossrun.openai_client import DEFAULT_BASE_URL, OpenAIClient
from lossrun.router import ModelRouter
from lossrun.superapp_client import Attachment, TokenExpired
from lossrun.telemetry import Telemetry

LUNA = "openai/gpt-5.6-luna"
GEMINI = "gemini/gemini-3.6-flash"


def make_config(**overrides) -> Config:
    return Config(
        api=ApiConfig(
            "https://superapp.invalid/api/v1",
            0,
            30,
            5,
            1,
            overrides.pop("max_retries", 0),
        ),
        primary_model=LUNA,
        consensus_models=(LUNA, GEMINI),
        chunking=ChunkingConfig(50, 2, 5, 12, 20 * 1024 * 1024, 3),
        model_overrides=overrides.pop("model_overrides", {}),
        pricing={LUNA: Pricing(0.2, 0.8), GEMINI: Pricing(0.2, 0.8)},
        reasoning=ReasoningConfig(),
        token=overrides.pop("token", "superapp-token"),
        default_provider=overrides.pop("default_provider", "openai"),
        openai_api_key=overrides.pop("openai_api_key", "sk-test"),
        gemini_api_key=overrides.pop("gemini_api_key", "gem-test"),
        provider_max_request_bytes=overrides.pop("provider_max_request_bytes", {}),
        adjudicate=overrides.pop("adjudicate", True),
        adjudicator=overrides.pop("adjudicator", ""),
    )


def openai_with(handler, **config_overrides) -> OpenAIClient:
    config = make_config(**config_overrides)
    telemetry = Telemetry(run_id="r", document="d", pricing=config.pricing)
    client = OpenAIClient(config=config, telemetry=telemetry)
    # Keep the headers the client built, so the bearer header is genuinely tested.
    client._client = httpx.Client(
        base_url=DEFAULT_BASE_URL,
        headers=dict(client._client.headers),
        transport=httpx.MockTransport(handler),
    )
    return client


def completed_body(text: str = "ok") -> dict:
    return {
        "id": "resp_1",
        "status": "completed",
        "output": [{"content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 120, "output_tokens": 34},
    }


def capture(captured: dict):
    """A handler that records the create request and completes on the first poll."""

    def handler(request):
        if request.method == "POST":
            captured.update(json.loads(request.content))
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("Authorization")
            captured["idempotency"] = request.headers.get("Idempotency-Key")
            return httpx.Response(200, json={"id": "resp_1", "status": "queued"})
        return httpx.Response(200, json=completed_body("the table"))

    return handler


def test_a_successful_run_returns_text_and_usage():
    captured: dict = {}
    with openai_with(capture(captured)) as client:
        result = client.run(model=LUNA, prompt="extract")
    assert result.ok
    assert result.output_text == "the table"
    assert (result.input_tokens, result.output_tokens) == (120, 34)
    assert result.poll_count == 1


def test_the_request_is_bearer_authenticated_against_openai():
    captured: dict = {}
    with openai_with(capture(captured)) as client:
        client.run(model=LUNA, prompt="x")
    assert captured["url"] == f"{DEFAULT_BASE_URL}/responses"
    assert captured["auth"] == "Bearer sk-test"
    assert captured["idempotency"], "a retried create must not start a second billed run"


def test_the_catalog_prefix_is_stripped_from_the_wire_model():
    """OpenAI names the model `gpt-5.6-luna`; the `openai/` prefix is SuperApp's."""
    captured: dict = {}
    with openai_with(capture(captured)) as client:
        client.run(model=LUNA, prompt="x")
    assert captured["model"] == "gpt-5.6-luna"


def test_the_request_carries_prompt_instructions_effort_and_pdf():
    captured: dict = {}
    with openai_with(capture(captured)) as client:
        client.run(
            model=LUNA,
            prompt="extract the table",
            instructions="you extract loss runs",
            attachments=[Attachment(filename="p.pdf", data=b"%PDF-1.7 fake")],
            effort="max",
        )

    assert captured["instructions"] == "you extract loss runs"
    assert captured["reasoning"] == {"effort": "max"}, "max is accepted on gpt-5.6"
    assert captured["background"] is True

    content = captured["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "extract the table"}
    assert content[1]["type"] == "input_file"
    assert content[1]["filename"] == "p.pdf"
    assert content[1]["file_data"].startswith("data:application/pdf;base64,")


def test_no_temperature_is_sent_because_the_reasoning_models_reject_it():
    captured: dict = {}
    with openai_with(capture(captured)) as client:
        client.run(model=LUNA, prompt="x")
    assert "temperature" not in captured


def test_a_rejected_api_key_is_not_retried():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(
            401, json={"error": {"message": "Incorrect API key provided", "code": "invalid_api_key"}}
        )

    with openai_with(handler, max_retries=2) as client:
        with pytest.raises(TokenExpired, match="OPENAI_API_KEY"):
            client.run(model=LUNA, prompt="x")
    assert attempts["n"] == 1


def test_an_unsupported_region_is_treated_as_a_credential_failure():
    """403 is not fixed by trying again, so it must not burn the retry budget."""
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(403, json={"error": {"message": "Country not supported"}})

    with openai_with(handler, max_retries=2) as client:
        with pytest.raises(TokenExpired):
            client.run(model=LUNA, prompt="x")
    assert attempts["n"] == 1


def test_a_nested_vendor_error_message_reaches_the_operator():
    def handler(request):
        return httpx.Response(
            400, json={"error": {"message": "Unsupported value: 'effort'", "type": "invalid_request_error"}}
        )

    from lossrun.superapp_client import SuperAppError

    with openai_with(handler) as client:
        with pytest.raises(SuperAppError, match="Unsupported value"):
            client.run(model=LUNA, prompt="x")


def test_a_missing_api_key_fails_with_a_clear_message():
    config = make_config(openai_api_key="")
    telemetry = Telemetry(run_id="r", document="d", pricing={})
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIClient(config=config, telemetry=telemetry)


def test_telemetry_records_the_provider_that_served_the_call():
    config = make_config()
    telemetry = Telemetry(run_id="r", document="d", pricing=config.pricing)
    client = OpenAIClient(config=config, telemetry=telemetry)
    client._client = httpx.Client(
        base_url=DEFAULT_BASE_URL,
        headers=dict(client._client.headers),
        transport=httpx.MockTransport(capture({})),
    )
    with client:
        client.run(model=LUNA, prompt="x", stage="extract", label="chunk 1/1", effort="max")

    call = telemetry.calls[-1]
    assert call.model == LUNA
    assert call.provider == "openai"
    assert call.effort == "max"
    assert call.cost_total_usd > 0


# --- auto routing -----------------------------------------------------------


def test_auto_sends_each_pin_to_its_own_vendor():
    config = make_config(default_provider="auto")
    assert config.provider_for(LUNA) == "openai"
    assert config.provider_for(GEMINI) == "gemini"


def test_auto_falls_back_to_superapp_for_the_key_that_is_missing():
    config = make_config(default_provider="auto", openai_api_key="")
    assert config.provider_for(LUNA) == "superapp"
    assert config.provider_for(GEMINI) == "gemini", "the other route is untouched"


def test_auto_falls_back_for_a_pin_no_direct_client_serves():
    config = make_config(default_provider="auto")
    assert config.provider_for("anthropic/claude-opus-5") == "superapp"


def test_an_explicit_pin_without_its_key_fails_instead_of_falling_back():
    """A named provider is honored or it fails.

    Falling back silently would bill the run through the agent loop, at agent-loop
    token counts, while the config and the ledger both claim otherwise.
    """
    config = make_config(
        default_provider="auto",
        openai_api_key="",
        model_overrides={LUNA: {"provider": "openai"}},
    )
    with pytest.raises(ValueError, match="OPENAI_API_KEY is not set"):
        config.provider_for(LUNA)


def test_a_non_openai_model_cannot_use_the_openai_provider():
    config = make_config(model_overrides={GEMINI: {"provider": "openai"}})
    with pytest.raises(ValueError, match="prefix is not openai/"):
        config.provider_for(GEMINI)


def test_superapp_remains_reachable_for_a_pin_whose_key_is_present():
    config = make_config(default_provider="auto", model_overrides={LUNA: {"provider": "superapp"}})
    assert config.provider_for(LUNA) == "superapp"


def test_the_router_builds_one_client_per_provider():
    config = make_config(default_provider="auto")
    telemetry = Telemetry(run_id="r", document="d", pricing=config.pricing)
    with ModelRouter(config=config, telemetry=telemetry) as router:
        assert type(router._client_for(LUNA)).__name__ == "OpenAIClient"
        assert type(router._client_for(GEMINI)).__name__ == "GeminiClient"


def test_the_router_never_builds_a_client_it_does_not_need():
    """An OpenAI-only run must not require a Gemini key."""
    config = make_config(default_provider="auto", gemini_api_key="")
    telemetry = Telemetry(run_id="r", document="d", pricing=config.pricing)
    with ModelRouter(config=config, telemetry=telemetry) as router:
        router._client_for(LUNA)
        assert "gemini" not in router._clients


# --- per-provider request ceiling -------------------------------------------


def test_the_request_ceiling_follows_the_route_not_the_model():
    """The 1 MiB window is a SuperApp upload workaround, not a property of Luna.

    Keying it per model would leave a model that falls back to SuperApp uploading
    bodies that endpoint times out on.
    """
    ceilings = {"superapp": 1_048_576, "openai": 20_971_520}
    direct = make_config(default_provider="auto", provider_max_request_bytes=ceilings)
    fallback = make_config(
        default_provider="auto", openai_api_key="", provider_max_request_bytes=ceilings
    )
    assert direct.chunking_for(LUNA).max_request_bytes == 20_971_520
    assert fallback.chunking_for(LUNA).max_request_bytes == 1_048_576


def test_a_model_override_still_wins_over_its_providers_ceiling():
    config = make_config(
        default_provider="auto",
        provider_max_request_bytes={"openai": 20_971_520},
        model_overrides={LUNA: {"max_request_bytes": 4096}},
    )
    assert config.chunking_for(LUNA).max_request_bytes == 4096


# --- route class, which groups a run's output --------------------------------


def test_a_run_reaching_every_vendor_directly_is_classed_direct():
    config = make_config(default_provider="auto")
    assert config.route_class([LUNA, GEMINI]) == "direct"


def test_a_run_entirely_on_the_fallback_is_classed_superapp():
    config = make_config(default_provider="superapp")
    assert config.route_class([LUNA, GEMINI]) == "superapp"


def test_one_fallback_makes_the_whole_run_mixed():
    """A run is only comparable with runs measured the same way.

    One SuperApp call puts agent-loop tokens into the totals, so the run belongs
    with neither population — not filed under whichever route dominated.
    """
    config = make_config(default_provider="auto", openai_api_key="")
    assert config.route_class([LUNA, GEMINI]) == "mixed"


def test_the_adjudicator_counts_toward_the_route_class():
    """It bills real calls, and it can be a pin the extracting models are not."""
    config = make_config(
        default_provider="auto",
        model_overrides={LUNA: {"provider": "superapp"}},
        adjudicator=LUNA,
    )
    assert config.route_class([GEMINI]) == "mixed", "adjudication is on by default"

    off = make_config(
        default_provider="auto",
        model_overrides={LUNA: {"provider": "superapp"}},
        adjudicator=LUNA,
        adjudicate=False,
    )
    assert off.route_class([GEMINI]) == "direct"


# --- which credentials a run actually needs ---------------------------------

CONFIG_YAML = f"""
api:
  base_url: https://superapp.invalid/api/v1
models:
  primary: {LUNA}
  consensus: [{LUNA}, {GEMINI}]
providers:
  default: auto
"""


def write_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML)
    return path


def set_env(monkeypatch, **values: str) -> None:
    """Pin the credential environment.

    Every name is set, empty ones included, because `load_dotenv` fills in only
    what is absent — leaving one unset would let the developer's own `.env`
    decide the outcome of the test.
    """
    for name in ("SUPERAPP_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(name, values.get(name, ""))


def test_a_fully_direct_run_needs_no_superapp_token(tmp_path, monkeypatch):
    set_env(monkeypatch, OPENAI_API_KEY="sk-test", GEMINI_API_KEY="gem-test")
    config = load_config(write_config(tmp_path))
    assert config.token == ""
    assert [config.provider_for(m) for m in config.routed_models] == ["openai", "gemini"]


def test_a_token_is_required_as_soon_as_one_model_falls_back(tmp_path, monkeypatch):
    set_env(monkeypatch, GEMINI_API_KEY="gem-test")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        load_config(write_config(tmp_path))


def test_a_token_covers_the_models_that_fall_back(tmp_path, monkeypatch):
    set_env(monkeypatch, SUPERAPP_TOKEN="jwt", GEMINI_API_KEY="gem-test")
    config = load_config(write_config(tmp_path))
    assert config.provider_for(LUNA) == "superapp"
    assert config.provider_for(GEMINI) == "gemini"
