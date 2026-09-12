"""LLM providers.

Contract: TECH-SPEC.md section 3 — `stream(messages) -> AsyncIterator[str]`
yielding sentence-sized chunks, plus `route(text) -> "stall"|"answer"`.
`stream()` is an async generator; the server wraps every other (sync)
provider call in `asyncio.to_thread`.
"""
from __future__ import annotations

import abc
import asyncio
import os
from typing import Any, AsyncIterator, Optional

from ._shared import (
    LITELLM_DEFAULT_BASE_URL,
    REASONING_TOKEN_FLOOR,
    ProviderError,
    accepts_reasoning_effort,
    finalize_think,
    is_reasoning_model,
    logger,
    redacted_repr,
    split_sentences,
    strip_closed_think_tags,
)

# The LiteLLM proxy is the house default LLM route. Its address comes from
# LITELLM_BASE_URL (or the generic LLM_BASE_URL); the literal fallback lives in
# `_shared.py` so this module and `server/settings.py` share one definition and
# no fleet host is baked into the tree.
DEFAULT_LITELLM_BASE_URL = (
    os.environ.get("LITELLM_BASE_URL")
    or os.environ.get("LLM_BASE_URL")
    or LITELLM_DEFAULT_BASE_URL
)

#: The house default model on the proxy. Measured 2026-09-12 (N=3, serial,
#: this machine): first-content-delta p50 617ms / p95 649ms — inside the 800ms
#: `llm_ms` budget. `gemini-3.7-flash` was the intended default and is
#: rejected on evidence: the proxy's Gemini key answers HTTP 429 "exceeded
#: your current quota" on every call. `claude-sonnet-5`/`claude-fable-5` are
#: HTTP 500 (Bedrock: model not enabled for the account), and
#: `claude-sonnet-4-6` streams but at 1673ms p50 — over budget.
DEFAULT_LITELLM_MODEL = "gpt-oss-120b-groq"

#: Groq's own fastest verified plain-chat model (2026-09-12, N=3: 450ms p50 /
#: 532ms p95 first content delta with reasoning_effort=low). Groq no longer
#: serves a llama chat model; `qwen/qwen3.6-27b` streams reasoning only at any
#: sane token ceiling, and `groq/compound-mini` — the previous default — is
#: agentic and cannot be relied on to emit `content` at all.
DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"


class LLMProvider(abc.ABC):
    @abc.abstractmethod
    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """Yield sentence/token chunks. Raises ProviderError on failure."""
        raise NotImplementedError
        yield  # pragma: no cover — keeps this an async generator

    @abc.abstractmethod
    def route(self, text: str) -> str:
        """Return 'stall' (research/worker path) or 'answer' (direct path)."""
        raise NotImplementedError


RESEARCH_TRIGGERS = (
    "research",
    "look up",
    "search",
    "find out",
    "investigate",
    "deep dive",
    "what is the weather",
    "weather",
    "what is going on",
    "what's going on",
    "how are things",
    "status report",
    "check on",
    "explain",
    "analyze",
)


def route_text(text: str) -> str:
    lowered = text.lower()
    for trigger in RESEARCH_TRIGGERS:
        if trigger in lowered:
            return "stall"
    return "answer"


class StubLLM(LLMProvider):
    """Streams canned sentences with a small delay so queueing is exercised."""

    def __init__(self, sentences: Optional[list[str]] = None) -> None:
        self.sentences = sentences or [
            "Got it, working on that now.",
            "I found three relevant points for you.",
            "Here is the summary of what matters most.",
        ]

    def route(self, text: str) -> str:
        return route_text(text)

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        if not messages:
            raise ProviderError("llm_empty_messages", "no messages to complete")
        for sentence in self.sentences:
            await asyncio.sleep(0.05)
            yield sentence


DEFAULT_MAX_TOKENS = 120


async def _sentence_stream(
    deltas: AsyncIterator[str], stats: Optional[dict] = None
) -> AsyncIterator[str]:
    """Buffer raw text deltas, strip `<think>` reasoning spans, and yield
    complete sentence-sized chunks via `split_sentences()`.

    This is the single splitting code path shared by every branch of
    `OpenAICompatibleLLM.stream()` — previously each branch duplicated its
    own ~70-line copy of this loop (and the no-httpx copy referenced an
    undefined `is_reasoning` name, raising NameError on every call).

    Fail-closed (law 1): a stream that completes having produced no spoken
    text raises ``llm_no_content`` instead of returning quietly. A silent
    empty generator is how a reasoning-only model looked like a working
    provider for a whole day — the worker path emitted zero sentences and
    nothing in the stack said why. ``stats`` (filled by the caller's delta
    pump) carries the reasoning-delta count into that error message.
    """
    buf = ""
    spoke = False
    async for delta in deltas:
        if not delta:
            continue
        buf += delta
        stripped = strip_closed_think_tags(buf)
        if stripped is None:
            continue  # mid <think>...</think>, wait for the closing tag
        buf = stripped
        sentences, buf = split_sentences(buf)
        for sentence in sentences:
            spoke = True
            yield sentence
    tail = finalize_think(buf)
    if tail:
        spoke = True
        yield tail
    if not spoke:
        reasoning = (stats or {}).get("reasoning_deltas", 0)
        model = (stats or {}).get("model", "?")
        raise ProviderError(
            "llm_no_content",
            f"model {model} streamed no content deltas"
            + (
                f" ({reasoning} reasoning deltas — raise the token ceiling or "
                "pick a plain chat model)"
                if reasoning
                else " (empty completion)"
            ),
        )


class OpenAICompatibleLLM(LLMProvider):
    """Real backend: any OpenAI-compatible chat-completions endpoint.

    Streams via httpx SSE when httpx is installed; falls back to the
    `openai` SDK's own streaming client otherwise. Both paths route through
    `_sentence_stream()` so sentence-splitting and `<think>` handling live
    in exactly one place.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_LITELLM_BASE_URL,
        model: str = DEFAULT_LITELLM_MODEL,
        api_key: str = "",
        max_tokens: Optional[int] = None,
        timeout_s: float = 45.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        # No hardcoded secret fallback here: an empty key means the caller
        # (make_llm, or whoever constructed this directly) didn't have one
        # to give us — that's a deliberate choice for self-hosted endpoints
        # that don't require auth, not something this class should paper
        # over with a shared master key.
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        # Spoken brevity comes from the system prompt/persona, not from
        # truncating the model mid-sentence — this is a safety ceiling on
        # runaway generations, not the mechanism for "keep it short".
        self.max_tokens = max_tokens or int(os.environ.get("LLM_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)))
        self.timeout_s = timeout_s

    def __repr__(self) -> str:
        return redacted_repr(self, secret_attrs=("api_key",))

    def route(self, text: str) -> str:
        return route_text(text)

    def _build_payload(self, messages: Any, system_prompt: str = "") -> dict[str, Any]:
        """Format request payload with model-specific parameters.
        Reasoning models (gpt-5, o1, o3) use max_completion_tokens and omit temperature.
        Standard models use max_tokens and temperature.
        """
        formatted_messages = []
        if system_prompt:
            formatted_messages.append({"role": "system", "content": system_prompt})
        if isinstance(messages, list):
            for m in messages:
                if isinstance(m, tuple) and len(m) == 2:
                    formatted_messages.append({"role": m[0], "content": m[1]})
                elif isinstance(m, dict):
                    formatted_messages.append(m)
        elif isinstance(messages, str):
            formatted_messages.append({"role": "user", "content": messages})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": formatted_messages,
            "stream": True,
        }
        if is_reasoning_model(self.model):
            # A reasoning model spends the ceiling on `reasoning` deltas
            # before it writes a word of `content`. At the ordinary 120-token
            # ceiling that means `finish_reason: "length"` with nothing
            # spoken (measured 2026-09-12 on gpt-oss-20b, qwen3.6-27b and
            # compound-mini), so the ceiling is floored well above it.
            ceiling = max(self.max_tokens, REASONING_TOKEN_FLOOR)
            if any(x in self.model for x in ("gpt-5", "o1", "o3")):
                payload["max_completion_tokens"] = ceiling
            else:
                payload["max_tokens"] = ceiling
                payload["temperature"] = 0.7
            if accepts_reasoning_effort(self.model):
                # Shortest path to content on the gpt-oss family: ~15
                # reasoning deltas instead of ~80, 450ms vs 617ms to first
                # content delta (measured, Groq, N=3).
                payload["reasoning_effort"] = "low"
        else:
            payload["max_tokens"] = self.max_tokens
            payload["temperature"] = 0.7
        return payload

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        import json as _json

        try:
            import httpx
        except ImportError:
            httpx = None

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = self._build_payload(messages)
        # Filled by whichever delta pump runs; read by _sentence_stream when a
        # stream ends having spoken nothing, so the named error says why.
        stats: dict[str, Any] = {"model": self.model, "reasoning_deltas": 0}

        if httpx is not None:

            async def _deltas() -> AsyncIterator[str]:
                timeout = httpx.Timeout(connect=3.0, read=self.timeout_s, write=10.0, pool=10.0)
                try:
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        async with client.stream("POST", url, headers=headers, json=payload) as resp:
                            if resp.status_code != 200:
                                err_body = await resp.aread()
                                raise ProviderError(
                                    "llm_stream_failed",
                                    f"HTTP {resp.status_code}: {err_body.decode(errors='replace')}",
                                )
                            async for line in resp.aiter_lines():
                                if not line or not line.startswith("data: "):
                                    continue
                                data_str = line[6:].strip()
                                if data_str == "[DONE]":
                                    break
                                try:
                                    chunk = _json.loads(data_str)
                                    delta = chunk["choices"][0]["delta"]
                                    if delta.get("reasoning") or delta.get(
                                        "reasoning_content"
                                    ):
                                        stats["reasoning_deltas"] += 1
                                    yield delta.get("content") or ""
                                except Exception:
                                    continue
                except ProviderError:
                    raise
                except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                    raise ProviderError("provider_unreachable", f"{url}: {e}")
                except Exception as e:
                    raise ProviderError("llm_stream_failed", str(e))

            async for sentence in _sentence_stream(_deltas(), stats):
                yield sentence
        else:
            logger.debug("OpenAICompatibleLLM.stream: httpx unavailable, using openai SDK")

            async def _deltas() -> AsyncIterator[str]:
                try:
                    from openai import AsyncOpenAI  # type: ignore
                except ImportError as e:
                    raise ProviderError("llm_openai_not_installed", str(e))

                try:
                    client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key or "x")
                    resp = await client.chat.completions.create(**payload)
                    async for chunk in resp:
                        choice = chunk.choices[0] if chunk.choices else None
                        delta = getattr(choice, "delta", None)
                        if delta is not None and (
                            getattr(delta, "reasoning", None)
                            or getattr(delta, "reasoning_content", None)
                        ):
                            stats["reasoning_deltas"] += 1
                        yield (getattr(delta, "content", None) or "") if delta else ""
                except ProviderError:
                    raise
                except (ConnectionError, OSError) as e:
                    raise ProviderError("provider_unreachable", f"{self.base_url}: {e}")
                except Exception as e:
                    raise ProviderError("llm_stream_failed", str(e))

            async for sentence in _sentence_stream(_deltas(), stats):
                yield sentence


def _require_key(key: Optional[str], env_var: str) -> str:
    if not key:
        raise ProviderError("missing_api_key", env_var)
    return key


def make_llm(
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> LLMProvider:
    """Tyre switch: LLM_PROVIDER=litellm|haiku|opencode|zen|gemini|groq|openai|fleet|stub.

    Default is ``litellm`` — the LiteLLM proxy is the house LLM route
    (Saurabh, 2026-09-12), and its address comes from ``LITELLM_BASE_URL``.

    Fail-closed: unknown provider names and missing required keys raise
    ProviderError instead of silently defaulting. Construction is cheap and
    does no network I/O.
    """
    which = (provider or os.environ.get("LLM_PROVIDER", "litellm")).lower()
    logger.debug("make_llm: selecting provider=%s", which)

    if which in ("haiku", "claude-haiku", "claude"):
        b_url = base_url or os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_LITELLM_BASE_URL
        m = model or os.environ.get("HAIKU_MODEL", "claude-3-5-haiku-20241022")
        key = _require_key(
            api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LITELLM_MASTER_KEY"),
            "ANTHROPIC_API_KEY",
        )
        return OpenAICompatibleLLM(base_url=b_url, model=m, api_key=key)

    if which in ("opencode", "zen", "opencode-zen"):
        b_url = base_url or os.environ.get("OPENCODE_BASE_URL", "https://api.opencode.ai/v1")
        m = model or os.environ.get("OPENCODE_MODEL", "flash-3.8")
        key = _require_key(
            api_key
            or os.environ.get("OPENCODE_GO_KEY")
            or os.environ.get("OPENCODE_LENOVO_KEY")
            or os.environ.get("OPENCODE_API_KEY"),
            "OPENCODE_API_KEY",
        )
        return OpenAICompatibleLLM(base_url=b_url, model=m, api_key=key)

    if which in ("gemini", "google", "flash"):
        b_url = base_url or os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
        m = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        key = _require_key(
            api_key or os.environ.get("GEMINI_PRIMARY_API_KEY") or os.environ.get("GOOGLE_API_KEY"),
            "GEMINI_PRIMARY_API_KEY",
        )
        return OpenAICompatibleLLM(base_url=b_url, model=m, api_key=key)

    if which == "groq":
        b_url = base_url or "https://api.groq.com/openai/v1"
        m = model or os.environ.get("GROQ_MODEL", DEFAULT_GROQ_MODEL)
        key = _require_key(api_key or os.environ.get("GROQ_API_KEY"), "GROQ_API_KEY")
        return OpenAICompatibleLLM(base_url=b_url, model=m, api_key=key)

    if which in ("openai", "gpt"):
        b_url = base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        m = model or os.environ.get("OPENAI_MODEL", "gpt-5-nano")
        key = _require_key(api_key or os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY")
        return OpenAICompatibleLLM(base_url=b_url, model=m, api_key=key)

    if which in ("litellm", "fleet", "local"):
        b_url = base_url or os.environ.get("LLM_BASE_URL", DEFAULT_LITELLM_BASE_URL)
        m = model or os.environ.get("LITELLM_MODEL") or os.environ.get(
            "LLM_MODEL", DEFAULT_LITELLM_MODEL
        )
        key = _require_key(
            api_key or os.environ.get("LLM_API_KEY") or os.environ.get("LITELLM_MASTER_KEY"),
            "LITELLM_MASTER_KEY",
        )
        return OpenAICompatibleLLM(base_url=b_url, model=m, api_key=key)

    if which == "stub":
        return StubLLM()

    raise ProviderError("llm_unknown_provider", f"unknown LLM provider: {which}")
