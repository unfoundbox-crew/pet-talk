"""Backchannel gate for pet-talk (TECH-SPEC section 8.3). Stdlib only.

Decides whether the agent may emit one Micro ("mmhmm" / "yeah" / "right")
while the user holds the floor. Pure function: no wall-clock reads, no
randomness — same input always gives same output.

Rules (section 8.3, strict readings — the spec says ">1.5s continuous"):
  1. continuous speech must EXCEED 1500ms (1500 holds, 1501 emits);
  2. last emit at least 8000ms ago, or never (None);
  3. current user pause at least 500ms (never during a <500ms pause);
  4. floor_holder must be 'user' (never steal the floor).

Calling conventions (both supported so the QA conformance test in
qa/test_humanizer_gates.py can call the entry point directly):
  dict form (coordinator contract):
    should_backchannel({"continuous_speech_ms": 1600,
                        "last_emit_ms_ago": None,
                        "current_pause_ms": 600,
                        "floor_holder": "user",
                        "now_ms": 8000})
    -> (emit: bool, reason: str); reason names the deciding rule, e.g.
       'emit:mmhmm:1600ms-continuous', 'hold:pause-200ms',
       'hold:floor-agent', 'hold:emitted-3000ms-ago'.
  positional form (QA derived interface):
    should_backchannel(continuous_speech_ms, ms_since_last_emit,
                       user_pause_ms, floor_held_by)
    -> (emit: bool, token: str | None); token on emit is one of
       MICRO_VOCAB, None on hold — mirroring the QA reference oracle.

Micro selection is seedable rotation, not random: index
(now_ms // 8000 + seed) over MICRO_VOCAB, so successive emits cycle
mmhmm -> yeah -> right -> ... and identical inputs pick the same micro.
"""

MICRO_VOCAB = ("mmhmm", "yeah", "right")

MIN_CONTINUOUS_SPEECH_MS = 1500
MIN_EMIT_GAP_MS = 8000
MIN_USER_PAUSE_MS = 500


def choose_micro(seed_value=0):
    """Deterministic rotation over MICRO_VOCAB. No randomness."""
    try:
        n = int(seed_value)
    except (TypeError, ValueError):
        n = 0
    return MICRO_VOCAB[n % len(MICRO_VOCAB)]


def _num(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _num_or_none(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_ms(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "?"
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return repr(f)


def _rotation_index(now_ms, seed):
    try:
        now = int(now_ms)
    except (TypeError, ValueError):
        now = 0
    try:
        s = int(seed)
    except (TypeError, ValueError):
        s = 0
    if now < 0:
        now = 0
    return (now // MIN_EMIT_GAP_MS) + s


def _decide(cont_ms, gap_ms_or_none, pause_ms, floor_holder, rotation_idx):
    """Core gate. Returns (emit, micro_or_None, reason)."""
    if not cont_ms > MIN_CONTINUOUS_SPEECH_MS:
        return False, None, "hold:continuous-%sms" % _fmt_ms(cont_ms)
    if gap_ms_or_none is not None and gap_ms_or_none < MIN_EMIT_GAP_MS:
        return False, None, "hold:emitted-%sms-ago" % _fmt_ms(gap_ms_or_none)
    if not pause_ms >= MIN_USER_PAUSE_MS:
        return False, None, "hold:pause-%sms" % _fmt_ms(pause_ms)
    if floor_holder != "user":
        return False, None, "hold:floor-%s" % (floor_holder,)
    micro = choose_micro(rotation_idx)
    return True, micro, "emit:%s:%sms-continuous" % (micro, _fmt_ms(cont_ms))


def should_backchannel(state_or_continuous_ms,
                       ms_since_last_emit=None,
                       user_pause_ms=None,
                       floor_held_by="user",
                       seed=0,
                       now_ms=0):
    """Backchannel gate (section 8.3). See module docstring for contracts."""
    if isinstance(state_or_continuous_ms, dict):
        state = state_or_continuous_ms
        cont = _num(state.get("continuous_speech_ms", 0), 0)
        gap = _num_or_none(state.get("last_emit_ms_ago", None))
        pause = _num(state.get("current_pause_ms", 0), 0)
        floor = state.get("floor_holder", "user")
        now = state.get("now_ms", now_ms)
        s = state.get("seed", seed)
        emit, _, reason = _decide(cont, gap, pause, floor,
                                  _rotation_index(now, s))
        return emit, reason
    cont = _num(state_or_continuous_ms, 0)
    gap = _num_or_none(ms_since_last_emit)
    pause = _num(user_pause_ms, 0)
    emit, micro, _ = _decide(cont, gap, pause, floor_held_by,
                             _rotation_index(now_ms, seed))
    if emit:
        return True, micro
    return False, None
