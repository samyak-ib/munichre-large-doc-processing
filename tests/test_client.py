"""SuperApp client: request limits, polling, and transient-failure handling."""

from __future__ import annotations

import httpx
import pytest

from lossrun.config import ApiConfig, ChunkingConfig, Config, Pricing, ReasoningConfig
from lossrun.superapp_client import (
    Attachment,
    LimitExceeded,
    SuperAppClient,
    SuperAppError,
    TokenExpired,
)
from lossrun.telemetry import Telemetry


def make_config(**api_overrides) -> Config:
    api = ApiConfig(
        base_url="https://example.invalid/api/v1",
        poll_interval_s=0,
        poll_deadline_s=api_overrides.pop("poll_deadline_s", 30),
        request_timeout_s=5,
        max_concurrent_calls=1,
        max_retries=api_overrides.pop("max_retries", 0),
    )
    return Config(
        api=api,
        primary_model="m",
        consensus_models=("m",),
        chunking=ChunkingConfig(50, 2, 5, 12, 20 * 1024 * 1024, 3),
        model_overrides={},
        pricing={"m": Pricing(0.2, 0.8)},
        reasoning=ReasoningConfig(),
        token="t",
    )


def client_with(handler, **config_overrides) -> SuperAppClient:
    config = make_config(**config_overrides)
    telemetry = Telemetry(run_id="r", document="d", pricing=config.pricing)
    client = SuperAppClient(config=config, telemetry=telemetry)
    # Keep the headers the client built, so the bearer header is genuinely tested.
    client._client = httpx.Client(
        base_url=config.api.base_url,
        headers=dict(client._client.headers),
        transport=httpx.MockTransport(handler),
    )
    return client


def completed_body(text: str = "ok") -> dict:
    return {
        "id": "resp_1",
        "status": "completed",
        "output": [{"content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }


def test_create_then_poll_returns_the_output():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": "resp_1", "status": "queued"})
        return httpx.Response(200, json=completed_body("hello"))

    with client_with(handler) as client:
        result = client.run(model="m", prompt="hi")
    assert result.ok
    assert result.output_text == "hello"
    assert result.input_tokens == 10


def test_a_dropped_connection_mid_poll_keeps_polling():
    """A transient disconnect must not discard a run already executing.

    Abandoning it re-uploads the whole document and starts a second billed run,
    for a failure that says nothing about the run's state.
    """
    state = {"polls": 0}

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": "resp_1", "status": "queued"})
        state["polls"] += 1
        if state["polls"] <= 2:
            raise httpx.ReadError("Server disconnected without sending a response.")
        return httpx.Response(200, json=completed_body())

    with client_with(handler) as client:
        result = client.run(model="m", prompt="hi")
    assert result.ok
    assert state["polls"] == 3


def test_a_dropped_connection_past_the_deadline_gives_up():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": "resp_1", "status": "queued"})
        raise httpx.ReadError("Server disconnected without sending a response.")

    with client_with(handler, poll_deadline_s=-1) as client:
        with pytest.raises(SuperAppError, match="poll failed"):
            client.run(model="m", prompt="hi")


def test_a_503_while_polling_is_retried():
    state = {"polls": 0}

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": "resp_1", "status": "queued"})
        state["polls"] += 1
        if state["polls"] == 1:
            return httpx.Response(503, json={"detail": "temporarily unavailable"})
        return httpx.Response(200, json=completed_body())

    with client_with(handler) as client:
        assert client.run(model="m", prompt="hi").ok


def test_an_expired_token_is_not_retried():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(401, json={"detail": "Signature has expired"})

    with client_with(handler, max_retries=2) as client:
        with pytest.raises(TokenExpired):
            client.run(model="m", prompt="hi")
    assert attempts["n"] == 1, "an expired token cannot be fixed by retrying"


def test_every_create_carries_an_idempotency_key_and_the_bearer_token():
    seen = {}

    def handler(request):
        if request.method == "POST":
            seen["auth"] = request.headers.get("Authorization")
            seen["key"] = request.headers.get("Idempotency-Key")
            return httpx.Response(200, json={"id": "resp_1", "status": "queued"})
        return httpx.Response(200, json=completed_body())

    with client_with(handler) as client:
        client.run(model="m", prompt="hi")
    assert seen["key"]
    assert seen["auth"] == "Bearer t"


def test_background_is_always_requested_and_streaming_never_is():
    body = {}

    def handler(request):
        if request.method == "POST":
            import json as _json

            body.update(_json.loads(request.content))
            return httpx.Response(200, json={"id": "resp_1", "status": "queued"})
        return httpx.Response(200, json=completed_body())

    with client_with(handler) as client:
        client.run(model="m", prompt="hi")
    assert body["background"] is True
    assert "stream" not in body


def test_oversized_text_is_rejected_before_sending():
    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("request should not have been sent")

    with client_with(handler) as client:
        with pytest.raises(LimitExceeded, match="byte API limit"):
            client.run(model="m", prompt="x" * (65 * 1024))


def test_too_many_attachments_are_rejected_before_sending():
    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("request should not have been sent")

    attachments = [Attachment(filename=f"{i}.pdf", data=b"x") for i in range(21)]
    with client_with(handler) as client:
        with pytest.raises(LimitExceeded, match="attachments"):
            client.run(model="m", prompt="hi", attachments=attachments)


def test_a_failed_run_is_reported_with_its_error_code():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": "resp_1", "status": "queued"})
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "status": "failed",
                "output": [],
                "error": {"code": "workflow_failed", "message": "the agent run failed"},
            },
        )

    with client_with(handler) as client:
        result = client.run(model="m", prompt="hi")
    assert not result.ok
    assert result.error_code == "workflow_failed"


def _capture_body(handler_state):
    def handler(request):
        if request.method == "POST":
            import json as _json

            handler_state.update(_json.loads(request.content))
            return httpx.Response(200, json={"id": "resp_1", "status": "queued"})
        return httpx.Response(200, json=completed_body())

    return handler


def test_reasoning_effort_is_sent_when_configured():
    body = {}
    with client_with(_capture_body(body)) as client:
        client.run(model="m", prompt="hi", effort="low")
    assert body["reasoning"] == {"effort": "low"}


def test_no_reasoning_block_is_sent_without_an_effort():
    """Omitting the field lets the model use its own default.

    Sending an explicit null or empty effort would be a 400.
    """
    body = {}
    with client_with(_capture_body(body)) as client:
        client.run(model="m", prompt="hi", effort=None)
    assert "reasoning" not in body


def test_the_effort_and_route_used_are_recorded_on_the_call():
    body = {}
    client = client_with(_capture_body(body))
    with client:
        client.run(model="m", prompt="hi", effort="low", stage="extract")
    call = client.telemetry.calls[-1]
    assert call.effort == "low"
    assert call.provider == "superapp"


def test_the_catalog_pin_reaches_superapp_unchanged():
    """SuperApp's catalog is keyed by the prefixed pin, unlike the vendor APIs."""
    body = {}
    with client_with(_capture_body(body)) as client:
        client.run(model="openai/gpt-5.6-luna", prompt="hi")
    assert body["model"] == "openai/gpt-5.6-luna"
