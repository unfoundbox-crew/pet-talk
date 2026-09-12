#!/usr/bin/env python3
"""qa/benchmarks/voice_matrix.py — measured voice matrix for every TTS provider
constructible from Doppler.

Runs the same 12-word sentence through every reachable (provider, voice) pair,
N times each, and reports first_audio_ms, total_synth_ms (p50, min, max), wav
bytes, audio duration (via pcm_duration_ms), and real-time factor. Providers
missing a key print SKIP with a named reason; providers that error print the
ProviderError reason verbatim. No fake numbers — anything unmeasured prints
NOT MEASURED.

Interpreter: MUST be run with ~/miniconda3/envs/local-ml-py311/bin/python —
kokoro-local needs mlx-audio + misaki[en], which live only in that env.

Usage:
    ~/miniconda3/envs/local-ml-py311/bin/python qa/benchmarks/voice_matrix.py \
        [--providers kokoro-local,deepgram] [--voices af_heart,af_bella] \
        [--n 3] [--json out.json]

Secrets come from the environment this process is launched with — run it
under `doppler run --project unfoundbox --config dev_personal -- <cmd>` to
populate DEEPGRAM_API_KEY / SMALLEST_API_KEY / ELEVENLABS_API_KEY. This script
never prints a secret value; it only checks presence.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from typing import Any, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import io  # noqa: E402
import wave  # noqa: E402

from server.providers._shared import ProviderError, pcm_duration_ms  # noqa: E402


def safe_duration_ms(wav_bytes: bytes) -> tuple[int, bool]:
    """Duration in ms, preferring the actual audio byte count over the RIFF
    header's declared chunk size, plus whether the header was untrustworthy.

    Deepgram's `speak` endpoint (measured 2026-09-12) writes a streaming
    placeholder into the RIFF/`data` chunk-size fields (observed:
    `data` size = 0x7fff0000) rather than patching in the real size once
    the stream ends. `wave.open(...).getnframes()` trusts that field, so
    `pcm_duration_ms` (which trusts `wave`) reports ~44,700,000ms for a
    ~5s clip. This recomputes duration from the real byte count of the
    `data` chunk's payload instead, and flags when the header value was
    off by more than 2x from that recomputation.
    """
    header_ms = pcm_duration_ms(wav_bytes)
    if not wav_bytes:
        return 0, False
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            rate = w.getframerate() or 1
            channels = w.getnchannels() or 1
            sampwidth = w.getsampwidth() or 2
    except (wave.Error, EOFError, OSError):
        return header_ms, False
    data_idx = wav_bytes.find(b"data")
    if data_idx == -1:
        return header_ms, False
    actual_payload_bytes = len(wav_bytes) - (data_idx + 8)
    frame_size = max(channels * sampwidth, 1)
    frames = actual_payload_bytes / frame_size
    byte_derived_ms = int(frames * 1000 / rate)
    header_suspect = header_ms > 0 and byte_derived_ms > 0 and (
        header_ms > byte_derived_ms * 2 or byte_derived_ms > header_ms * 2
    )
    return (byte_derived_ms if header_suspect else header_ms), header_suspect
from server.providers.tts import (  # noqa: E402
    DeepgramTTS,
    ElevenLabsTTS,
    KokoroLocalTTS,
    SmallestAITTS,
    StubTTS,
)

SENTENCE = "The weather today is bright and clear so the afternoon looks genuinely pleasant"
_actual_word_count = len(SENTENCE.split())
if _actual_word_count != 12:
    # The brief that specified this exact sentence also claimed it was
    # 12 words; a plain split() count says otherwise. Flagging loudly
    # instead of silently editing the sentence or crashing the run.
    print(
        f"WARNING: SENTENCE has {_actual_word_count} words, not the 12 the "
        "brief named. Sentence text used verbatim as specified; word count "
        "here is measured, not asserted.",
        file=sys.stderr,
    )

# kokoro-local voice enumeration is done at runtime from the HF cache (see
# find_kokoro_voice_dir / list_kokoro_voices below) — no hardcoded list here.
KOKORO_BRITISH_FALLBACK = ["bf_emma", "bm_george"]
KOKORO_AMERICAN_SUBSET = ["af_heart", "af_bella", "af_nicole", "af_sarah", "am_adam", "am_michael"]

DEEPGRAM_VOICES = ["aura-2-thalia-en", "aura-2-andromeda-en", "aura-asteria-en", "aura-luna-en"]
SMALLEST_VOICES = ["meher", "emily", "radha"]
ELEVENLABS_DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"  # Rachel


def find_kokoro_voice_dir() -> Optional[str]:
    """Search the HF cache for the Kokoro-82M snapshot's voices/ directory.
    Checks prince-canuma first (what tts.py's KokoroLocalTTS.DEFAULT_MODEL
    uses), then hexgrad as a fallback."""
    hub = os.path.expanduser("~/.cache/huggingface/hub")
    if not os.path.isdir(hub):
        return None
    for repo_dirname in ("models--prince-canuma--Kokoro-82M", "models--hexgrad--Kokoro-82M"):
        repo_path = os.path.join(hub, repo_dirname, "snapshots")
        if not os.path.isdir(repo_path):
            continue
        for snap in sorted(os.listdir(repo_path)):
            voices_dir = os.path.join(repo_path, snap, "voices")
            if os.path.isdir(voices_dir):
                return voices_dir
    return None


def list_kokoro_voices() -> tuple[list[str], Optional[str]]:
    """Returns (voice_names_sorted, path_found). voice_names are bare names
    with the file extension stripped (e.g. 'af_heart')."""
    voices_dir = find_kokoro_voice_dir()
    if not voices_dir:
        return [], None
    names = []
    for fname in os.listdir(voices_dir):
        base, ext = os.path.splitext(fname)
        if ext in (".pt", ".safetensors", ".npz"):
            names.append(base)
    return sorted(names), voices_dir


def pick_kokoro_subset(all_voices: list[str]) -> list[str]:
    af_am = sorted(v for v in all_voices if v.startswith("af_") or v.startswith("am_"))
    if len(af_am) <= 10:
        return af_am
    subset = [v for v in KOKORO_AMERICAN_SUBSET if v in all_voices]
    for brit in KOKORO_BRITISH_FALLBACK:
        if brit in all_voices:
            subset.append(brit)
    # If the exact fallback names aren't present, grab any bf_/bm_ voices.
    if not any(v.startswith(("bf_", "bm_")) for v in subset):
        for v in sorted(all_voices):
            if v.startswith(("bf_", "bm_")):
                subset.append(v)
                if sum(1 for s in subset if s.startswith(("bf_", "bm_"))) >= 2:
                    break
    return subset


def fetch_elevenlabs_second_voice(api_key: str) -> Optional[str]:
    """Best-effort GET of /v1/voices to name a second voice id. Returns None
    (never raises) on any failure — caller just measures one voice then."""
    try:
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/voices",
            headers={"xi-api-key": api_key, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        for v in data.get("voices", []):
            vid = v.get("voice_id", "")
            if vid and vid != ELEVENLABS_DEFAULT_VOICE:
                return vid
    except Exception:
        return None
    return None


def measure(
    provider_name: str,
    voice: str,
    synth_fn,
    n: int,
    warm_up: bool,
) -> dict[str, Any]:
    """Run synth_fn(text) n times (plus one untimed warm-up if requested),
    returning the measured row. synth_fn takes no args besides the closed-over
    voice; raises ProviderError on failure."""
    row: dict[str, Any] = {
        "provider": provider_name,
        "voice": voice,
        "date": "2026-09-12",
    }
    if warm_up:
        try:
            synth_fn()
        except ProviderError as e:
            row["status"] = "ERROR"
            row["reason"] = e.reason
            row["detail"] = e.detail
            return row
        except Exception as e:
            row["status"] = "ERROR"
            row["reason"] = "unexpected_exception"
            row["detail"] = str(e)
            return row

    totals_ms: list[float] = []
    wav_bytes_list: list[int] = []
    duration_ms_list: list[int] = []
    header_suspect_any = False
    last_wav: bytes = b""
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            wav, word_times = synth_fn()
        except ProviderError as e:
            row["status"] = "ERROR"
            row["reason"] = e.reason
            row["detail"] = e.detail
            return row
        except Exception as e:
            row["status"] = "ERROR"
            row["reason"] = "unexpected_exception"
            row["detail"] = str(e)
            return row
        t1 = time.perf_counter()
        total_ms = (t1 - t0) * 1000.0
        totals_ms.append(total_ms)
        wav_bytes_list.append(len(wav))
        dur_ms, header_suspect = safe_duration_ms(wav)
        duration_ms_list.append(dur_ms)
        header_suspect_any = header_suspect_any or header_suspect
        last_wav = wav
        last_word_times = word_times

    row["status"] = "OK"
    row["n"] = n
    row["total_synth_ms_p50"] = round(statistics.median(totals_ms), 1)
    row["total_synth_ms_min"] = round(min(totals_ms), 1)
    row["total_synth_ms_max"] = round(max(totals_ms), 1)
    # Every measured provider here returns one complete WAV per call (no
    # streaming / chunked-first-audio path exists in this codebase's tts.py),
    # so first_audio_ms == total_synth_ms. Named explicitly, not invented.
    row["first_audio_ms"] = row["total_synth_ms_p50"]
    row["first_audio_ms_note"] = "whole-WAV provider: first_audio_ms == total_synth_ms"
    row["wav_bytes"] = wav_bytes_list[-1]
    row["duration_ms"] = duration_ms_list[-1]
    if header_suspect_any:
        row["duration_ms_note"] = (
            "RIFF header chunk size was implausible (e.g. a streaming "
            "placeholder); duration recomputed from actual data-chunk byte "
            "count instead of trusting the header"
        )
    if row["duration_ms"] > 0:
        row["rtf"] = round(row["total_synth_ms_p50"] / row["duration_ms"], 4)
    else:
        row["rtf"] = None
        row["rtf_note"] = "NOT MEASURED (duration_ms was 0 — audio was likely not a WAV container, e.g. mp3 bytes)"
    word_times_estimated = bool(last_word_times) and all(w.get("estimated") for w in last_word_times)
    word_times_real = bool(last_word_times) and any(not w.get("estimated") for w in last_word_times)
    if not last_word_times:
        row["word_times"] = "NOT MEASURED (empty)"
    elif word_times_real:
        row["word_times"] = "real"
    elif word_times_estimated:
        row["word_times"] = "estimated"
    else:
        row["word_times"] = "mixed"
    return row


def run_kokoro_local(voices: list[str], n: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        tts = KokoroLocalTTS(warm=False)  # we do our own explicit warm-up per voice below
    except Exception as e:
        return [{"provider": "kokoro-local", "voice": v, "status": "ERROR",
                  "reason": "construct_failed", "detail": str(e), "date": "2026-09-12"} for v in voices]
    for v in voices:
        def synth_fn(voice=v):
            return tts.synth(SENTENCE, voice=voice, speed=1.0)
        row = measure("kokoro-local", v, synth_fn, n, warm_up=True)
        rows.append(row)
    return rows


def run_deepgram(voices: list[str], n: int) -> list[dict[str, Any]]:
    key = os.environ.get("DEEPGRAM_API_KEY", "")
    if not key:
        return [{"provider": "deepgram", "voice": v, "status": "SKIP",
                  "reason": "DEEPGRAM_API_KEY not set", "date": "2026-09-12"} for v in voices]
    rows = []
    for v in voices:
        tts = DeepgramTTS(model=v)
        def synth_fn(t=tts):
            return t.synth(SENTENCE, voice=v, speed=1.0)
        row = measure("deepgram", v, synth_fn, n, warm_up=False)
        rows.append(row)
    return rows


def run_smallest(voices: list[str], n: int) -> list[dict[str, Any]]:
    key = os.environ.get("SMALLEST_API_KEY", "")
    if not key:
        return [{"provider": "smallest", "voice": v, "status": "SKIP",
                  "reason": "SMALLEST_API_KEY not set", "date": "2026-09-12"} for v in voices]
    rows = []
    for v in voices:
        tts = SmallestAITTS(api_key=key, voice_id=v)
        def synth_fn(t=tts, voice=v):
            return t.synth(SENTENCE, voice=voice, speed=1.0)
        row = measure("smallest", v, synth_fn, n, warm_up=False)
        rows.append(row)
    return rows


def run_elevenlabs(voices: list[str], n: int) -> list[dict[str, Any]]:
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not key:
        return [{"provider": "elevenlabs", "voice": v, "status": "SKIP",
                  "reason": "ELEVENLABS_API_KEY not set", "date": "2026-09-12"} for v in voices]
    rows = []
    for v in voices:
        tts = ElevenLabsTTS(voice_id=v)
        def synth_fn(t=tts, voice=v):
            return t.synth(SENTENCE, voice=voice, speed=1.0)
        row = measure("elevenlabs", v, synth_fn, n, warm_up=False)
        rows.append(row)
    return rows


def run_stub(n: int) -> list[dict[str, Any]]:
    tts = StubTTS()
    def synth_fn():
        return tts.synth(SENTENCE, voice="af_heart", speed=1.0)
    return [measure("stub", "af_heart", synth_fn, n, warm_up=True)]


def print_table(rows: list[dict[str, Any]]) -> None:
    headers = ["provider", "voice", "status", "first_ms", "p50_ms", "min_ms", "max_ms",
               "wav_bytes", "dur_ms", "rtf", "word_times", "note"]
    widths = {h: len(h) for h in headers}
    display_rows = []
    for r in rows:
        if r.get("status") == "OK":
            d = {
                "provider": r["provider"], "voice": r["voice"], "status": "OK",
                "first_ms": f"{r['first_audio_ms']:.1f}",
                "p50_ms": f"{r['total_synth_ms_p50']:.1f}",
                "min_ms": f"{r['total_synth_ms_min']:.1f}",
                "max_ms": f"{r['total_synth_ms_max']:.1f}",
                "wav_bytes": str(r["wav_bytes"]),
                "dur_ms": str(r["duration_ms"]),
                "rtf": f"{r['rtf']:.4f}" if r["rtf"] is not None else "NOT MEASURED",
                "word_times": r["word_times"],
                "note": "header-size-suspect(recomputed from bytes)" if r.get("duration_ms_note") else "",
            }
        elif r.get("status") == "SKIP":
            d = {"provider": r["provider"], "voice": r["voice"], "status": "SKIP",
                 "first_ms": "", "p50_ms": "", "min_ms": "", "max_ms": "",
                 "wav_bytes": "", "dur_ms": "", "rtf": "", "word_times": "",
                 "note": f"reason={r.get('reason', '')}"}
        else:
            d = {"provider": r["provider"], "voice": r["voice"], "status": "ERROR",
                 "first_ms": "", "p50_ms": "", "min_ms": "", "max_ms": "",
                 "wav_bytes": "", "dur_ms": "", "rtf": "", "word_times": "",
                 "note": f"reason={r.get('reason', '')} detail={r.get('detail', '')[:60]}"}
        display_rows.append(d)
        for h in headers:
            widths[h] = max(widths[h], len(d[h]))

    def fmt_row(vals: dict[str, str]) -> str:
        return "  ".join(vals[h].ljust(widths[h]) for h in headers)

    print(fmt_row({h: h for h in headers}))
    print("  ".join("-" * widths[h] for h in headers))
    for d in display_rows:
        print(fmt_row(d))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", default="", help="comma-separated: kokoro-local,deepgram,smallest,elevenlabs,stub")
    ap.add_argument("--voices", default="", help="comma-separated voice filter, applies within each selected provider")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--json", default="", help="path to also write JSON output")
    args = ap.parse_args()

    provider_filter = set(args.providers.split(",")) if args.providers else None
    voice_filter = set(args.voices.split(",")) if args.voices else None

    def want(provider: str) -> bool:
        return provider_filter is None or provider in provider_filter

    def filt(voices: list[str]) -> list[str]:
        return [v for v in voices if voice_filter is None or v in voice_filter]

    all_rows: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"date": "2026-09-12", "sentence": SENTENCE, "n": args.n}

    if want("kokoro-local"):
        kokoro_all, kokoro_path = list_kokoro_voices()
        meta["kokoro_voice_dir"] = kokoro_path
        meta["kokoro_voices_found"] = kokoro_all
        if not kokoro_all:
            all_rows.append({"provider": "kokoro-local", "voice": "*", "status": "SKIP",
                              "reason": "no HF cache snapshot with voices/ dir found", "date": "2026-09-12"})
        else:
            subset = filt(pick_kokoro_subset(kokoro_all))
            all_rows.extend(run_kokoro_local(subset, args.n))

    if want("deepgram"):
        all_rows.extend(run_deepgram(filt(DEEPGRAM_VOICES), args.n))

    if want("smallest"):
        all_rows.extend(run_smallest(filt(SMALLEST_VOICES), args.n))

    if want("elevenlabs"):
        key = os.environ.get("ELEVENLABS_API_KEY", "")
        el_voices = [ELEVENLABS_DEFAULT_VOICE]
        if key:
            second = fetch_elevenlabs_second_voice(key)
            if second:
                el_voices.append(second)
        all_rows.extend(run_elevenlabs(filt(el_voices), args.n))

    if want("stub"):
        all_rows.extend(run_stub(args.n))

    print_table(all_rows)
    print()
    print(json.dumps({"meta": meta, "rows": all_rows}, indent=2))

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"meta": meta, "rows": all_rows}, f, indent=2)


if __name__ == "__main__":
    main()
