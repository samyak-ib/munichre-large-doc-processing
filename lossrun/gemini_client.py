"""Direct client for Google's Gemini API.

An alternative to routing through SuperApp. Two reasons to want it:

- **Temperature.** Gemini accepts `temperature: 0`, which SuperApp's Responses
  subset does not expose. Deterministic decoding is what the loss-run work
  actually wants from an extraction pass.
- **No agent overhead.** SuperApp runs a full agent loop, so its token counts
  include context loading and tool scaffolding that a raw call does not.

The interface deliberately mirrors `SuperAppClient.run`, so the router can swap
one for the other without the pipeline knowing which it holds.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import ClassVar

import httpx

from .config import Config
from .superapp_client import (
    MAX_ATTACHMENTS,
    Attachment,
    LimitExceeded,
    RunResult,
    SuperAppError,
    TokenExpired,
)
from .telemetry import Telemetry

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Inline attachment ceiling for generateContent. Larger payloads need the Files
# API, which this client does not use.
MAX_INLINE_BYTES = 20 * 1024 * 1024

# Gemini has no 64 KiB text cap like the Responses API, but keeping the same
# budget means a prompt that fits one provider fits the other.
MAX_TEXT_BYTES = 64 * 1024


@dataclass
class GeminiClient:
    config: Config
    telemetry: Telemetry
    _client: httpx.Client = field(init=False, repr=False)

    PROVIDER: ClassVar[str] = "gemini"

    def __post_init__(self) -> None:
        if not self.config.gemini_api_key:
            raise ValueError(
                "GEMINI_API_KEY is not set, but a model is routed to the gemini "
                "provider. Add it to .env or route that model to superapp."
            )
        self._client = httpx.Client(
            base_url=self.config.gemini_base_url or DEFAULT_BASE_URL,
            headers={
                "x-goog-api-key": self.config.gemini_api_key,
                "Content-Type": "application/json",
            },
            timeout=self.config.api.request_timeout_s,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GeminiClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

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
        """Generate content and record telemetry, matching the SuperApp client.

        Gemini is synchronous — there is no create/poll cycle — so `poll_count`
        is always zero and latency is the single round trip.
        """
        attachments = attachments or []
        self._check_limits(prompt, instructions, attachments)

        api_model = self.config.api_model_for(model)
        body = self._build_body(prompt, instructions, attachments, effort)

        last_error: SuperAppError | None = None
        for attempt in range(1, self.config.api.max_retries + 2):
            started = time.monotonic()
            try:
                result = self._generate(api_model, body, time.monotonic() - started)
                result.attempts = attempt
                self._record(stage, model, label, pages, attempt, effort, result)
                return result
            except TokenExpired:
                raise
            except SuperAppError as exc:
                last_error = exc
                self.telemetry.record(
                    stage=stage,
                    model=model,
                    provider=self.PROVIDER,
                    label=label,
                    pages=pages,
                    attempt=attempt,
                    effort=effort or "",
                    response_id="",
                    status="error",
                    latency_s=time.monotonic() - started,
                    poll_count=0,
                    input_tokens=0,
                    output_tokens=0,
                    error_code=exc.code or str(exc.status),
                    error_message=str(exc),
                )
                if attempt > self.config.api.max_retries:
                    break
                time.sleep(min(2**attempt, 15))

        raise last_error or SuperAppError("gemini call failed with no diagnostic")

    def _build_body(
        self,
        prompt: str,
        instructions: str,
        attachments: list[Attachment],
        effort: str | None,
    ) -> dict:
        parts: list[dict] = [{"text": prompt}]
        for attachment in attachments:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": attachment.mime,
                        "data": base64.b64encode(attachment.data).decode("ascii"),
                    }
                }
            )
        body: dict = {
            "contents": [{"role": "user", "parts": parts}],
            # Temperature 0 is the point of calling Gemini directly: the
            # extraction pass wants transcription, not sampling.
            "generationConfig": {"temperature": self.config.gemini_temperature},
        }
        if instructions:
            body["systemInstruction"] = {"parts": [{"text": instructions}]}
        budget = self.config.thinking_budget_for(effort)
        if budget is not None:
            body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": budget}
        return body

    def _generate(self, api_model: str, body: dict, _elapsed: float) -> RunResult:
        started = time.monotonic()
        try:
            response = self._client.post(f"/models/{api_model}:generateContent", json=body)
        except httpx.HTTPError as exc:
            raise SuperAppError(f"gemini request failed: {exc}") from exc

        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "5"))
            time.sleep(min(retry_after, 60))
            raise SuperAppError("rate limited", status=429, code="RATE_LIMITED")
        self._raise_for_status(response)

        payload = response.json()
        return self._to_result(payload, time.monotonic() - started)

    @staticmethod
    def _to_result(payload: dict, latency: float) -> RunResult:
        candidates = payload.get("candidates") or []
        text_parts: list[str] = []
        finish_reason = ""
        for candidate in candidates[:1]:
            finish_reason = candidate.get("finishReason", "")
            for part in (candidate.get("content") or {}).get("parts") or []:
                if isinstance(part.get("text"), str):
                    text_parts.append(part["text"])

        usage = payload.get("usageMetadata") or {}
        # MAX_TOKENS means the answer was cut off; the caller's truncation
        # handling needs to see that as a non-success, not a clean stop.
        failed = finish_reason not in ("", "STOP")
        return RunResult(
            response_id=payload.get("responseId", ""),
            status="incomplete" if failed else "completed",
            output_text="".join(text_parts),
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0),
            error_code=finish_reason if failed else "",
            error_message=f"finishReason={finish_reason}" if failed else "",
            poll_count=0,
            latency_s=latency,
        )

    def _check_limits(
        self, prompt: str, instructions: str, attachments: list[Attachment]
    ) -> None:
        text_bytes = len(prompt.encode()) + len(instructions.encode())
        if text_bytes > MAX_TEXT_BYTES:
            raise LimitExceeded(
                f"prompt + instructions is {text_bytes} bytes, over the "
                f"{MAX_TEXT_BYTES} byte budget"
            )
        if len(attachments) > MAX_ATTACHMENTS:
            raise LimitExceeded(
                f"{len(attachments)} attachments, over the {MAX_ATTACHMENTS} limit"
            )
        encoded = sum(len(a.data) for a in attachments) * 4 // 3
        if encoded > MAX_INLINE_BYTES:
            raise LimitExceeded(
                f"inline attachments are ~{encoded} bytes, over the "
                f"{MAX_INLINE_BYTES} byte inline limit for generateContent"
            )

    def _record(
        self,
        stage: str,
        model: str,
        label: str,
        pages: str,
        attempt: int,
        effort: str | None,
        result: RunResult,
    ) -> None:
        self.telemetry.record(
            stage=stage,
            model=model,
            provider=self.PROVIDER,
            label=label,
            pages=pages,
            attempt=attempt,
            effort=effort or "",
            response_id=result.response_id,
            status=result.status,
            latency_s=result.latency_s,
            poll_count=result.poll_count,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            error_code=result.error_code,
            error_message=result.error_message,
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            error = (response.json() or {}).get("error") or {}
        except ValueError:
            error = {}
        message = error.get("message") or response.text[:300]
        status = error.get("status", "")
        if response.status_code in (401, 403):
            raise TokenExpired(
                f"Gemini rejected the API key ({response.status_code}): {message}",
                status=response.status_code,
                code=status,
            )
        raise SuperAppError(
            f"gemini HTTP {response.status_code}: {message}",
            status=response.status_code,
            code=status,
        )
