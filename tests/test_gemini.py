"""Direct Gemini client and per-model provider routing.

The client has to present the same surface as the SuperApp one — same `run`
signature, same `RunResult`, same telemetry — or the router cannot swap them
without the pipeline noticing.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from lossrun.config import ApiConfig, ChunkingConfig, Config, Pricing, ReasoningConfig
from lossrun.gemini_client import GeminiClient
from lossrun.router import ModelRouter
from lossrun.superapp_client import Attachment, LimitExceeded, SuperAppError, TokenExpired
from lossrun.telemetry import Telemetry

GEMINI = "gemini/gemini-3.6-flash"
LUNA = "openai/gpt-5.6-luna"


def make_config(**overrides) -> Config:
    return Config(
        api=ApiConfig("https://superapp.invalid/api/v1", 0, 30, 5, 1, overrides.pop("max_retries", 0)),
        primary_model=LUNA,
        consensus_models=(LUNA, GEMINI),
        chunking=ChunkingConfig(50, 2, 5, 12, 20 * 1024 * 1024, 3),
        model_overrides=overrides.pop("model_overrides", {}),
        pricing={GEMINI: Pricing(0.2, 0.8), LUNA: Pricing(0.2, 0.8)},
        reasoning=ReasoningConfig(),
        token="superapp-token",
        default_provider=overrides.pop("default_provider", "superapp"),
        gemini_api_key=overrides.pop("gemini_api_key", "test-key"),
        thinking_budgets=overrides.pop("thinking_budgets", {}),
        gemini_temperature=overrides.pop("gemini_temperature", 0.0),
    )


def gemini_with(handler, **config_overrides) -> GeminiClient:
    config = make_config(**config_overrides)
    telemetry = Telemetry(run_id="r", document="d", pricing=config.pricing)
    client = GeminiClient(config=config, telemetry=telemetry)
    # Keep the headers the client built, so the auth header is genuinely tested.
    client._client = httpx.Client(
        base_url="https://gemini.invalid/v1beta",
        headers=dict(client._client.headers),
        transport=httpx.MockTransport(handler),
    )
    return client


def ok_body(text: str = "hello", finish: str = "STOP") -> dict:
    return {
        "responseId": "resp-1",
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}],
        "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 34},
    }


def test_a_successful_generate_returns_text_and_usage():
    with gemini_with(lambda r: httpx.Response(200, json=ok_body("the table"))) as client:
        result = client.run(model=GEMINI, prompt="extract")
    assert result.ok
    assert result.output_text == "the table"
    assert (result.input_tokens, result.output_tokens) == (120, 34)
    assert result.poll_count == 0, "Gemini is synchronous; there is nothing to poll"


def test_the_request_carries_prompt_instructions_and_pdf():
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        captured["url"] = str(request.url)
        captured["key"] = request.headers.get("x-goog-api-key")
        return httpx.Response(200, json=ok_body())

    with gemini_with(handler) as client:
        client.run(
            model=GEMINI,
            prompt="extract the table",
            instructions="you extract loss runs",
            attachments=[Attachment(filename="p.pdf", data=b"%PDF-1.7 fake")],
        )

    assert "models/gemini-3.6-flash:generateContent" in captured["url"]
    assert captured["key"] == "test-key"
    assert captured["systemInstruction"]["parts"][0]["text"] == "you extract loss runs"

    parts = captured["contents"][0]["parts"]
    assert parts[0]["text"] == "extract the table"
    inline = parts[1]["inline_data"]
    assert inline["mime_type"] == "application/pdf"
    assert base64.b64decode(inline["data"]) == b"%PDF-1.7 fake"


def test_temperature_is_sent_because_superapp_cannot_set_it():
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=ok_body())

    with gemini_with(handler) as client:
        client.run(model=GEMINI, prompt="x")
    assert captured["generationConfig"]["temperature"] == 0.0


def test_a_thinking_budget_is_sent_only_when_configured():
    captured = {}

    def handler(request):
        captured.clear()
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=ok_body())

    with gemini_with(handler) as client:
        client.run(model=GEMINI, prompt="x", effort="low")
    assert "thinkingConfig" not in captured["generationConfig"]

    with gemini_with(handler, thinking_budgets={"low": 0}) as client:
        client.run(model=GEMINI, prompt="x", effort="low")
    assert captured["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


def test_a_truncated_answer_is_reported_as_incomplete():
    """MAX_TOKENS is a cut-off answer, not a clean stop.

    Reporting it as completed would let a half table through as if it were whole.
    """
    body = ok_body("partial", finish="MAX_TOKENS")
    with gemini_with(lambda r: httpx.Response(200, json=body)) as client:
        result = client.run(model=GEMINI, prompt="x")
    assert not result.ok
    assert result.status == "incomplete"
    assert result.error_code == "MAX_TOKENS"


def test_a_rejected_api_key_is_not_retried():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(403, json={"error": {"message": "API key not valid", "status": "PERMISSION_DENIED"}})

    with gemini_with(handler, max_retries=2) as client:
        with pytest.raises(TokenExpired, match="API key"):
            client.run(model=GEMINI, prompt="x")
    assert attempts["n"] == 1


def test_an_api_error_surfaces_its_message():
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "bad request", "status": "INVALID_ARGUMENT"}})

    with gemini_with(handler) as client:
        with pytest.raises(SuperAppError, match="bad request"):
            client.run(model=GEMINI, prompt="x")


def test_oversized_inline_attachments_are_rejected_before_sending():
    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("request should not have been sent")

    big = Attachment(filename="big.pdf", data=b"x" * (21 * 1024 * 1024))
    with gemini_with(handler) as client:
        with pytest.raises(LimitExceeded, match="inline limit"):
            client.run(model=GEMINI, prompt="x", attachments=[big])


def test_a_missing_api_key_fails_with_a_clear_message():
    config = make_config(gemini_api_key="")
    telemetry = Telemetry(run_id="r", document="d", pricing={})
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiClient(config=config, telemetry=telemetry)


def test_telemetry_records_the_call_like_the_superapp_client_does():
    config = make_config()
    telemetry = Telemetry(run_id="r", document="d", pricing=config.pricing)
    client = GeminiClient(config=config, telemetry=telemetry)
    client._client = httpx.Client(
        base_url="https://gemini.invalid/v1beta",
        headers=dict(client._client.headers),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=ok_body())),
    )
    with client:
        client.run(model=GEMINI, prompt="x", stage="extract", label="chunk 1/1", effort="low")

    call = telemetry.calls[-1]
    assert call.model == GEMINI
    assert call.provider == "gemini"
    assert call.stage == "extract"
    assert call.effort == "low"
    assert call.input_tokens == 120
    assert call.cost_total_usd > 0


# --- routing ---------------------------------------------------------------


def test_naming_superapp_as_the_default_keeps_every_model_on_it():
    config = make_config()
    assert config.provider_for(LUNA) == "superapp"
    assert config.provider_for(GEMINI) == "superapp"


def test_a_model_can_be_routed_to_gemini_individually():
    config = make_config(model_overrides={GEMINI: {"provider": "gemini"}})
    assert config.provider_for(GEMINI) == "gemini"
    assert config.provider_for(LUNA) == "superapp", "the other model is untouched"


def test_a_non_gemini_model_cannot_use_the_gemini_provider():
    config = make_config(model_overrides={LUNA: {"provider": "gemini"}})
    with pytest.raises(ValueError, match="prefix is not gemini/"):
        config.provider_for(LUNA)


def test_an_unknown_provider_is_rejected():
    config = make_config(model_overrides={GEMINI: {"provider": "bedrock"}})
    with pytest.raises(ValueError, match="unknown provider"):
        config.provider_for(GEMINI)


def test_the_api_model_defaults_to_the_pin_without_its_prefix():
    config = make_config()
    assert config.api_model_for(GEMINI) == "gemini-3.6-flash"
    assert config.api_model_for("gemini/x") == "x"


def test_the_api_model_can_be_overridden_when_the_vendor_name_differs():
    config = make_config(model_overrides={GEMINI: {"api_model": "gemini-2.5-flash-002"}})
    assert config.api_model_for(GEMINI) == "gemini-2.5-flash-002"


def test_the_router_builds_a_client_per_provider_and_reuses_it():
    config = make_config(model_overrides={GEMINI: {"provider": "gemini"}})
    telemetry = Telemetry(run_id="r", document="d", pricing=config.pricing)
    with ModelRouter(config=config, telemetry=telemetry) as router:
        assert router.provider_for(GEMINI) == "gemini"
        first = router._client_for(GEMINI)
        assert router._client_for(GEMINI) is first, "clients are cached, not rebuilt"
        assert type(router._client_for(LUNA)).__name__ == "SuperAppClient"
        assert type(first).__name__ == "GeminiClient"


def test_the_router_never_builds_a_client_it_does_not_need():
    """A SuperApp-only run must not require a Gemini key, and vice versa."""
    config = make_config(gemini_api_key="")
    telemetry = Telemetry(run_id="r", document="d", pricing=config.pricing)
    with ModelRouter(config=config, telemetry=telemetry) as router:
        router._client_for(LUNA)
        assert "gemini" not in router._clients
