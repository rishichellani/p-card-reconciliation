"""Reusable free-tier LLM client with resilient multi-provider routing.

Talks to any OpenAI-compatible chat-completions endpoint (Gemini via AI Studio, Groq, OpenRouter). Providers are
tried in priority order; a provider that fails is skipped for that call, and repeated failures open a circuit breaker
(temporary for rate limits/outages, permanent for bad credentials). If every provider fails, ``complete`` raises
``LLMResponseError`` so the caller can degrade gracefully instead of crashing the run.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping

from utils.errors import LLMAuthError, LLMResponseError, PipelineError

log = logging.getLogger(__name__)

# name -> (api-key env var, base URL, model env var, default model, min seconds between calls)
# Free-tier model names change often: override with the *_MODEL variables if a default is retired.
PROVIDERS: dict[str, tuple[str, str, str, str, float]] = {
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/",
               "GEMINI_MODEL", "gemini-3.8-flash", 5.0),
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1",
             "GROQ_MODEL", "openai/gpt-oss-120b", 15.0),  # free tier: 8K tokens/min, ~1.9K per audit => ~4 audits/min
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1",
                   "OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free", 3.5),
}
DEFAULT_ORDER = "gemini,groq,openrouter"

FAILURES_BEFORE_COOLDOWN = 3
COOLDOWN_SECONDS = 60.0
MAX_WAIT_SECONDS = 30.0


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    api_key: str = field(repr=False)  # never log keys
    model: str
    min_interval: float


@dataclass(frozen=True)
class LLMReply:
    text: str
    provider: str
    model: str


def _describe_delay(seconds: float | None) -> str:
    if seconds is None:
        return ""
    return f", retry in about {seconds / 60:.0f} min" if seconds >= 90 else f", retry in about {seconds:.0f}s"


class ProviderError(Exception):
    def __init__(self, message: str, *, permanent: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.permanent = permanent
        self.retry_after = retry_after


def load_providers(env: Mapping[str, str] | None = None, order: str | None = None) -> list[ProviderConfig]:
    """Providers that have an API key configured, in priority order (LLM_PROVIDERS or --providers to reorder)."""
    env = os.environ if env is None else env
    names = [n.strip().lower() for n in (order or env.get("LLM_PROVIDERS") or DEFAULT_ORDER).split(",") if n.strip()]
    unknown = [n for n in names if n not in PROVIDERS]
    if unknown:
        raise PipelineError(f"Unknown LLM provider(s) {unknown}; choose from {sorted(PROVIDERS)}")
    override = env.get("LLM_MIN_INTERVAL_SECONDS")
    configs: list[ProviderConfig] = []
    for name in names:
        key_var, base_url, model_var, default_model, interval = PROVIDERS[name]
        key = (env.get(key_var) or "").strip()
        if not key:
            log.debug("Provider %s skipped: %s not set", name, key_var)
            continue
        try:
            interval = float(override) if override is not None else interval
        except ValueError as exc:
            raise PipelineError(f"LLM_MIN_INTERVAL_SECONDS must be a number, got {override!r}") from exc
        configs.append(ProviderConfig(name, base_url, key, (env.get(model_var) or default_model).strip(), interval))
    if not configs:
        wanted = ", ".join(PROVIDERS[n][0] for n in names)
        raise LLMAuthError(f"No LLM API key found. Set at least one of: {wanted}")
    return configs


def probe(providers: list[ProviderConfig], timeout: float = 30.0) -> list[tuple[str, str, bool, str]]:
    """One tiny request per provider, so a person can see which connection works. Returns (name, model, ok, detail)."""
    out = []
    for p in providers:
        client = LLMClient([p], timeout=timeout, max_tokens=500, sleep=lambda s: None)
        try:
            reply = client.complete("Reply with JSON only.", 'Return exactly {"ok": true}.')
            out.append((p.name, p.model, True, "responded"))
        except LLMResponseError as exc:
            out.append((p.name, p.model, False, str(exc).removeprefix("all LLM providers failed: ")))
    return out


@dataclass
class _State:
    dead: bool = False
    fails: int = 0
    cooldown_until: float = 0.0
    last_call: float = float("-inf")
    json_mode: bool = True


class LLMClient:
    def __init__(
        self,
        providers: list[ProviderConfig],
        *,
        timeout: float = 60.0,
        max_tokens: int = 2000,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not providers:
            raise LLMAuthError("LLMClient needs at least one provider")
        self.providers = providers
        self.timeout, self.max_tokens = timeout, max_tokens
        self._sleep, self._clock = sleep, clock
        self._state = {p.name: _State() for p in providers}
        self._sdk_clients: dict[str, object] = {}
        self._last_error: dict[str, str] = {}  # why each provider last failed

    @property
    def provider_names(self) -> list[str]:
        return [p.name for p in self.providers]

    # ------------------------------------------------------------------ routing
    def complete(self, system: str, user: str, avoid: frozenset[str] = frozenset()) -> LLMReply:
        """Return the first successful reply. Providers named in ``avoid`` are tried last."""
        ordered = sorted(self.providers, key=lambda p: p.name in avoid)  # stable: keeps priority order
        errors: list[str] = []
        for round_no in (1, 2):
            for p in ordered:
                st = self._state[p.name]
                if st.dead or self._clock() < st.cooldown_until:
                    continue
                self._throttle(p, st)
                try:
                    text = self._request(p, st, system, user)
                except ProviderError as exc:
                    errors.append(f"{p.name}: {exc}")
                    self._record_failure(p, st, exc)
                    continue
                st.fails = 0
                return LLMReply(text=text, provider=p.name, model=p.model)
            wait = self._soonest_cooldown(ordered)
            if round_no == 1 and wait is not None and wait <= MAX_WAIT_SECONDS:
                log.warning("All providers cooling down; waiting %.0fs", wait)
                self._sleep(wait)
                continue
            break
        if errors:
            detail = "; ".join(errors)
        else:  # every provider was already switched off: say why, not just "none available"
            why = "; ".join(f"{name}: {msg}" for name, msg in self._last_error.items())
            detail = f"no provider is available ({why})" if why else "no provider is available"
        raise LLMResponseError(f"all LLM providers failed: {detail}")

    def _soonest_cooldown(self, providers: list[ProviderConfig]) -> float | None:
        now = self._clock()
        waits = [self._state[p.name].cooldown_until - now for p in providers
                 if not self._state[p.name].dead and self._state[p.name].cooldown_until > now]
        return min(waits) if waits else None

    def _throttle(self, p: ProviderConfig, st: _State) -> None:
        gap = st.last_call + p.min_interval - self._clock()
        if gap > 0:
            self._sleep(gap)
        st.last_call = self._clock()

    def _record_failure(self, p: ProviderConfig, st: _State, exc: ProviderError) -> None:
        self._last_error[p.name] = str(exc)
        if exc.permanent:
            st.dead = True
            log.error("Provider %s disabled for this run: %s", p.name, exc)
            return
        st.fails += 1
        if exc.retry_after is not None:
            st.cooldown_until = self._clock() + min(exc.retry_after, COOLDOWN_SECONDS)
        elif st.fails >= FAILURES_BEFORE_COOLDOWN:
            st.cooldown_until = self._clock() + COOLDOWN_SECONDS
            st.fails = 0
            log.warning("Provider %s cooling down for %.0fs after repeated failures", p.name, COOLDOWN_SECONDS)
        log.warning("Provider %s failed (%s); trying next", p.name, exc)

    # ---------------------------------------------------------------- transport
    def _sdk(self, p: ProviderConfig):
        if p.name not in self._sdk_clients:
            import openai  # lazy: --mock-llm runs without the SDK or any key

            self._sdk_clients[p.name] = openai.OpenAI(
                api_key=p.api_key, base_url=p.base_url, timeout=self.timeout, max_retries=1
            )
        return self._sdk_clients[p.name]

    def _request(self, p: ProviderConfig, st: _State, system: str, user: str) -> str:
        import openai

        kwargs = dict(
            model=p.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0,
            max_tokens=self.max_tokens,
        )
        for _ in range(2):  # second pass only if the endpoint rejects JSON mode
            try:
                extra = {"response_format": {"type": "json_object"}} if st.json_mode else {}
                resp = self._sdk(p).chat.completions.create(**kwargs, **extra)
                break
            except openai.BadRequestError as exc:
                msg = str(exc).lower()
                if st.json_mode and any(k in msg for k in ("response_format", "json_object", "json mode")):
                    log.warning("%s/%s rejected JSON mode; continuing without it", p.name, p.model)
                    st.json_mode = False
                    continue
                raise ProviderError(f"bad request: {exc}") from exc
            except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
                raise ProviderError(f"credentials rejected ({exc.status_code})", permanent=True) from exc
            except openai.NotFoundError as exc:
                raise ProviderError(f"model '{p.model}' not found - set {PROVIDERS[p.name][2]}", permanent=True) from exc
            except openai.RateLimitError as exc:
                retry = exc.response.headers.get("retry-after") if exc.response is not None else None
                try:
                    delay = float(retry) if retry else None
                except ValueError:
                    delay = None
                text = str(exc).lower()
                if any(k in text for k in ("(tpd)", "(rpd)", "per day", "perday")):
                    kind = "daily limit"
                elif any(k in text for k in ("(tpm)", "(rpm)", "per minute", "perminute")):
                    kind = "per-minute limit"
                else:
                    kind = "rate limit"
                # A daily quota (or a very long wait) will not clear during this run: stop using the provider for it.
                if kind == "daily limit" or (delay is not None and delay > 300):
                    raise ProviderError(f"{kind} reached{_describe_delay(delay)}", permanent=True) from exc
                raise ProviderError(f"rate limited ({kind}){_describe_delay(delay)}",
                                    retry_after=delay if delay is not None else 30.0) from exc
            except openai.APIConnectionError as exc:  # includes timeouts
                raise ProviderError(f"network error: {exc}") from exc
            except openai.APIStatusError as exc:
                raise ProviderError(f"API error {exc.status_code}") from exc
        else:
            raise ProviderError("JSON-mode negotiation failed")

        if not resp.choices:
            raise ProviderError("empty response (no choices)")
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise ProviderError("response truncated at max_tokens")
        text = choice.message.content
        if not text or not text.strip():
            raise ProviderError("empty response (no content)")
        return text
