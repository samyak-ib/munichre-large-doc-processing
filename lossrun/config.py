"""Configuration loading: config.yaml plus the bearer token from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field as dataclass_field, replace
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    poll_interval_s: float
    poll_deadline_s: float
    request_timeout_s: float
    max_concurrent_calls: int
    max_retries: int
    # How many documents a batch (`lossrun extract a.pdf b.pdf ...`) runs at
    # once. Independent of `max_concurrent_calls`, which caps concurrent calls
    # *within* one document's QA stage.
    max_concurrent_documents: int = 3


@dataclass(frozen=True)
class ChunkingConfig:
    max_pages_per_chunk: int
    overlap_pages: int
    header_pages: int
    overlap_anchor_rows: int
    max_request_bytes: int
    max_resumes: int


@dataclass(frozen=True)
class Pricing:
    """USD per 1M tokens."""

    input: float
    output: float


@dataclass(frozen=True)
class ReasoningConfig:
    """Default reasoning effort per pipeline stage.

    `extract` defaults to None — no preference sent — because measured runs
    showed effort is not what drives extraction quality on these documents (see
    docs/CHALLENGES.md #1). Per-model overrides in `models.overrides.<model>.effort`
    take precedence, which is how a single pin runs at `max` while the rest do
    not.

    `low`, `medium` and `high` are accepted by every selectable model; `xhigh`
    and `max` are OpenAI-only and the API rejects them elsewhere with a 400.
    """

    layout: str | None = "medium"
    extract: str | None = None


@dataclass(frozen=True)
class Config:
    api: ApiConfig
    primary_model: str
    chunking: ChunkingConfig
    model_overrides: dict[str, dict[str, Any]]
    pricing: dict[str, Pricing]
    reasoning: ReasoningConfig
    token: str
    # Direct-provider settings. Empty when every model routes through SuperApp.
    default_provider: str = "auto"
    openai_api_key: str = ""
    openai_base_url: str = ""
    gemini_api_key: str = ""
    gemini_base_url: str = ""
    gemini_temperature: float = 0.0
    # Per-provider request-body ceiling, applied to whichever models route there.
    provider_max_request_bytes: dict[str, int] = dataclass_field(default_factory=dict)
    thinking_budgets: dict[str, int] = dataclass_field(default_factory=dict)
    # A --effort flag for this run: outranks per-model overrides and the
    # config default. None means no flag was given; "" means "send nothing".
    forced_extract_effort: str | None = None
    # Review the extracted table against the source pages instead of extracting
    # it a second time with another model. One call per chunk.
    qa_enabled: bool = True
    # Which model reviews. Empty means the primary, which is the shipped setup:
    # the reviewer is checking a transcription against the page rather than
    # arbitrating between two readings, so it is not judging its own tie-break.
    qa_reviewer: str = ""

    @property
    def qa_model(self) -> str:
        return self.qa_reviewer or self.primary_model

    @property
    def routed_models(self) -> tuple[str, ...]:
        """Every model a run may call, deduplicated in configuration order.

        The reviewer is included because it bills real calls even when it is not
        the extracting model.
        """
        pins = [self.primary_model]
        if self.qa_enabled:
            pins.append(self.qa_model)
        return tuple(dict.fromkeys(pins))

    def route_class(self, models: list[str]) -> str:
        """Whether this run's calls all go direct, all through SuperApp, or both.

        Names the measurement rather than the destination. A run with even one
        SuperApp call carries agent-loop tokens in its totals and may have been
        served by a substituted model, so it is not comparable with an all-direct
        run — `mixed` keeps it out of both populations instead of filing it under
        whichever route happened to dominate.

        The reviewer counts whenever QA is on: it bills real calls, and it can be
        a different pin from the extracting model.
        """
        pins = list(models)
        if self.qa_enabled:
            pins.append(self.qa_model)
        providers = {self.provider_for(m) for m in pins}
        if providers == {"superapp"}:
            return "superapp"
        if "superapp" not in providers:
            return "direct"
        return "mixed"

    def chunking_for(self, model: str) -> ChunkingConfig:
        """Chunking settings for one model, most specific setting winning.

        Three layers: the `chunking` block, then the request-size ceiling of the
        provider this model routes to, then the model's own overrides. The
        provider layer exists because the body-size ceiling belongs to the
        transport, not the model — the tight SuperApp window is a workaround for
        that endpoint's upload timeouts, and the same model called directly has
        no such limit. Keying it per model instead would leave a model that falls
        back to SuperApp uploading bodies that endpoint cannot take.
        """
        override = self.model_overrides.get(model, {})
        allowed = {f.name for f in ChunkingConfig.__dataclass_fields__.values()}
        unknown = set(override) - allowed - {"effort", "layout_effort", "provider", "api_model"}
        if unknown:
            raise ValueError(f"unknown override keys for {model}: {sorted(unknown)}")

        chunking = self.chunking
        provider_bytes = self.provider_max_request_bytes.get(self.provider_for(model))
        if provider_bytes:
            chunking = replace(chunking, max_request_bytes=provider_bytes)

        chunk_override = {k: v for k, v in override.items() if k in allowed}
        return replace(chunking, **chunk_override)

    def extract_effort_for(self, model: str) -> str | None:
        """Extraction effort for one model.

        A per-model `effort` override exists because the levels are not uniform:
        `xhigh` and `max` are OpenAI-only, and asking for one on a Gemini pin is
        a hard 400. Setting Luna to `max` therefore cannot be a global setting.

        A `--effort` flag on the command line outranks both, so a single run can
        be forced onto one level across every model.
        """
        if self.forced_extract_effort is not None:
            return self.forced_extract_effort or None
        override = self.model_overrides.get(model, {}).get("effort")
        if override is not None:
            return _effort(override)
        return self.reasoning.extract

    def layout_effort_for(self, model: str) -> str | None:
        """Layout-discovery effort for one model.

        Separate from `extract_effort_for` rather than shared with it: the two
        stages have different global defaults (`reasoning.layout` vs
        `reasoning.extract`), and a model with no override should keep its own
        stage's default rather than inheriting the other stage's. The same
        per-model `effort` override is not reused here for the same reason
        `extract_effort_for` documents — `max`/`xhigh` are OpenAI-only, so
        raising layout effort for one model cannot be a global setting.
        """
        override = self.model_overrides.get(model, {}).get("layout_effort")
        if override is not None:
            return _effort(override)
        return self.reasoning.layout

    def provider_for(self, model: str) -> str:
        """Which client reaches this model: "superapp", "openai" or "gemini".

        Routing is per model so one run can compare a SuperApp-hosted pin
        against the same family called directly.

        `auto` prefers the model's own vendor whenever that vendor's key is set,
        and falls back to SuperApp when it is not. A provider named outright —
        in `providers.default` or a per-model override — is honored or fails:
        falling back from an explicit choice would let a run bill through the
        agent loop while the config and the ledger claim otherwise.
        """
        override = self.model_overrides.get(model, {}).get("provider")
        requested = str(override or self.default_provider).strip().lower()
        if requested not in VALID_PROVIDERS and requested != AUTO_PROVIDER:
            raise ValueError(
                f"unknown provider {requested!r} for {model}; expected one of "
                f"{', '.join((AUTO_PROVIDER, *VALID_PROVIDERS))}"
            )

        if requested == AUTO_PROVIDER:
            vendor = self._vendor_provider(model)
            return vendor if vendor and self.api_key_for(vendor) else "superapp"

        if requested != "superapp":
            if self._vendor_provider(model) != requested:
                raise ValueError(
                    f"{model} cannot use the {requested} provider — its catalog "
                    f"prefix is not {requested}/"
                )
            if not self.api_key_for(requested):
                raise ValueError(
                    f"{model} is routed to the {requested} provider but "
                    f"{ENV_KEYS[requested]} is not set. Add it to .env, or drop "
                    f"the provider override to fall back to superapp."
                )
        return requested

    def api_key_for(self, provider: str) -> str:
        """The direct provider's key, or empty — including for "superapp",
        which authenticates with a bearer token rather than a key."""
        return {
            "openai": self.openai_api_key,
            "gemini": self.gemini_api_key,
        }.get(provider, "")

    @staticmethod
    def _vendor_provider(model: str) -> str:
        """The direct provider that owns this pin, from its catalog prefix.

        Empty for a pin whose vendor has no direct client here, which `auto`
        reads as "SuperApp is the only way to reach it".
        """
        prefix = model.split("/", 1)[0].strip().lower()
        return prefix if prefix in DIRECT_PROVIDERS else ""

    def api_model_for(self, model: str) -> str:
        """The id the direct provider expects, e.g. gemini-3.6-flash.

        Defaults to the pin without its provider prefix; override with
        `api_model` when the vendor's name differs from the catalog's.
        """
        override = self.model_overrides.get(model, {}).get("api_model")
        if override:
            return str(override)
        return model.split("/", 1)[-1]

    def thinking_budget_for(self, effort: str | None) -> int | None:
        """Gemini's thinking budget for a reasoning effort, if configured.

        Returns None when unmapped, which leaves the model's own default alone
        rather than guessing a budget.
        """
        if not effort:
            return None
        return self.thinking_budgets.get(effort)

    def price(self, model: str) -> Pricing:
        """Configured price for a model, or zero when the operator has not set one."""
        return self.pricing.get(model, Pricing(input=0.0, output=0.0))


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
VALID_PROVIDERS = ("superapp", "openai", "gemini")

# Providers reached by calling the vendor directly with an API key. Their names
# match the catalog prefixes of the pins they serve, which is what lets `auto`
# route `openai/gpt-5.6-luna` without a per-model entry.
DIRECT_PROVIDERS = ("openai", "gemini")

# Route each model to its own vendor where the key allows, SuperApp where it does
# not. Not a provider itself — a rule for choosing one.
AUTO_PROVIDER = "auto"

ENV_KEYS = {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}


def _effort(value: object) -> str | None:
    """Validate a reasoning effort locally so a typo fails before any API call."""
    if value in (None, "", "default"):
        return None
    effort = str(value).strip().lower()
    if effort not in VALID_EFFORTS:
        raise ValueError(
            f"reasoning effort {value!r} is not one of {', '.join(VALID_EFFORTS)}"
        )
    return effort


def load_config(
    path: Path | None = None,
    *,
    primary_model: str | None = None,
    base_url: str | None = None,
    extract_effort: str | None = None,
    qa: bool | None = None,
    qa_model: str | None = None,
    provider: str | None = None,
    require_token: bool = True,
) -> Config:
    """Load config.yaml, then apply environment and explicit overrides."""
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(DEFAULT_CONFIG_PATH.parent / ".env")

    config_path = path or DEFAULT_CONFIG_PATH
    raw = yaml.safe_load(config_path.read_text()) or {}

    api_raw = raw.get("api", {})
    resolved_base = base_url or os.environ.get("SUPERAPP_BASE_URL") or api_raw["base_url"]
    api = ApiConfig(
        base_url=resolved_base.rstrip("/"),
        poll_interval_s=float(api_raw.get("poll_interval_s", 3)),
        poll_deadline_s=float(api_raw.get("poll_deadline_s", 900)),
        request_timeout_s=float(api_raw.get("request_timeout_s", 120)),
        max_concurrent_calls=int(api_raw.get("max_concurrent_calls", 4)),
        max_retries=int(api_raw.get("max_retries", 2)),
        max_concurrent_documents=int(api_raw.get("max_concurrent_documents", 3)),
    )

    chunk_raw = raw.get("chunking", {})
    chunking = ChunkingConfig(
        max_pages_per_chunk=int(chunk_raw.get("max_pages_per_chunk", 50)),
        overlap_pages=int(chunk_raw.get("overlap_pages", 2)),
        header_pages=int(chunk_raw.get("header_pages", 5)),
        overlap_anchor_rows=int(chunk_raw.get("overlap_anchor_rows", 12)),
        max_request_bytes=int(chunk_raw.get("max_request_bytes", 20 * 1024 * 1024)),
        max_resumes=int(chunk_raw.get("max_resumes", 3)),
    )

    models_raw = raw.get("models", {})
    primary = primary_model or models_raw.get("primary")
    if not primary:
        raise ValueError("models.primary is required")

    qa_raw = raw.get("qa") or {}
    qa_on = bool(qa_raw.get("enabled", True)) if qa is None else qa
    reviewer = (qa_model if qa_model is not None else qa_raw.get("model", "")) or ""

    pricing = {
        name: Pricing(input=float(v.get("input", 0.0)), output=float(v.get("output", 0.0)))
        for name, v in (raw.get("pricing") or {}).items()
    }

    reasoning_raw = raw.get("reasoning") or {}
    reasoning = ReasoningConfig(
        layout=_effort(reasoning_raw.get("layout", "medium")),
        extract=_effort(reasoning_raw.get("extract", "default")),
    )
    forced = None if extract_effort is None else (_effort(extract_effort) or "")

    providers_raw = raw.get("providers") or {}
    openai_raw = providers_raw.get("openai") or {}
    gemini_raw = providers_raw.get("gemini") or {}
    default_provider = (
        provider or str(providers_raw.get("default", AUTO_PROVIDER))
    ).strip().lower()
    if default_provider not in VALID_PROVIDERS and default_provider != AUTO_PROVIDER:
        raise ValueError(
            "providers.default must be one of "
            f"{', '.join((AUTO_PROVIDER, *VALID_PROVIDERS))}"
        )
    thinking_budgets = {
        str(k).lower(): int(v) for k, v in (gemini_raw.get("thinking_budgets") or {}).items()
    }
    provider_max_request_bytes = {
        name: int((providers_raw.get(name) or {})["max_request_bytes"])
        for name in VALID_PROVIDERS
        if (providers_raw.get(name) or {}).get("max_request_bytes")
    }

    # A forced provider outranks the per-model overrides too, or `--provider
    # superapp` could not put a pinned model back on the fallback route.
    overrides = models_raw.get("overrides") or {}
    if provider:
        overrides = {m: {k: v for k, v in o.items() if k != "provider"} for m, o in overrides.items()}

    config = Config(
        api=api,
        primary_model=primary,
        chunking=chunking,
        model_overrides=overrides,
        pricing=pricing,
        reasoning=reasoning,
        token=os.environ.get("SUPERAPP_TOKEN", "").strip(),
        forced_extract_effort=forced,
        default_provider=default_provider,
        openai_api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
        openai_base_url=str(openai_raw.get("base_url", "") or ""),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        gemini_base_url=str(gemini_raw.get("base_url", "") or ""),
        gemini_temperature=float(gemini_raw.get("temperature", 0.0)),
        provider_max_request_bytes=provider_max_request_bytes,
        thinking_budgets=thinking_budgets,
        qa_enabled=qa_on,
        qa_reviewer=str(reviewer).strip(),
    )

    # Resolving the routes is what says whether a bearer token is needed at all:
    # a run whose every model reaches its vendor directly needs no SuperApp
    # session. This must follow construction — under `auto`, only the resolved
    # route reveals which models fall back.
    if (
        require_token
        and not config.token
        and any(config.provider_for(m) == "superapp" for m in config.routed_models)
    ):
        raise ValueError(
            "SUPERAPP_TOKEN is not set, and at least one model falls back to the "
            "superapp provider. Set OPENAI_API_KEY / GEMINI_API_KEY to reach every "
            "model directly, or paste a bearer token from a logged-in SuperApp "
            "session into .env."
        )
    return config
