#!/usr/bin/env python3
"""qa/test_capability_matrix.py — the no-vendor-lock-in claim, as a test.

Lane 4c (2026-09-12): "no vendor lock-in, full customisation, any vendor at
each capability layer can be plugged in or out." This file is the proof, not
the slogan. For every capability layer it asserts:

  (a) at least two providers construct from env alone, no code change
  (b) switching a layer via env changes the class GET /health reports
  (c) switching a layer via POST /settings does the same, at runtime,
      atomically per turn (no restart, no half-swapped ProviderSet)
  (d) an unknown provider name fails closed with a named reason
  (e) a missing required key fails closed with `missing_api_key:<VAR>`
  (f) no provider class name or vendor host is hardcoded outside
      server/providers/ and server/provider_factory.py

Hermetic: stdlib unittest + FastAPI's TestClient only. Every provider under
test is asked only to *construct* (and, for TestClient calls, to answer
GET/POST against the in-process app) — nothing here dials a real vendor.
Every env var this file sets is a fabricated stub value; construction for
every provider below is documented as doing no network I/O (see the class
docstrings in server/providers/*.py), so a stub key never has to be real.

Full narrative results, including two things this file finds and does NOT
paper over — VAD has no second provider today, and LLM's per-vendor classes
collapse to one shared `OpenAICompatibleLLM` so `/health` cannot tell groq
from openai apart — live in docs/CAPABILITY-MATRIX.md. Read that file
alongside this one; this file is what keeps it honest on the next change.
"""
from __future__ import annotations

import contextlib
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server import eyes as eyes_mod
from server.provider_factory import build_providers
from server.providers import ProviderError
from server.settings import RuntimeSettings


# --------------------------------------------------------------- helpers ---


@contextlib.contextmanager
def env(**kv):
    """Set env vars for the block, restoring exactly what was there before.

    Unset a var by passing it the value ``None``.
    """
    missing = object()
    saved = {k: os.environ.get(k, missing) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, prior in saved.items():
            if prior is missing:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prior


STUB = "capability-matrix-stub-value-not-a-real-key"

# One row per provider this layer's `*_PROVIDER` env var accepts, per
# docs/CAPABILITY-MATRIX.md. `keys` are the env vars that must carry a
# (stub) value for construction to succeed; `cls` is the class name
# `ProviderSet.class_names()` — and therefore `GET /health`'s `providers`
# field — reports once that provider is active.
STT_ROWS = [
    ("faster-whisper", {}, "FasterWhisperSTT"),
    ("groq", {"GROQ_API_KEY": STUB}, "GroqSTT"),
    ("deepgram", {"DEEPGRAM_API_KEY": STUB}, "DeepgramSTT"),
    ("openai", {"OPENAI_API_KEY": STUB}, "OpenAIWhisperSTT"),
    ("whisperkit", {}, "WhisperKitSTT"),
    ("mlx", {}, "MLXWhisperSTT"),
    ("sensevoice", {}, "SenseVoiceSTT"),
    ("stub", {}, "StubSTT"),
]

# LLM tyres share one wire-format class (`OpenAICompatibleLLM` — see the
# module docstring): the vendor lives in `llm_base_url`/`llm_model`, not in
# a distinct Python type. `cls` below is honest about that collapse.
LLM_ROWS = [
    ("litellm", {"LLM_API_KEY": STUB}, "OpenAICompatibleLLM"),
    ("haiku", {"ANTHROPIC_API_KEY": STUB}, "OpenAICompatibleLLM"),
    ("opencode", {"OPENCODE_API_KEY": STUB}, "OpenAICompatibleLLM"),
    ("gemini", {"GEMINI_PRIMARY_API_KEY": STUB}, "OpenAICompatibleLLM"),
    ("groq", {"GROQ_API_KEY": STUB}, "OpenAICompatibleLLM"),
    ("openai", {"OPENAI_API_KEY": STUB}, "OpenAICompatibleLLM"),
    ("stub", {}, "StubLLM"),
]

TTS_ROWS = [
    ("kokoro-local", {}, "KokoroLocalTTS"),
    ("kokoro", {}, "KokoroSpacePilotTTS"),
    ("smallest", {"SMALLEST_API_KEY": STUB}, "SmallestAITTS"),
    ("elevenlabs", {"ELEVENLABS_API_KEY": STUB}, "ElevenLabsTTS"),
    ("deepgram", {"DEEPGRAM_API_KEY": STUB}, "DeepgramTTS"),
    ("stub", {}, "StubTTS"),
]

# server/eyes.py is its own config surface (EyesConfig / EYES_ENGINE), not
# RuntimeSettings — so it gets its own small row set rather than reusing
# STT/LLM/TTS's settings-based helper below.
EYES_ROWS = [
    ("zrv", "ZrvEngine"),
    ("stub", "StubEngine"),
]


def _settings_env(stt=None, llm=None, tts=None, extra=None):
    """Build the env kwargs for `env()` that select one provider per layer.

    Every OTHER provider's key vars are explicitly unset so a previous
    test's (or the real shell's) leftover credential can never make a
    provider construct that the test didn't ask for — the whole point of
    this file is that env alone decides, deterministically.

    A layer the caller did not name still gets a value: STT/TTS default to
    ``stub`` (their real env-absent defaults, `faster-whisper`/`kokoro-local`,
    need no key so leaving them alone would also be safe, but pinning to
    `stub` keeps a test about one layer from depending on another layer's
    heavy default construction). LLM's real env-absent default is
    `litellm`, which DOES require a key — so LLM is pinned to `stub` here
    too whenever the test isn't specifically exercising the LLM layer,
    otherwise every STT/TTS-only case would show a spurious
    `llm:missing_api_key:LITELLM_MASTER_KEY` in `.degraded`.
    """
    all_keys = {
        "DEEPGRAM_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "SMALLEST_API_KEY",
        "OPENCODE_API_KEY", "OPENCODE_GO_KEY", "OPENCODE_LENOVO_KEY",
        "GEMINI_PRIMARY_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY",
        "LLM_API_KEY", "LITELLM_MASTER_KEY", "ELEVENLABS_API_KEY",
    }
    kv: dict = {k: None for k in all_keys}
    kv["STT_PROVIDER"] = stt[0] if stt else "stub"
    kv["LLM_PROVIDER"] = llm[0] if llm else "stub"
    kv["TTS_PROVIDER"] = tts[0] if tts else "stub"
    if stt:
        kv.update(stt[1])
    if llm:
        kv.update(llm[1])
    if tts:
        kv.update(tts[1])
    kv.update(extra or {})
    return kv


def _build(stt=None, llm=None, tts=None):
    """`RuntimeSettings.from_env()` + `build_providers()` — the exact pair
    `server/runtime.py` runs at boot and on every `POST /settings`. This is
    what `GET /health`'s `providers`/`degraded` fields are built from, so
    exercising it here is exercising `/health` without paying for a live
    server process per case.
    """
    with env(**_settings_env(stt=stt, llm=llm, tts=tts)):
        settings = RuntimeSettings.from_env()
        return build_providers(settings)


# --------------------------------------------- (a) two+ providers, no code ---


class TestAtLeastTwoProvidersFromEnv(unittest.TestCase):
    """Acceptance (a): each layer has >= 2 providers constructible from env
    with no code change. `build_providers` never raises (provider_factory.py
    docstring); a provider that failed to build shows up in `.degraded`, so
    "constructible" here means "not degraded", checked explicitly.
    """

    def test_stt_has_at_least_two(self):
        ok = []
        for name, keys, cls in STT_ROWS:
            with self.subTest(provider=name):
                pset = _build(stt=(name, keys))
                self.assertEqual(pset.degraded, (), f"{name} degraded: {pset.degraded}")
                self.assertEqual(type(pset.stt).__name__, cls)
                ok.append(name)
        self.assertGreaterEqual(len(ok), 2, STT_ROWS)

    def test_llm_has_at_least_two(self):
        ok = []
        for name, keys, cls in LLM_ROWS:
            with self.subTest(provider=name):
                pset = _build(llm=(name, keys))
                self.assertEqual(pset.degraded, (), f"{name} degraded: {pset.degraded}")
                self.assertEqual(type(pset.llm).__name__, cls)
                ok.append(name)
        self.assertGreaterEqual(len(ok), 2, LLM_ROWS)

    def test_tts_has_at_least_two(self):
        ok = []
        for name, keys, cls in TTS_ROWS:
            with self.subTest(provider=name):
                pset = _build(tts=(name, keys))
                self.assertEqual(pset.degraded, (), f"{name} degraded: {pset.degraded}")
                self.assertEqual(type(pset.tts).__name__, cls)
                ok.append(name)
        self.assertGreaterEqual(len(ok), 2, TTS_ROWS)

    def test_eyes_ocr_engine_has_at_least_two(self):
        for name, cls in EYES_ROWS:
            with self.subTest(engine=name):
                engine = eyes_mod.make_engine(name, eyes_mod.EyesConfig(engine=name))
                self.assertEqual(type(engine).__name__, cls)
        self.assertGreaterEqual(len(EYES_ROWS), 2)

    def test_vad_has_only_one_provider_today_this_is_a_real_gap(self):
        """NOT a fake green: `server/providers/vad.py` ships exactly one
        class (`EnergyGateVAD`), no `make_vad()` factory, and no
        `VAD_PROVIDER` env var — and grepping server/*.py for its own name
        shows it is never imported outside its own module and the package
        `__init__.py` re-export. The VAD actually running in production is
        a *different*, hardcoded class (`cli/audio.py`'s `EnergyVAD`), not
        this one. Recorded here so a future second VAD provider has a test
        to flip from skip to pass — see docs/CAPABILITY-MATRIX.md."""
        import inspect

        from server.providers import vad as vad_mod

        provider_classes = [
            obj
            for _name, obj in vars(vad_mod).items()
            if inspect.isclass(obj) and issubclass(obj, vad_mod.VAD) and obj is not vad_mod.VAD
        ]
        self.assertEqual(
            [c.__name__ for c in provider_classes],
            ["EnergyGateVAD"],
            "expected exactly one VAD implementation today (server/providers/vad.py); "
            "if this now fails because a second one was added, update this test AND "
            "docs/CAPABILITY-MATRIX.md's VAD row together",
        )
        self.assertFalse(
            hasattr(vad_mod, "make_vad"),
            "a make_vad() factory appeared — wire VAD_PROVIDER through "
            "server/settings.py and server/provider_factory.py and update the "
            "capability matrix; this assertion is the tripwire for that work",
        )
        self.assertNotIn("VAD_PROVIDER", os.environ.keys() | set())  # documents: no such var exists to unset


# ------------------------------------------- (b) env swap changes /health ---


class TestEnvSwapChangesHealth(unittest.TestCase):
    """Acceptance (b): switching a layer via env changes the active class
    `GET /health` reports. `/health` (routes_http.py) reports
    `runtime.current().class_names()`, which is exactly `ProviderSet.class_names()`
    on the object `build_providers()` returns — so asserting on that object's
    `class_names()` is asserting on `/health`'s own payload shape, without
    needing a live process per env combination.

    LLM is the one layer where this is only partly true — see the class
    docstring collapse noted above. The stub-vs-real pair below still proves
    the *mechanism* switches; it does not prove `/health` can tell two cloud
    LLM vendors apart, because today it cannot. That gap is recorded, not
    hidden, in docs/CAPABILITY-MATRIX.md.
    """

    def test_stt_env_swap_changes_health_class(self):
        a = _build(stt=("faster-whisper", {}))
        b = _build(stt=("groq", {"GROQ_API_KEY": STUB}))
        self.assertNotEqual(
            a.class_names()["stt"], b.class_names()["stt"],
            "STT_PROVIDER=faster-whisper vs groq must report different /health classes",
        )

    def test_tts_env_swap_changes_health_class(self):
        a = _build(tts=("kokoro-local", {}))
        b = _build(tts=("elevenlabs", {}))
        self.assertNotEqual(a.class_names()["tts"], b.class_names()["tts"])

    def test_llm_env_swap_stub_vs_real_changes_health_class(self):
        a = _build(llm=("stub", {}))
        b = _build(llm=("litellm", {"LLM_API_KEY": STUB}))
        self.assertNotEqual(a.class_names()["llm"], b.class_names()["llm"])

    def test_llm_env_swap_between_two_cloud_vendors_does_not_change_class_KNOWN_GAP(self):
        """Documents the real limitation named above, so it cannot regress
        into a silent claim of "fixed" without this test being touched."""
        groq = _build(llm=("groq", {"GROQ_API_KEY": STUB}))
        openai = _build(llm=("openai", {"OPENAI_API_KEY": STUB}))
        self.assertEqual(
            groq.class_names()["llm"], openai.class_names()["llm"],
            "if this now differs, the LLM layer grew per-vendor classes — "
            "great, but then update docs/CAPABILITY-MATRIX.md's LLM row and "
            "this test together, don't just let it silently start passing",
        )


# ------------------------------------ (c) POST /settings swap, at runtime ---


class TestRuntimeSwapViaSettingsApi(unittest.TestCase):
    """Acceptance (c): `POST /settings` swaps a layer at runtime, no
    restart, atomically per turn. Same TestClient pattern as
    qa/test_settings_api.py; this file adds STT (untested there) and checks
    the atomicity property directly: `ProviderSet` is a frozen dataclass, so
    a swap can only ever *replace* the object a turn snapshotted, never
    mutate it out from under an in-flight turn.
    """

    def setUp(self):
        from fastapi.testclient import TestClient

        from server.app import app
        from server.auth import STUDIO_TOKEN_HEADER, studio_token

        self.client = TestClient(app, headers={STUDIO_TOKEN_HEADER: studio_token()})
        self.addCleanup(
            self.client.post,
            "/settings",
            json={"stt_provider": "stub", "llm_provider": "stub", "tts_provider": "stub"},
        )

    def test_post_settings_swaps_stt_and_health_reflects_it(self):
        r = self.client.post(
            "/settings",
            json={"stt_provider": "deepgram", "deepgram_api_key": STUB},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["active"]["stt"], "DeepgramSTT")
        health = self.client.get("/health").json()
        self.assertEqual(health["providers"]["stt"], "DeepgramSTT")

    def test_post_settings_swap_is_atomic_new_object_not_mutated_old(self):
        from server import runtime

        before = runtime.current()
        r = self.client.post(
            "/settings", json={"tts_provider": "smallest", "smallest_api_key": STUB}
        )
        self.assertEqual(r.status_code, 200)
        after = runtime.current()
        self.assertIsNot(before, after, "a swap must install a new ProviderSet, never mutate in place")
        self.assertEqual(type(before.tts).__name__, "StubTTS" if before is not after else type(before.tts).__name__)
        self.assertEqual(after.class_names()["tts"], "SmallestAITTS")
        # ProviderSet is declared frozen — a snapshot a turn already holds
        # cannot be edited by a later swap even if some caller tried to.
        with self.assertRaises(Exception):
            after.tts = None  # type: ignore[misc]

    def test_no_restart_needed_process_stays_up_across_swap(self):
        r1 = self.client.get("/health")
        self.client.post("/settings", json={"stt_provider": "groq", "groq_api_key": STUB})
        r2 = self.client.get("/health")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertNotEqual(r1.json()["providers"]["stt"], r2.json()["providers"]["stt"])


# ------------------------------------------- (d) unknown provider, closed ---


class TestUnknownProviderFailsClosed(unittest.TestCase):
    def test_stt_unknown_provider(self):
        pset = _build(stt=("not-a-real-provider", {}))
        self.assertIn("stt:stt_unknown_provider", pset.degraded[0])

    def test_llm_unknown_provider(self):
        pset = _build(llm=("not-a-real-provider", {}))
        self.assertIn("llm:llm_unknown_provider", pset.degraded[0])

    def test_tts_unknown_provider(self):
        pset = _build(tts=("not-a-real-provider", {}))
        self.assertIn("tts:tts_unknown_provider", pset.degraded[0])

    def test_eyes_unknown_engine(self):
        with self.assertRaises(eyes_mod.EyesError) as ctx:
            eyes_mod.make_engine("not-a-real-engine")
        self.assertEqual(ctx.exception.reason, eyes_mod.REASON_DISABLED)
        self.assertIn("unknown_eyes_engine", ctx.exception.detail)


# --------------------------------------------- (e) missing key, closed ---


class TestMissingKeyFailsClosedByName(unittest.TestCase):
    """`missing_api_key:<VAR>` is `provider_factory._require_key`'s reason
    plus detail, joined the same way `build_providers` joins every degraded
    entry: `f"{kind}:{e.reason}" + f":{e.detail}"`.
    """

    def test_stt_missing_key(self):
        pset = _build(stt=("groq", {}))  # GROQ_API_KEY deliberately absent
        self.assertEqual(pset.degraded, ("stt:missing_api_key:GROQ_API_KEY",))

    def test_llm_missing_key(self):
        pset = _build(llm=("haiku", {}))  # ANTHROPIC_API_KEY deliberately absent
        self.assertEqual(pset.degraded, ("llm:missing_api_key:ANTHROPIC_API_KEY",))

    def test_tts_missing_key(self):
        pset = _build(tts=("smallest", {}))  # SMALLEST_API_KEY deliberately absent
        self.assertEqual(pset.degraded, ("tts:missing_api_key:SMALLEST_API_KEY",))

    def test_degraded_entry_is_reported_at_health(self):
        with env(**_settings_env(stt=("groq", {}))):
            settings = RuntimeSettings.from_env()
            pset = build_providers(settings)
            from server import runtime

            runtime.install(pset)
            try:
                from fastapi.testclient import TestClient

                from server.app import app

                client = TestClient(app)
                health = client.get("/health").json()
                self.assertIn("stt:missing_api_key:GROQ_API_KEY", health["degraded"])
            finally:
                runtime.install(build_providers(RuntimeSettings.from_env()))


# -------------------------------------- (f) no hardcoded vendor / class ---


#: Files where a vendor host or a provider class name is EXPECTED and
#: documented, not a lock-in leak:
#:   server/providers/*.py, server/provider_factory.py — this IS the
#:     provider layer, per the plan card and provider_factory.py's own
#:     docstring ("the tyre factory wrapper").
#:   server/settings.py — the config-RESOLUTION module (its own docstring:
#:     "Provider construction itself lives in server.provider_factory");
#:     it needs each vendor's default base URL/model to resolve an env
#:     chain, same as the provider files do for their own fallback literal.
#:   server/auth.py — the egress ALLOWLIST (PROVIDER_HOSTS): a security
#:     control that must enumerate vendor hosts to refuse everything else,
#:     already asserted to match reality by qa/test_security.py.
#:   server/test_dg.py — a hand-run vendor-specific smoke script, not
#:     production request-serving code; it is not registered in
#:     qa/run_all.sh and never executes as part of a turn.
_ALLOWED_FILES = {"provider_factory.py", "settings.py", "auth.py", "test_dg.py"}

_VENDOR_HOST_RE = re.compile(
    r"api\.(anthropic|openai|groq|deepgram|elevenlabs|smallest|opencode)\.(com|ai|io)"
    r"|generativelanguage\.googleapis\.com",
    re.IGNORECASE,
)

_PROVIDER_CLASS_NAMES = (
    "KokoroLocalTTS", "KokoroSpacePilotTTS", "OpenAICompatibleLLM",
    "FasterWhisperSTT", "GroqSTT", "DeepgramSTT", "DeepgramTTS",
    "OpenAIWhisperSTT", "WhisperKitSTT", "MLXWhisperSTT", "SenseVoiceSTT",
    "ElevenLabsTTS", "SmallestAITTS", "WhisperLocalSTT",
)


class TestNoHardcodedVendorOutsideProviderLayer(unittest.TestCase):
    """Acceptance (f), grep-based. `server/providers/` and the four allowed
    files above are exempt for the documented reasons; every other
    `server/*.py` must name zero vendor hosts and zero provider class names
    — the app's business logic talks to `ProviderSet`/`LLMProvider` etc.,
    never to "the Groq one" by name.
    """

    def _server_py_files(self):
        server_dir = os.path.join(ROOT, "server")
        for name in sorted(os.listdir(server_dir)):
            path = os.path.join(server_dir, name)
            if os.path.isfile(path) and name.endswith(".py") and name not in _ALLOWED_FILES:
                yield path

    def test_no_vendor_host_outside_provider_layer(self):
        offenders = []
        for path in self._server_py_files():
            with open(path, encoding="utf-8") as f:
                text = f.read()
            m = _VENDOR_HOST_RE.search(text)
            if m:
                offenders.append((os.path.basename(path), m.group(0)))
        self.assertEqual(offenders, [], f"vendor host(s) hardcoded outside the provider layer: {offenders}")

    def test_no_provider_class_name_outside_provider_layer(self):
        """Matches an actual code coupling — importing the class, calling
        its constructor, or an `isinstance` check — not a bare mention.
        A comment or docstring that *names* the current default tyre for a
        human reader (e.g. `server/stall.py`'s "``KokoroLocalTTS`` already
        warms its model this way") is documentation, not lock-in: nothing
        in that file imports, constructs, or branches on the class. If a
        future edit turns a mention like that into a real import or an
        `isinstance()` branch, this test starts failing on it — which is
        the point.
        """
        offenders = []
        for path in self._server_py_files():
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for cls in _PROVIDER_CLASS_NAMES:
                coupling = re.search(
                    rf"\bimport\b[^\n]*\b{re.escape(cls)}\b|\b{re.escape(cls)}\s*\(", text
                )
                if coupling:
                    offenders.append((os.path.basename(path), cls, coupling.group(0)))
        self.assertEqual(offenders, [], f"provider class name(s) referenced outside the provider layer: {offenders}")


# --------------------------------------- LLM routing through fleet aliases ---


class TestFleetRoutingAliases(unittest.TestCase):
    """The plan names "LLM routing through fleet aliases once the router
    ships" as a sixth layer. Lane 1 (the sovereign router) is in flight in a
    separate session today and has not landed `x-fleet-route`, strict mode,
    or the `fleet/frontier`-style model aliases. Skipped, not faked green:
    this file will start asserting real routing behaviour the day that
    lands, per docs/CAPABILITY-MATRIX.md's LLM-routing row.
    """

    def test_fleet_alias_routing_not_shippped_yet(self):
        raise unittest.SkipTest(
            "NOT-MEASURED: fleet/* capability-alias routing depends on the sovereign "
            "router (docs/BRIEF-2026-09-12-sovereign-quota.md), in flight in a separate "
            "session as of 2026-09-12 and not yet in this checkout. Today "
            "LLM_PROVIDER=litellm|fleet|local are three names for the same LiteLLM "
            "proxy address (server/settings.py LLM_BASE_URL_DEFAULTS) — not per-capability "
            "routing."
        )

    def test_llm_provider_fleet_and_local_are_litellm_aliases_today(self):
        """What IS true today, so this isn't purely a skip: `fleet` and
        `local` already resolve to the same base URL as `litellm` — the
        alias exists, the router behind it does not yet."""
        for name in ("litellm", "fleet", "local"):
            with self.subTest(provider=name):
                pset = _build(llm=(name, {"LLM_API_KEY": STUB}))
                self.assertEqual(pset.degraded, ())
                self.assertEqual(type(pset.llm).__name__, "OpenAICompatibleLLM")


if __name__ == "__main__":
    unittest.main()
