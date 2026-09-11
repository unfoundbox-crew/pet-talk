"""Process-wide runtime state: the live provider set and the swap lock.

The provider triple lives as module attributes on :mod:`server.app` because
that is the documented seam — QA suites monkeypatch ``server.app.stt`` and
friends, and a turn must observe the patch. Everything else reads the triple
through :func:`snapshot`, which returns ONE immutable
:class:`~server.settings.ProviderSet` for the whole turn, so a ``POST
/settings`` landing mid-turn cannot split that turn across two generations.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Optional

from .logs import log
from .memory import Hippocampus
from .persona import Persona, load_persona
from .provider_factory import ProviderSet, build_providers
from .settings import LEDGER_PATH, RuntimeSettings, SettingsStore

APP_MODULE = "server.app"

#: Held for write by ``POST /settings`` while it rebuilds providers, and for
#: read (briefly) by every turn as it snapshots them.
swap_lock = asyncio.Lock()

settings_store = SettingsStore()
memory = Hippocampus(ledger_path=LEDGER_PATH)

_providers: ProviderSet = build_providers(settings_store.settings)


def _app_module() -> Optional[Any]:
    return sys.modules.get(APP_MODULE)


def current() -> ProviderSet:
    """The provider set as the process sees it right now, honouring any
    monkeypatched ``server.app.{stt,llm,tts}`` override."""
    mod = _app_module()
    if mod is None:
        return _providers
    overrides = {
        name: mod.__dict__[name]
        for name in ("stt", "llm", "tts")
        if name in mod.__dict__ and mod.__dict__[name] is not getattr(_providers, name)
    }
    if not overrides:
        return _providers
    return ProviderSet(
        stt=overrides.get("stt", _providers.stt),
        llm=overrides.get("llm", _providers.llm),
        tts=overrides.get("tts", _providers.tts),
        degraded=_providers.degraded,
    )


def snapshot() -> ProviderSet:
    """Alias for :func:`current`, named for its use at turn start."""
    return current()


def install(pset: ProviderSet) -> ProviderSet:
    """Make ``pset`` the live provider set and publish it on :mod:`server.app`."""
    global _providers
    _providers = pset
    mod = _app_module()
    if mod is not None:
        mod.__dict__["stt"] = pset.stt
        mod.__dict__["llm"] = pset.llm
        mod.__dict__["tts"] = pset.tts
    log.info(
        "providers_installed %s degraded=%s",
        pset.class_names(),
        list(pset.degraded),
    )
    return pset


def degraded() -> list[str]:
    return list(current().degraded)


def settings() -> RuntimeSettings:
    return settings_store.settings


async def swap(req: dict[str, Any]) -> tuple[RuntimeSettings, ProviderSet]:
    """Apply a settings patch and rebuild providers atomically."""
    async with swap_lock:
        merged = settings_store.apply(req)
        pset = install(build_providers(merged))
        return merged, pset


async def snapshot_under_lock() -> ProviderSet:
    """Snapshot providers while no swap is halfway through."""
    async with swap_lock:
        return snapshot()


# ------------------------------------------------------------- persona ---

DEFAULT_PERSONA_NAME = "donna"


def default_persona_name() -> str:
    return (os.environ.get("DEFAULT_PERSONA") or DEFAULT_PERSONA_NAME).strip().lower()


def default_persona() -> Persona:
    try:
        return load_persona(default_persona_name())
    except Exception as e:  # a broken default persona file must not kill boot
        log.warning("default_persona_unloadable name=%s exc=%s", default_persona_name(), e)
        return load_persona("default")
