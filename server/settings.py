"""Runtime settings: paths, the settings dataclass, and the settings store.

Law 2 (AGENTS.md): provider swaps are config-only — env at boot, the
``/settings`` API at runtime. Law 3: no secrets in tree, so every credential
comes from the environment or an explicit API call; there are no baked-in
default keys. Provider construction itself lives in
:mod:`server.provider_factory`.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Optional

from .logs import swallowed

# --------------------------------------------------------------- paths ---

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SERVER_DIR)
VOICES_PATH = os.path.join(REPO_ROOT, "personas", "voices.yaml")
TURNS_PATH = os.path.join(SERVER_DIR, "turns.jsonl")
LEDGER_PATH = os.path.join(SERVER_DIR, "ledger.jsonl")

SERVICE_NAME = "pet-talk-duplex"
SERVICE_VERSION = "0.2"

# Field names holding a credential. Redacted by ``/settings``.
SECRET_HINTS = ("key", "token", "secret", "password")
REDACTED = "***"

# Credential each provider needs, by canonical env var name. Providers absent
# from a table need no credential (local daemons, stubs).
STT_REQUIRED_KEY: dict[str, str] = {
    "deepgram": "DEEPGRAM_API_KEY",
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-whisper": "OPENAI_API_KEY",
    "whisper-openai": "OPENAI_API_KEY",
}
TTS_REQUIRED_KEY: dict[str, str] = {
    "smallest": "SMALLEST_API_KEY",
    "smallest-ai": "SMALLEST_API_KEY",
    "smallest_ai": "SMALLEST_API_KEY",
    "waves": "SMALLEST_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
}
LLM_REQUIRED_KEY: dict[str, str] = {
    "haiku": "ANTHROPIC_API_KEY",
    "claude-haiku": "ANTHROPIC_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "opencode": "OPENCODE_API_KEY",
    "zen": "OPENCODE_API_KEY",
    "opencode-zen": "OPENCODE_API_KEY",
    "gemini": "GEMINI_PRIMARY_API_KEY",
    "google": "GEMINI_PRIMARY_API_KEY",
    "flash": "GEMINI_PRIMARY_API_KEY",
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gpt": "OPENAI_API_KEY",
}

#: Where a self-hosted LiteLLM proxy lives when nothing says otherwise.
#: Localhost, never a tailnet address — a hardcoded 100.x default sent every
#: fresh checkout at one particular machine.
LITELLM_DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"
GEMINI_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
LITELLM_BASE_URL_VARS = ("LITELLM_BASE_URL", "LLM_BASE_URL")

# Per-provider endpoint/model defaults chosen when the provider changes and the
# caller did not pin them. Each value is (env var chain, literal): the first
# var that is set wins, and the literal is the last resort.
LLM_BASE_URL_DEFAULTS: dict[str, tuple[tuple[str, ...], str]] = {
    "haiku": (("ANTHROPIC_BASE_URL",), "https://api.anthropic.com/v1"),
    "claude-haiku": (("ANTHROPIC_BASE_URL",), "https://api.anthropic.com/v1"),
    "claude": (("ANTHROPIC_BASE_URL",), "https://api.anthropic.com/v1"),
    "opencode": (("OPENCODE_BASE_URL",), "https://api.opencode.ai/v1"),
    "zen": (("OPENCODE_BASE_URL",), "https://api.opencode.ai/v1"),
    "opencode-zen": (("OPENCODE_BASE_URL",), "https://api.opencode.ai/v1"),
    "gemini": (("GEMINI_BASE_URL",), GEMINI_DEFAULT_BASE_URL),
    "google": (("GEMINI_BASE_URL",), GEMINI_DEFAULT_BASE_URL),
    "flash": (("GEMINI_BASE_URL",), GEMINI_DEFAULT_BASE_URL),
    "groq": (("GROQ_BASE_URL",), "https://api.groq.com/openai/v1"),
    "openai": (("OPENAI_BASE_URL",), "https://api.openai.com/v1"),
    "gpt": (("OPENAI_BASE_URL",), "https://api.openai.com/v1"),
    "litellm": (LITELLM_BASE_URL_VARS, LITELLM_DEFAULT_BASE_URL),
    "fleet": (LITELLM_BASE_URL_VARS, LITELLM_DEFAULT_BASE_URL),
    "local": (LITELLM_BASE_URL_VARS, LITELLM_DEFAULT_BASE_URL),
}
LLM_MODEL_DEFAULTS: dict[str, tuple[tuple[str, ...], str]] = {
    "haiku": (("HAIKU_MODEL",), "claude-3-5-haiku-20241022"),
    "claude-haiku": (("HAIKU_MODEL",), "claude-3-5-haiku-20241022"),
    "claude": (("HAIKU_MODEL",), "claude-3-5-haiku-20241022"),
    "opencode": (("OPENCODE_MODEL",), "flash-3.8"),
    "zen": (("OPENCODE_MODEL",), "flash-3.8"),
    "opencode-zen": (("OPENCODE_MODEL",), "flash-3.8"),
    "gemini": (("GEMINI_MODEL",), "gemini-2.5-flash"),
    "google": (("GEMINI_MODEL",), "gemini-2.5-flash"),
    "flash": (("GEMINI_MODEL",), "gemini-2.5-flash"),
    "groq": (("GROQ_MODEL",), "groq/compound-mini"),
    "openai": (("OPENAI_MODEL",), "gpt-5-nano"),
    "gpt": (("OPENAI_MODEL",), "gpt-5-nano"),
    "litellm": (("LITELLM_MODEL", "LLM_MODEL"), "claude-sonnet-4-6"),
    "fleet": (("LITELLM_MODEL", "LLM_MODEL"), "claude-sonnet-4-6"),
    "local": (("LITELLM_MODEL", "LLM_MODEL"), "claude-sonnet-4-6"),
}
# Settings field holding the key for each LLM provider family.
LLM_KEY_FIELD: dict[str, str] = {
    "groq": "groq_api_key",
    "openai": "openai_api_key",
    "gpt": "openai_api_key",
    "haiku": "anthropic_api_key",
    "claude-haiku": "anthropic_api_key",
    "claude": "anthropic_api_key",
    "opencode": "opencode_api_key",
    "zen": "opencode_api_key",
    "opencode-zen": "opencode_api_key",
    "gemini": "gemini_api_key",
    "google": "gemini_api_key",
    "flash": "gemini_api_key",
}


def _env_default(var: str, literal: str = "") -> str:
    return os.environ.get(var) or literal


def _env_chain(variables: tuple[str, ...], literal: str = "") -> str:
    """First env var in the chain that is set, else the literal."""
    for var in variables:
        value = os.environ.get(var)
        if value:
            return value
    return literal


DEFAULT_LLM_BASE = (LITELLM_BASE_URL_VARS, LITELLM_DEFAULT_BASE_URL)
DEFAULT_LLM_MODEL = (("LITELLM_MODEL", "LLM_MODEL"), "claude-sonnet-4-6")


def _resolve_chain(variables: tuple[str, ...], literal: str, generic: str) -> str:
    """Resolve a provider's env chain, honouring the generic override.

    The chain is tried in order first, so a provider-specific var
    (``LITELLM_BASE_URL``) beats the generic one (``LLM_BASE_URL``) when both
    are set. The generic var still applies to providers whose chain does not
    already list it, where it reads as an explicit operator override.
    """
    resolved = _env_chain(variables)
    if resolved:
        return resolved
    if generic not in variables:
        generic_value = os.environ.get(generic)
        if generic_value:
            return generic_value
    return literal


def is_secret_field(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in SECRET_HINTS)


# ------------------------------------------------------------ settings ---


@dataclass
class RuntimeSettings:
    """Every knob the ``/settings`` API can turn. No credential defaults."""

    stt_provider: str = "stub"
    llm_provider: str = "stub"
    tts_provider: str = "stub"

    deepgram_api_key: str = ""
    groq_api_key: str = ""
    openai_api_key: str = ""
    smallest_api_key: str = ""
    opencode_api_key: str = ""
    gemini_api_key: str = ""
    anthropic_api_key: str = ""

    sensevoice_base_url: str = ""
    kokoro_base_url: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    vad_silence_ms: int = 600

    @classmethod
    def from_env(cls) -> "RuntimeSettings":
        groq = os.environ.get("GROQ_API_KEY", "")
        anthropic = os.environ.get("ANTHROPIC_API_KEY", "")
        stt_default = "deepgram" if os.environ.get("DEEPGRAM_API_KEY") else "faster-whisper"
        llm_default = "groq" if groq else ("haiku" if anthropic else "litellm")
        tts_default = "smallest" if os.environ.get("SMALLEST_API_KEY") else "kokoro"
        llm_provider = os.environ.get("LLM_PROVIDER", llm_default).lower()
        base_vars, base_literal = LLM_BASE_URL_DEFAULTS.get(llm_provider, DEFAULT_LLM_BASE)
        model_vars, model_literal = LLM_MODEL_DEFAULTS.get(llm_provider, DEFAULT_LLM_MODEL)
        settings = cls(
            stt_provider=os.environ.get("STT_PROVIDER", stt_default).lower(),
            llm_provider=llm_provider,
            tts_provider=os.environ.get("TTS_PROVIDER", tts_default).lower(),
            deepgram_api_key=os.environ.get("DEEPGRAM_API_KEY", ""),
            groq_api_key=groq,
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            smallest_api_key=os.environ.get("SMALLEST_API_KEY", ""),
            opencode_api_key=(
                os.environ.get("OPENCODE_GO_KEY")
                or os.environ.get("OPENCODE_LENOVO_KEY")
                or os.environ.get("OPENCODE_API_KEY", "")
            ),
            gemini_api_key=(
                os.environ.get("GEMINI_PRIMARY_API_KEY")
                or os.environ.get("GOOGLE_API_KEY", "")
            ),
            anthropic_api_key=anthropic,
            sensevoice_base_url=_env_default("SENSEVOICE_BASE_URL", "http://127.0.0.1:8086"),
            kokoro_base_url=_env_default("KOKORO_BASE_URL", "http://127.0.0.1:8088"),
            llm_base_url=_resolve_chain(base_vars, base_literal, "LLM_BASE_URL"),
            llm_model=_resolve_chain(model_vars, model_literal, "LLM_MODEL"),
            vad_silence_ms=600,
        )
        try:
            settings.vad_silence_ms = int(os.environ.get("VAD_SILENCE_MS", "600"))
        except ValueError as e:
            swallowed("bad_vad_silence_ms_env", e, value=os.environ.get("VAD_SILENCE_MS"))
        settings.llm_api_key = os.environ.get("LLM_API_KEY") or settings.key_for_llm()
        return settings

    # -- credential resolution ------------------------------------------

    def key_for_llm(self) -> str:
        field_name = LLM_KEY_FIELD.get(self.llm_provider)
        if field_name:
            return str(getattr(self, field_name, "") or "")
        return str(self.llm_api_key or "")

    def key_for_stt(self) -> str:
        which = self.stt_provider
        if which == "deepgram":
            return self.deepgram_api_key
        if which == "groq":
            return self.groq_api_key
        if which in ("openai", "openai-whisper", "whisper-openai"):
            return self.openai_api_key
        return ""

    def key_for_tts(self) -> str:
        if self.tts_provider in ("smallest", "smallest-ai", "smallest_ai", "waves"):
            return self.smallest_api_key
        return ""

    # -- serialization --------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def redacted(self) -> tuple[dict[str, Any], dict[str, bool]]:
        """Return (settings with secrets masked, {secret_name: set?})."""
        out: dict[str, Any] = {}
        secrets_set: dict[str, bool] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if is_secret_field(f.name):
                present = bool(str(value or "").strip())
                secrets_set[f.name] = present
                out[f.name] = REDACTED if present else ""
            else:
                out[f.name] = value
        return out, secrets_set


# ``RUNTIME_SETTINGS`` is the long-standing dict view of the settings above.
# It is kept in sync by :meth:`SettingsStore.sync_dict` — never rebind it.
RUNTIME_SETTINGS: dict[str, Any] = {}


# -------------------------------------------------------------- store ---


class SettingsStore:
    """Owns the mutable settings dataclass. Mutated only under the swap
    lock held by ``POST /settings`` (see :mod:`server.runtime`)."""

    def __init__(self, settings: Optional[RuntimeSettings] = None) -> None:
        self.settings = settings or RuntimeSettings.from_env()
        self.sync_dict()

    def sync_dict(self) -> dict[str, Any]:
        RUNTIME_SETTINGS.clear()
        RUNTIME_SETTINGS.update(self.settings.to_dict())
        return RUNTIME_SETTINGS

    def apply(self, req: dict[str, Any]) -> RuntimeSettings:
        """Merge a ``POST /settings`` body. Returns the new settings.

        A secret arriving as the redaction mask means "leave it alone", so a
        UI that round-trips a masked ``GET`` cannot wipe a live credential.
        """
        current = self.settings
        updates: dict[str, Any] = {}
        known = {f.name for f in fields(RuntimeSettings)}

        for name in known:
            if name not in req:
                continue
            raw = req[name]
            if is_secret_field(name) and str(raw).strip() == REDACTED:
                continue
            if name == "vad_silence_ms":
                try:
                    updates[name] = int(raw)
                except (TypeError, ValueError) as e:
                    swallowed("bad_vad_silence_ms", e, value=raw)
                continue
            if name.endswith("_provider"):
                updates[name] = str(raw).strip().lower()
            else:
                updates[name] = str(raw)

        provider_changed = (
            "llm_provider" in updates and updates["llm_provider"] != current.llm_provider
        )
        merged = replace(current, **updates)

        # A provider change without an explicit endpoint/model/key picks that
        # provider's own defaults rather than inheriting the previous one's.
        if provider_changed:
            which = merged.llm_provider
            if "llm_base_url" not in updates:
                variables, literal = LLM_BASE_URL_DEFAULTS.get(which, DEFAULT_LLM_BASE)
                resolved = _env_chain(variables, literal)
                if resolved:
                    merged.llm_base_url = resolved
            if "llm_model" not in updates:
                variables, literal = LLM_MODEL_DEFAULTS.get(which, DEFAULT_LLM_MODEL)
                resolved = _env_chain(variables, literal)
                if resolved:
                    merged.llm_model = resolved
            if "llm_api_key" not in updates:
                merged.llm_api_key = merged.key_for_llm()
        elif "llm_api_key" not in updates and merged.key_for_llm():
            merged.llm_api_key = merged.key_for_llm()

        self.settings = merged
        self.sync_dict()
        return merged
