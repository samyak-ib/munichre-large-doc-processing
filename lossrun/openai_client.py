"""Direct client for OpenAI's Responses API.

An alternative to routing the OpenAI pins through SuperApp. Two reasons to want
it:

- **The model that answers is the model that was asked.** Every SuperApp pin
  declares a fallback taken when the provider is unwired on the serving fleet,
  and the `model` echoed back is the pin that was requested either way — so a
  substitution is invisible, and cost is computed from the wrong price.
- **No agent overhead.** SuperApp runs a full agent loop, so its token counts
  include context loading and tool scaffolding a raw call does not pay for.

SuperApp's Responses API is modeled on this one: same request body, same
attachment part, same output and usage shape, same background-plus-poll cycle,
and `Idempotency-Key` is honored by both. The create, poll, retry and telemetry
paths are therefore inherited rather than restated — only the transport differs.
"""

from __future__ import annotations

from typing import ClassVar

import httpx

from .superapp_client import SuperAppClient

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIClient(SuperAppClient):
    PROVIDER: ClassVar[str] = "openai"

    AUTH_HINT: ClassVar[str] = (
        "OpenAI rejected the API key. Check OPENAI_API_KEY in .env, or route the "
        "model to superapp."
    )

    # 403 covers an unsupported region and a key without access to the model.
    # Neither is fixed by trying again.
    AUTH_STATUSES: ClassVar[frozenset[int]] = frozenset({401, 403})

    def __post_init__(self) -> None:
        if not self.config.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY is not set, but a model is routed to the openai "
                "provider. Add it to .env or route that model to superapp."
            )
        self._client = httpx.Client(
            base_url=self.config.openai_base_url or DEFAULT_BASE_URL,
            headers={
                "Authorization": f"Bearer {self.config.openai_api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.config.api.request_timeout_s,
        )

    def _api_model(self, model: str) -> str:
        """Strip the catalog prefix: OpenAI names the model `gpt-5.6-luna`."""
        return self.config.api_model_for(model)
