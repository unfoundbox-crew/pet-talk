"""server/eyes.py — Eyes lane (Wave 3 §1, TECH-DESIGN Phase 4).

Zero-vision multimodal ingestion. A client attaches an image / screenshot /
PDF over the existing ``/ws`` duplex socket; this module OCRs it locally via
the ``zrv`` CLI (zero-vision) and hands back plain text. No vision tokens are
burned, no pixels leave the machine.

Caller contract (``server/app.py``, lane A)::

    try:
        from server import eyes
    except Exception:
        eyes = None
    ...
    if msg["type"] == "user.attach" and eyes is not None:
        await eyes.handle_attach(ws, msg, persona)

``handle_attach`` never raises: every failure becomes an ``agent.error``
frame with a snake_case reason (AGENTS.md law 1, fail closed).

Send path — DECISION: ``handle_attach`` builds its own sender by *lazily*
importing ``frame`` and ``_safe_send_json`` from ``server.app`` inside the
function body, so importing ``server.eyes`` never pulls in FastAPI and there
is no import cycle. If ``server.app`` is not importable (unit tests), it
falls back to a local frame builder plus ``ws.send_json``. Tests may bypass
the socket entirely by passing ``send=<async callable taking one dict>``.

Env knobs (config-only, AGENTS.md law 2):

===========================  =======================================
``EYES_ENGINE``              ``zrv`` (default) | ``stub``
``EYES_ENGINE_PIN``          zrv ``--engine`` for transcribe
                             (``apple-vision``/``tesseract``/…), unset
                             lets zrv resolve its own default
``EYES_DESCRIBE_ENGINE``     zrv ``--engine`` for describe (unset by
                             default; ``apple-vision`` CANNOT describe and
                             ``apple-fm`` measured 101.9s — see EyesConfig)
``EYES_DEFAULT_TASK``        task when frame+persona are silent
                             (default ``transcribe``)
``EYES_MAX_BYTES``           decoded byte cap (default 8388608)
``EYES_CHAR_CAP``            text cap before ``truncated:true`` (4000)
``EYES_TIMEOUT_S``           OCR wall clock (default 8.0)
``EYES_ZRV_BIN``             path/name of the zrv binary (default ``zrv``)
``EYES_SCRATCH_DIR``         temp dir for decoded bytes (default a
                             ``pet-talk-eyes`` dir inside the system temp
                             dir — never inside the repo, never
                             ``_audio_store``)
``PET_TALK_DICTATION``       ``1`` => default task is ``transcribe``
``PET_TALK_EYES_CONTEXT_MAX_CHARS``
                             cap, in characters, on the tagged context line
                             queued for the *next turn's prompt* (1200).
                             Independent of ``EYES_CHAR_CAP``, which bounds
                             only the ``eyes.text`` wire frame.
``FLEET_VISION_MODEL``       resolves the forward-looking
                             ``EYES_DESCRIBE_ENGINE=fleet/vision`` alias
                             once the LiteLLM router ships a ``fleet/vision``
                             route (expected: ``cloud-vlm``). Unset today —
                             a describe pinned to ``fleet/vision`` fails
                             closed as ``eyes_disabled`` naming this var.
===========================  =======================================
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field

from .logs import swallowed

# ---------------------------------------------------------------- errors ---

# The only reasons this module ever emits (cross-lane contract).
REASON_TOO_LARGE = "eyes_too_large"
REASON_BAD_KIND = "eyes_bad_kind"
REASON_OCR_FAILED = "eyes_ocr_failed"
REASON_NO_TEXT = "eyes_no_text"
REASON_DISABLED = "eyes_disabled"

#: Forward-looking describe route (docs/SPEC.md §8): once the LiteLLM
#: router ships a ``fleet/vision`` alias, pinning ``EYES_DESCRIBE_ENGINE``
#: to it should reach ``cloud-vlm`` through the proxy. Today it is a stub —
#: see ``resolve_fleet_vision_alias``.
FLEET_VISION_ALIAS = "fleet/vision"
FLEET_VISION_MODEL_ENV = "FLEET_VISION_MODEL"


class EyesError(RuntimeError):
    """Fail-closed error carrying a snake_case reason (mirrors ProviderError)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------- config ---

TASKS = ("transcribe", "describe")
DEFAULT_ALLOWED_KINDS = ("screenshot", "image", "pdf")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _default_scratch_dir() -> str:
    """Decoded bytes land in the system temp dir, never in the checkout.

    No absolute path is baked into the tree (AGENTS.md law 3); override with
    ``EYES_SCRATCH_DIR`` if you want a repo-local, gitignored scratch dir.
    """
    return os.environ.get("EYES_SCRATCH_DIR") or os.path.join(
        tempfile.gettempdir(), "pet-talk-eyes"
    )


@dataclass
class EyesConfig:
    """Everything tunable about the eyes lane. Construction does no I/O."""

    max_bytes: int = field(default_factory=lambda: _env_int("EYES_MAX_BYTES", 8 * 1024 * 1024))
    allowed_kinds: tuple[str, ...] = DEFAULT_ALLOWED_KINDS
    engine: str = field(default_factory=lambda: os.environ.get("EYES_ENGINE", "zrv").strip() or "zrv")
    #: zrv ``--engine`` pin for ``transcribe`` (None => let zrv resolve).
    engine_pin: str | None = field(default_factory=lambda: os.environ.get("EYES_ENGINE_PIN") or None)
    #: zrv ``--engine`` pin for ``describe``. Unset by default ON PURPOSE:
    #: zrv's local default (``apple-vision``) refuses ``describe`` and says so
    #: in ~0.2s, which is a fast, honest ``eyes_ocr_failed``. The only local
    #: describe engine on this box, ``apple-fm``, was MEASURED at 101.9s for
    #: one 2280x600 PNG (2026-09-12) — 12x the 8s bound and 68x the Phase 4
    #: 1.5s gate. Opt in with ``EYES_DESCRIBE_ENGINE=apple-fm`` plus a much
    #: larger ``EYES_TIMEOUT_S`` if you really want it.
    describe_engine_pin: str | None = field(
        default_factory=lambda: os.environ.get("EYES_DESCRIBE_ENGINE") or None
    )
    char_cap: int = field(default_factory=lambda: _env_int("EYES_CHAR_CAP", 4000))
    #: Cap on the tagged context line queued for the NEXT turn's prompt.
    #: Separate from ``char_cap`` above: that one bounds the ``eyes.text``
    #: wire frame, this one bounds what actually lands in the LLM's system
    #: prompt (server/turn.py), so a long OCR cannot eat the prompt even
    #: when a bigger ``eyes.text`` frame is allowed.
    context_max_chars: int = field(
        default_factory=lambda: _env_int("PET_TALK_EYES_CONTEXT_MAX_CHARS", 1200)
    )
    timeout_s: float = field(default_factory=lambda: _env_float("EYES_TIMEOUT_S", 8.0))
    zrv_bin: str = field(default_factory=lambda: os.environ.get("EYES_ZRV_BIN", "zrv"))
    scratch_dir: str = field(default_factory=_default_scratch_dir)
    #: WAVE3 §1.1 — PDFs are read front-to-back, first N pages only.
    pdf_max_pages: int = 5
    #: Task when neither the frame nor the persona names one. ``transcribe``
    #: per WAVE3 §1.2, and because ``describe`` needs a VLM (see above).
    default_task: str = field(
        default_factory=lambda: (os.environ.get("EYES_DEFAULT_TASK") or "transcribe").strip()
    )

    def pin_for(self, task: str) -> str | None:
        return self.describe_engine_pin if task == "describe" else self.engine_pin


# --------------------------------------------------------------- engines ---


class OcrEngine:
    """Abstract OCR tyre. ``ocr`` is async; the caller applies the timeout."""

    name = "base"

    def preflight(self) -> None:
        """Raise ``EyesError(eyes_disabled, …)`` when this engine cannot run."""
        raise NotImplementedError

    async def ocr(self, path: str, task: str, cfg: EyesConfig) -> str:
        raise NotImplementedError


class ZrvEngine(OcrEngine):
    """Shells out to the real ``zrv`` CLI (zero-vision).

    Verified flags (``zrv --help`` / ``zrv ocr --help``, 2026-09-12)::

        zrv ocr <file> [--engine e] [--task transcribe|describe]
                       [--mode scene|interval|all-idr] [--interval s]
                       [--max-frames n]

    ``--json`` makes the CLI print one object:
    ``{"ok":bool,"engine":str,"task":str,"text":str,"ms":int,"error"?:str}``.
    Valid ``--engine`` ids: ``apple-vision``, ``apple-fm``, ``local-vlm``,
    ``cloud-vlm``, ``tesseract``. ``apple-vision`` is the local default on
    Apple-silicon macOS and is OCR-only: it refuses ``--task describe``.
    """

    name = "zrv"

    def __init__(self, cfg: EyesConfig | None = None) -> None:
        self._bin = (cfg or EyesConfig()).zrv_bin

    def resolve_bin(self) -> str | None:
        if os.path.sep in self._bin:
            return self._bin if os.access(self._bin, os.X_OK) else None
        return shutil.which(self._bin)

    def preflight(self) -> None:
        if self.resolve_bin() is None:
            raise EyesError(REASON_DISABLED, f"zrv_binary_missing:{self._bin}")

    def argv(self, path: str, task: str, cfg: EyesConfig) -> list[str]:
        exe = self.resolve_bin() or self._bin
        argv = [exe, "ocr", path, "--task", task, "--json"]
        pin = cfg.pin_for(task)
        if pin:
            argv += ["--engine", pin]
        return argv

    async def ocr(self, path: str, task: str, cfg: EyesConfig) -> str:
        import json

        argv = self.argv(path, task, cfg)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, FileNotFoundError) as e:
            raise EyesError(REASON_DISABLED, f"zrv_spawn_failed:{e}")
        try:
            out, err = await proc.communicate()
        except asyncio.CancelledError:
            # Timeout cancels us: never leave an orphan OCR process behind.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise
        stdout = (out or b"").decode("utf-8", "replace").strip()
        stderr = (err or b"").decode("utf-8", "replace").strip()
        payload: dict = {}
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    payload = json.loads(line)
                except ValueError:
                    payload = {}
                break
        if payload:
            if not payload.get("ok"):
                raise EyesError(REASON_OCR_FAILED, str(payload.get("error") or "zrv_not_ok"))
            return str(payload.get("text") or "")
        if proc.returncode != 0:
            raise EyesError(REASON_OCR_FAILED, stderr or f"zrv_exit_{proc.returncode}")
        # Older zrv builds print bare text instead of JSON.
        return stdout


class StubEngine(OcrEngine):
    """Deterministic canned engine for tests. Never touches the filesystem."""

    name = "stub"

    def __init__(self, text: str = "STUB OCR TEXT", delay_s: float = 0.0, available: bool = True) -> None:
        self.text = text
        self.delay_s = delay_s
        self.available = available
        self.calls: list[tuple[str, str]] = []

    def preflight(self) -> None:
        if not self.available:
            raise EyesError(REASON_DISABLED, "stub_engine_unavailable")

    async def ocr(self, path: str, task: str, cfg: EyesConfig) -> str:
        self.calls.append((path, task))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return self.text


def resolve_fleet_vision_alias(pin: str | None) -> str | None:
    """Resolve the forward-looking ``fleet/vision`` describe pin.

    Stub: the LiteLLM router does not serve a ``fleet/vision`` route yet
    (docs/SPEC.md §8 — the intended target is ``cloud-vlm`` through the
    proxy). Any other pin passes through untouched. ``fleet/vision`` itself
    resolves via ``FLEET_VISION_MODEL``; unset, it fails closed with the
    same ``eyes_disabled`` reason the describe-gate already uses, naming
    the var — never a silent fall-through to zrv's local default engine.
    """
    if pin != FLEET_VISION_ALIAS:
        return pin
    resolved = os.environ.get(FLEET_VISION_MODEL_ENV)
    if not resolved:
        raise EyesError(REASON_DISABLED, f"fleet_vision_model_unset:{FLEET_VISION_MODEL_ENV}")
    return resolved


def make_engine(name: str | None = None, cfg: EyesConfig | None = None) -> OcrEngine:
    """Engine tyre factory. Unknown names fail closed (no silent fallback)."""
    cfg = cfg or EyesConfig()
    name = (name or cfg.engine or "zrv").strip()
    if name == "zrv":
        return ZrvEngine(cfg)
    if name == "stub":
        return StubEngine()
    raise EyesError(REASON_DISABLED, f"unknown_eyes_engine:{name}")


# --------------------------------------------------------- persona tasks ---


def _persona_frontmatter_task(persona) -> str | None:
    """Read the optional ``eyes_task`` / ``eyes_default`` frontmatter key.

    ``server/persona.py`` is another lane's file, so the field is read here
    instead of added to ``Persona``: an attribute wins if some later version
    of the dataclass grows one, otherwise the persona's markdown is parsed
    with persona.py's own frontmatter parser. Absent key => ``None``.
    """
    for attr in ("eyes_task", "eyes_default"):
        value = getattr(persona, attr, None)
        if isinstance(value, str) and value in TASKS:
            return value
    name = getattr(persona, "name", "") or ""
    if not name:
        return None
    try:
        from server.persona import PERSONAS_DIR, _parse_frontmatter
    except Exception as e:
        swallowed("eyes_persona_module_unavailable", e)
        return None
    path = os.path.join(PERSONAS_DIR, f"{name}.md")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            meta, _body = _parse_frontmatter(f.read())
    except Exception as e:
        swallowed("eyes_persona_frontmatter_unreadable", e, persona=name)
        return None
    for key in ("eyes_task", "eyes_default"):
        value = meta.get(key)
        if isinstance(value, str) and value in TASKS:
            return value
    return None


def task_for(persona, kind: str, requested: str | None = None, cfg: EyesConfig | None = None) -> str:
    """Resolve the OCR task. Precedence: frame > persona > context > default.

    * An explicit, valid ``task`` on the ``user.attach`` frame always wins.
    * A PDF is text by definition — always ``transcribe``.
    * Persona frontmatter ``eyes_task`` (or WAVE3's ``eyes_default``) next.
    * Dictation context (``PET_TALK_DICTATION=1``) => ``transcribe``.
    * Otherwise ``EyesConfig.default_task`` (``transcribe``; set
      ``EYES_DEFAULT_TASK=describe`` or persona frontmatter
      ``eyes_task: describe`` for a conversational scene summary — read the
      ``describe_engine_pin`` note first, it is not on the latency budget).
    """
    cfg = cfg or EyesConfig()
    if isinstance(requested, str) and requested in TASKS:
        return requested
    if kind == "pdf":
        return "transcribe"
    from_persona = _persona_frontmatter_task(persona)
    if from_persona:
        return from_persona
    if os.environ.get("PET_TALK_DICTATION") == "1":
        return "transcribe"
    return cfg.default_task if cfg.default_task in TASKS else "transcribe"


# -------------------------------------------------------------- provider ---


@dataclass
class Attachment:
    ref: str
    kind: str
    mime: str
    task: str
    path: str
    n_bytes: int
    filename: str = ""

    @property
    def source(self) -> str:
        """WAVE3 §1.2 context tag prefix: ``eyes:att-1:screenshot``."""
        return f"eyes:{self.ref}:{self.kind}"


class EyesProvider:
    """Stage attachment bytes, then resolve them to text.

    ``stage()`` validates and writes the decoded bytes to the scratch dir;
    ``resolve(ref)`` runs the OCR engine and returns ``(task, text)`` per
    WAVE3 §1.2. Temp files are deleted as soon as they are read.
    """

    def __init__(self, cfg: EyesConfig | None = None, engine: OcrEngine | None = None) -> None:
        self.cfg = cfg or EyesConfig()
        self.engine = engine or make_engine(self.cfg.engine, self.cfg)
        self._staged: dict[str, Attachment] = {}
        #: Tagged context lines awaiting injection into the NEXT turn's
        #: prompt (server/turn.py drains this via ``take_context``). Each
        #: entry is ``(tagged_line, truncated)``. An attachment's line is
        #: queued exactly once, at resolve time, and consumed exactly once.
        self._pending_context: list[tuple[str, bool]] = []

    # -- validation -------------------------------------------------------

    def validate(self, frame_in: dict) -> tuple[str, str, str, str]:
        """Return ``(ref, kind, mime, b64)`` or raise ``EyesError``."""
        ref = str(frame_in.get("ref") or "att-1")
        kind = str(frame_in.get("kind") or "").strip().lower()
        if kind not in self.cfg.allowed_kinds:
            raise EyesError(REASON_BAD_KIND, kind or "missing_kind")
        mime = str(frame_in.get("mime") or "").strip()
        # WAVE3 calls the field bytes_b64; the lane contract calls it b64.
        b64 = frame_in.get("b64") or frame_in.get("bytes_b64") or ""
        if not isinstance(b64, str) or not b64:
            raise EyesError(REASON_BAD_KIND, "missing_b64")
        # Cheap length check first: never materialise an oversize payload.
        if (len(b64) * 3) // 4 > self.cfg.max_bytes:
            raise EyesError(REASON_TOO_LARGE, f"{(len(b64) * 3) // 4}>{self.cfg.max_bytes}")
        return ref, kind, mime, b64

    # -- staging ----------------------------------------------------------

    def stage(self, frame_in: dict, persona=None) -> Attachment:
        ref, kind, mime, b64 = self.validate(frame_in)
        try:
            raw = base64.b64decode(b64, validate=False)
        except (binascii.Error, ValueError) as e:
            raise EyesError(REASON_BAD_KIND, f"b64_decode_failed:{e}")
        if not raw:
            raise EyesError(REASON_BAD_KIND, "empty_payload")
        if len(raw) > self.cfg.max_bytes:
            raise EyesError(REASON_TOO_LARGE, f"{len(raw)}>{self.cfg.max_bytes}")
        task = task_for(persona, kind, frame_in.get("task"), self.cfg)
        suffix = _suffix_for(kind, mime, str(frame_in.get("filename") or ""))
        try:
            os.makedirs(self.cfg.scratch_dir, exist_ok=True)
            path = os.path.join(self.cfg.scratch_dir, f"{ref}-{uuid.uuid4().hex[:8]}{suffix}")
            with open(path, "wb") as f:
                f.write(raw)
        except OSError as e:
            raise EyesError(REASON_OCR_FAILED, f"scratch_write_failed:{e}")
        att = Attachment(
            ref=ref,
            kind=kind,
            mime=mime,
            task=task,
            path=path,
            n_bytes=len(raw),
            filename=str(frame_in.get("filename") or ""),
        )
        self._staged[ref] = att
        return att

    def get(self, ref: str) -> Attachment:
        att = self._staged.get(ref)
        if att is None:
            raise EyesError(REASON_BAD_KIND, f"unknown_ref:{ref}")
        return att

    def discard(self, ref: str) -> None:
        att = self._staged.pop(ref, None)
        if att is None:
            return
        try:
            os.remove(att.path)
        except OSError:
            pass

    # -- resolve ----------------------------------------------------------

    async def resolve(self, ref: str) -> tuple[str, str]:
        """WAVE3 §1.2 — ``(task, text)`` for a staged ref. Deletes the temp file."""
        att = self.get(ref)
        try:
            if att.task == "describe":
                if not self.cfg.describe_engine_pin:
                    # Fail fast and name the knob (AGENTS.md law 1): describe
                    # needs a VLM another lane is landing in zero-vision
                    # (cloud-vlm / local-vlm); with no EYES_DESCRIBE_ENGINE
                    # set, this must never fall through to zrv's own default
                    # engine (apple-vision, which refuses --task describe
                    # with its own unrelated error) or hang on apple-fm's
                    # 100s+ latency.
                    raise EyesError(REASON_DISABLED, "describe_engine_unset:EYES_DESCRIBE_ENGINE")
                # Resolve the fleet/vision stub alias in place, once, so the
                # engine below sees a real zrv --engine id either way.
                self.cfg.describe_engine_pin = resolve_fleet_vision_alias(
                    self.cfg.describe_engine_pin
                )
            self.engine.preflight()
            try:
                text = await asyncio.wait_for(
                    self.engine.ocr(att.path, att.task, self.cfg), timeout=self.cfg.timeout_s
                )
            except asyncio.TimeoutError:
                raise EyesError(REASON_OCR_FAILED, "timeout")
            text = (text or "").strip()
            if not text:
                raise EyesError(REASON_NO_TEXT, f"{self.engine.name}:{att.task}")
            return att.task, text
        finally:
            self.discard(ref)

    def cap(self, text: str) -> tuple[str, bool]:
        if len(text) <= self.cfg.char_cap:
            return text, False
        return text[: self.cfg.char_cap], True

    def context_tag(self, att: Attachment, text: str) -> str:
        """Turn-context injection shape: ``[eyes:att-1:screenshot|describe] …``."""
        return f"[{att.source}|{att.task}] {text}"

    def cap_context(self, tag: str) -> tuple[str, bool]:
        """Bound one tagged context line to ``PET_TALK_EYES_CONTEXT_MAX_CHARS``.

        Independent of :meth:`cap`, which bounds the outgoing ``eyes.text``
        frame — a client may be shown more than the prompt ever sees.
        """
        if len(tag) <= self.cfg.context_max_chars:
            return tag, False
        return tag[: self.cfg.context_max_chars], True

    def queue_context(self, att: Attachment, text: str) -> tuple[str, bool]:
        """Queue this attachment's tagged line for the NEXT turn's prompt.

        ``server/turn.py`` drains the queue once per turn via
        :meth:`take_context` — so an attachment's OCR text reaches exactly
        one turn, never a later one.
        """
        capped, truncated = self.cap_context(self.context_tag(att, text))
        self._pending_context.append((capped, truncated))
        return capped, truncated

    def take_context(self) -> list[tuple[str, bool]]:
        """Drain and clear pending context lines. Consumed by exactly one turn."""
        pending, self._pending_context = self._pending_context, []
        return pending


# --------------------------------------------------------------- helpers ---

_SUFFIX_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/tiff": ".tiff",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
}


def _suffix_for(kind: str, mime: str, filename: str) -> str:
    suffix = _SUFFIX_BY_MIME.get(mime.lower())
    if suffix:
        return suffix
    ext = os.path.splitext(filename)[1].lower()
    if ext and len(ext) <= 6 and ext.isascii():
        return ext
    return ".pdf" if kind == "pdf" else ".png"


def _fallback_frame(ftype: str, turn_id: str, **fields) -> dict:
    if not turn_id:
        raise EyesError(REASON_BAD_KIND, "frame_no_turn_id")
    return {"type": ftype, "turn_id": turn_id, **fields}


def _sender_for(ws):
    """Lazy-import app.py's frame helpers; fall back to a local builder.

    Importing ``server.app`` at module scope would be an import cycle (app.py
    imports this module), so it happens here, per call, and is optional.
    """
    try:
        from server.app import _safe_send_json, frame  # type: ignore
    except Exception:
        frame = _fallback_frame  # type: ignore[assignment]

        async def _safe_send_json(sock, payload):  # type: ignore[misc]
            try:
                await sock.send_json(payload)
                return True
            except Exception:
                return False

    async def send(payload: dict) -> bool:
        return bool(await _safe_send_json(ws, payload))

    return send, frame


# ------------------------------------------------------------ entrypoint ---

#: Process-wide provider, built lazily so import stays cheap and I/O-free.
_provider: EyesProvider | None = None


def get_provider(cfg: EyesConfig | None = None) -> EyesProvider:
    global _provider
    if _provider is None or cfg is not None:
        _provider = EyesProvider(cfg)
    return _provider


def reset_provider() -> None:
    """Test hook: drop the cached provider so env changes take effect."""
    global _provider
    _provider = None


async def handle_attach(ws, frame_in: dict, persona=None, *, send=None, provider=None) -> None:
    """Handle one ``user.attach`` frame. Never raises.

    Emits ``eyes.received`` as soon as the payload is accepted, then
    ``eyes.text``. Any failure emits ``agent.error`` with one of
    ``eyes_bad_kind`` / ``eyes_too_large`` / ``eyes_disabled`` /
    ``eyes_ocr_failed`` / ``eyes_no_text``.

    ``send`` (async callable taking one dict) overrides the WS send path and
    is how the unit tests run with no socket at all.
    """
    turn_id = str(frame_in.get("turn_id") or "") or "eyes-0"
    if send is None:
        sender, build = _sender_for(ws)
    else:
        sender, build = send, _fallback_frame

    async def fail(reason: str, detail: str = "", ref: str = "") -> None:
        fields: dict = {"reason": reason}
        if detail:
            fields["detail"] = detail
        if ref:
            fields["ref"] = ref
        await sender(build("agent.error", turn_id, **fields))

    try:
        prov = provider if provider is not None else get_provider()
    except EyesError as e:
        await fail(e.reason, e.detail)
        return

    ref = str(frame_in.get("ref") or "att-1")
    try:
        # Fail closed BEFORE acking: a missing engine must never look accepted.
        prov.engine.preflight()
        att = prov.stage(frame_in, persona)
    except EyesError as e:
        await fail(e.reason, e.detail, ref)
        return
    except Exception as e:  # pragma: no cover - defensive, loop must stay up
        await fail(REASON_OCR_FAILED, f"stage_exception:{type(e).__name__}", ref)
        return

    await sender(
        build(
            "eyes.received",
            turn_id,
            ref=att.ref,
            kind=att.kind,
            task=att.task,
            bytes=att.n_bytes,
        )
    )

    try:
        task, text = await prov.resolve(att.ref)
    except EyesError as e:
        await fail(e.reason, e.detail, att.ref)
        return
    except Exception as e:  # pragma: no cover - defensive
        await fail(REASON_OCR_FAILED, f"resolve_exception:{type(e).__name__}", att.ref)
        return

    # Queue the tagged line for the NEXT turn's prompt (server/turn.py) —
    # its own cap (PET_TALK_EYES_CONTEXT_MAX_CHARS), independent of the
    # eyes.text frame's cap below.
    prov.queue_context(att, text)

    capped, truncated = prov.cap(text)
    await sender(
        build(
            "eyes.text",
            turn_id,
            ref=att.ref,
            source=att.source,
            kind=att.kind,
            task=task,
            engine=prov.engine.name,
            text=capped,
            truncated=truncated,
        )
    )
