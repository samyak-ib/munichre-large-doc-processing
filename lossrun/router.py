"""Dispatches each model's calls to the client configured for it.

The pipeline holds one object and never learns which provider served a call.
Clients are built lazily, so a run needs only the credentials for the routes it
actually takes — no `OPENAI_API_KEY` for a run that touches no OpenAI pin, no
bearer token for one that never falls back to SuperApp.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .gemini_client import GeminiClient
from .openai_client import OpenAIClient
from .superapp_client import Attachment, RunResult, SuperAppClient
from .telemetry import Telemetry


@dataclass
class ModelRouter:
    config: Config
    telemetry: Telemetry
    _clients: dict[str, object] = field(default_factory=dict, repr=False)

    def __enter__(self) -> ModelRouter:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    def provider_for(self, model: str) -> str:
        return self.config.provider_for(model)

    def run(
        self,
        *,
        model: str,
        prompt: str,
        instructions: str = "",
        attachments: list[Attachment] | None = None,
        stage: str = "extract",
        label: str = "",
        pages: str = "",
        effort: str | None = None,
    ) -> RunResult:
        return self._client_for(model).run(
            model=model,
            prompt=prompt,
            instructions=instructions,
            attachments=attachments,
            stage=stage,
            label=label,
            pages=pages,
            effort=effort,
        )

    def _client_for(self, model: str):
        provider = self.config.provider_for(model)
        if provider not in self._clients:
            self._clients[provider] = self._build(provider)
        return self._clients[provider]

    def _build(self, provider: str):
        if provider == "gemini":
            return GeminiClient(config=self.config, telemetry=self.telemetry)
        if provider == "openai":
            return OpenAIClient(config=self.config, telemetry=self.telemetry)
        return SuperAppClient(config=self.config, telemetry=self.telemetry)
