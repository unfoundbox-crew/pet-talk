"""pet-talk humanizer package: section 8.2 preprocessor + 8.3 backchannel gate.

Stdlib only.
"""

from .humanize import perform, DEFAULTS
from .backchannel import (
    should_backchannel,
    choose_micro,
    MICRO_VOCAB,
    MIN_CONTINUOUS_SPEECH_MS,
    MIN_EMIT_GAP_MS,
    MIN_USER_PAUSE_MS,
)

__all__ = [
    "perform",
    "DEFAULTS",
    "should_backchannel",
    "choose_micro",
    "MICRO_VOCAB",
    "MIN_CONTINUOUS_SPEECH_MS",
    "MIN_EMIT_GAP_MS",
    "MIN_USER_PAUSE_MS",
]
