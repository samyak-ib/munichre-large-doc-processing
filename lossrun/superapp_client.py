"""Thin client for the SuperApp Responses API.

The API is background-only: every call is create -> poll -> read. Request limits
(64 KiB of text, 25 MiB of body, 20 attachments) are asserted before sending so a
budget mistake fails locally instead of costing a round trip.
"""

from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass, field
from typing import ClassVar

import httpx

from .config import Config
from .telemetry import Telemetry

# API limits. See docs/api-server/01.004-responses-api-integration-guide.md.
MAX_TEXT_BYTES = 64 * 1024
MAX_BODY_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENTS = 20

TERMINAL_STATUSES = frozenset({"completed", "failed", "incomplete", "cancelled"})
PENDING_STATUSES = frozenset({"queued", "in_progress"})


class SuperAppError(Exception):
    def __init__(self, message: str, *, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class TokenExpired(SuperAppError):
    """The bearer token is missing, expired, or unresolvable."""


class LimitExceeded(SuperAppError):
    """A request exceeds an API limit and was not sent."""


@dataclass
class Attachment:
    filename: str
    data: bytes
    mime: str = "application/pdf"

    def to_part(self) -> dict[str, str]:
        encoded = base64.b64encode(self.data).decode("ascii")
        return {
            "type": "input_file",
            "filename": self.filename,
            "file_data": f"data:{self.mime};base64,{encoded}",
        }


@dataclass
class RunResult:
    response_id: str
    status: str
    output_text: str
    input_tokens: int
    output_tokens: int
    error_code: str = ""
    error_message: str = ""
    poll_count: int = 0
    latency_s: float = 0.0
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.status == "completed"


@dataclass
class SuperAppClient:
    config: Config
    telemetry: Telemetry
    _client: httpx.Client = field(init=False, repr=False)

    # Recorded on every call so the ledger shows which route served it. A run may
    # mix providers, and the same model costs different amounts on each.
    PROVIDER: ClassVar[str] = "superapp"

    # What a rejected credential means to the operator, and what to do about it.
    # Each transport authenticates differently, so the remedy differs too.
    AUTH_HINT: ClassVar[str] = (
        "SuperApp rejected the token (401). Paste a fresh SUPERAPP_TOKEN into "
        ".env from a logged-in session."
    )

    # Statuses that mean the credential itself is bad. These are raised as
    # TokenExpired, which `run` never retries — retrying a rejected credential
    # only spends the rate limit.
    AUTH_STATUSES: ClassVar[frozenset[int]] = frozenset({401})

    def __post_init__(self) -> None:
        self._client = httpx.Client(
            base_url=self.config.api.base_url,
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Content-Type": "application/json",
            },
            timeout=self.config.api.request_timeout_s,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SuperAppClient:
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
        """Create a response, poll it to a terminal status, and record telemetry.

        Retries transient failures (503, network) up to api.max_retries. Each
        attempt carries its own Idempotency-Key so a retried create that actually
        landed returns the original run instead of starting a second billed one.
        """
        attachments = attachments or []
        self._check_limits(prompt, instructions, attachments)

        body = {
            "model": self._api_model(model),
            "background": True,
            "input": self._build_input(prompt, attachments),
        }
        if instructions:
            body["instructions"] = instructions
        if effort:
            body["reasoning"] = {"effort": effort}

        last_error: SuperAppError | None = None
        for attempt in range(1, self.config.api.max_retries + 2):
            started = time.monotonic()
            idempotency_key = str(uuid.uuid4())
            try:
                created = self._create(body, idempotency_key)
                result = self._poll(created["id"], started)
                result.attempts = attempt
                self._record(stage, model, label, pages, attempt, effort, result)
                if result.status == "failed" and attempt <= self.config.api.max_retries:
                    last_error = SuperAppError(
                        result.error_message or "run failed", code=result.error_code
                    )
                    continue
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

        raise last_error or SuperAppError("run failed with no diagnostic")

    def _api_model(self, model: str) -> str:
        """The model id this transport expects for a catalog pin.

        SuperApp's catalog is keyed by the prefixed pin itself, so the pin goes
        out unchanged.
        """
        return model

    def _build_input(self, prompt: str, attachments: list[Attachment]) -> object:
        if not attachments:
            return prompt
        content: list[dict[str, str]] = [{"type": "input_text", "text": prompt}]
        content.extend(a.to_part() for a in attachments)
        return [{"role": "user", "content": content}]

    def _check_limits(
        self, prompt: str, instructions: str, attachments: list[Attachment]
    ) -> None:
        text_bytes = len(prompt.encode()) + len(instructions.encode())
        if text_bytes > MAX_TEXT_BYTES:
            raise LimitExceeded(
                f"prompt + instructions is {text_bytes} bytes, over the "
                f"{MAX_TEXT_BYTES} byte API limit"
            )
        if len(attachments) > MAX_ATTACHMENTS:
            raise LimitExceeded(
                f"{len(attachments)} attachments, over the {MAX_ATTACHMENTS} limit"
            )
        # base64 inflates by 4/3; compare against the real body cap.
        encoded = sum(len(a.data) for a in attachments) * 4 // 3
        if encoded + text_bytes > MAX_BODY_BYTES:
            raise LimitExceeded(
                f"request body is ~{encoded + text_bytes} bytes, over the "
                f"{MAX_BODY_BYTES} byte API limit"
            )

    def _create(self, body: dict, idempotency_key: str) -> dict:
        try:
            resp = self._client.post(
                "/responses", json=body, headers={"Idempotency-Key": idempotency_key}
            )
        except httpx.HTTPError as exc:
            raise SuperAppError(f"create failed: {exc}") from exc
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "5"))
            time.sleep(min(retry_after, 60))
            raise SuperAppError("rate limited", status=429, code="RATE_LIMITED")
        self._raise_for_status(resp)
        return resp.json()

    def _poll(self, response_id: str, started: float) -> RunResult:
        deadline = started + self.config.api.poll_deadline_s
        polls = 0
        while True:
            time.sleep(self.config.api.poll_interval_s)
            polls += 1
            try:
                resp = self._client.get(f"/responses/{response_id}")
            except httpx.HTTPError as exc:
                # A dropped connection says nothing about the run, which is
                # executing server-side. Abandoning it here would discard work
                # already paid for and re-upload the whole document to start
                # again; GET is idempotent, so keep polling until the deadline.
                if time.monotonic() > deadline:
                    raise SuperAppError(f"poll failed: {exc}") from exc
                continue
            if resp.status_code == 503:
                # Retryable by contract; keep polling until the deadline.
                if time.monotonic() > deadline:
                    raise SuperAppError("poll deadline exceeded during 503", status=503)
                continue
            self._raise_for_status(resp)
            payload = resp.json()
            status = payload.get("status", "")
            if status in TERMINAL_STATUSES:
                return self._to_result(payload, polls, time.monotonic() - started)
            if status not in PENDING_STATUSES:
                raise SuperAppError(f"unknown response status {status!r}")
            if time.monotonic() > deadline:
                raise SuperAppError(
                    f"run {response_id} still {status} after "
                    f"{self.config.api.poll_deadline_s}s"
                )

    @staticmethod
    def _to_result(payload: dict, polls: int, latency: float) -> RunResult:
        text_parts: list[str] = []
        for item in payload.get("output") or []:
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    text_parts.append(part.get("text", ""))
        error = payload.get("error") or {}
        incomplete = payload.get("incomplete_details") or {}
        usage = payload.get("usage") or {}
        return RunResult(
            response_id=payload.get("id", ""),
            status=payload.get("status", ""),
            output_text="".join(text_parts),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            error_code=error.get("code", "") or incomplete.get("reason", ""),
            error_message=error.get("message", ""),
            poll_count=polls,
            latency_s=latency,
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

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        # SuperApp reports the failure at the top level; the vendor APIs nest it
        # under `error`. Unwrap either so the message reaches the operator.
        error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
        detail = error.get("detail") or error.get("message") or resp.text[:300]
        code = error.get("code", "") or error.get("type", "")
        if resp.status_code in self.AUTH_STATUSES:
            raise TokenExpired(self.AUTH_HINT, status=resp.status_code, code=code)
        raise SuperAppError(f"HTTP {resp.status_code}: {detail}", status=resp.status_code, code=code)
