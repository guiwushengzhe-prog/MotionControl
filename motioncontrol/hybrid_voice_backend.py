from __future__ import annotations

import math
import queue
import threading
import time
from array import array
from collections import deque
from pathlib import Path

from motioncontrol.funasr_command_backend import FunAsrWorkerClient, build_hotwords
from motioncontrol.sherpa_wake_backend import SherpaWakeWordRecognizer
from motioncontrol_shared.text_norm import compact_text


DEFAULT_WAKE_WINDOW_SECONDS = 3.5
PRE_ROLL_SECONDS = 0.85
PROBE_DELAY_SECONDS = 0.60
END_SILENCE_SECONDS = 0.48
MAX_COMMAND_AFTER_SPEECH_SECONDS = 3.0



def resolve_command_candidate(
    recognized: str,
    wake_word: str,
    mappings: list[dict],
    emergency_phrases: list[str],
) -> str | None:
    """Map SeACo text back to a configured MotionControl command.

    No fuzzy edit-distance guessing is used.  A strong ASR model plus hotwords
    should produce a configured phrase or synonym; guessing a different command
    is more dangerous than rejecting one utterance in a game controller.
    """
    got = compact_text(recognized)
    wake = compact_text(wake_word)
    if wake and got.startswith(wake):
        got = got[len(wake):]
    if not got:
        return None

    for phrase in emergency_phrases:
        full = compact_text(phrase)
        spoken = full[len(wake):] if wake and full.startswith(wake) else full
        if got in {full, spoken}:
            return str(phrase)

    for mapping in mappings:
        canonical = str(mapping.get("phrase") or "").strip()
        for phrase in [canonical, *mapping.get("synonyms", [])]:
            if got == compact_text(phrase):
                return canonical
    return None


class HybridWakeAsrRecognizer:
    """Sherpa wake word + SeACoParaformer command recognizer.

    The 3M KWS model is intentionally restricted to one binary question:
    "did the user say the wake word?"  After wake-up we buffer one short
    utterance and ask the isolated 220M SeACoParaformer worker to recognize it
    with the configured MotionControl vocabulary as hotwords.
    """

    def __init__(
        self,
        root: Path,
        wake_model_path: Path,
        funasr_python: Path,
        command_model_path: Path,
        vad_model_path: Path,
        wake_word: str,
        mappings: list[dict],
        emergency_phrases: list[str],
        *,
        sample_rate: int = 16_000,
        wake_window_seconds: float = DEFAULT_WAKE_WINDOW_SECONDS,
        wake_recognizer=None,
        asr_client=None,
    ) -> None:
        self.root = Path(root)
        self.sample_rate = int(sample_rate)
        self.wake_word = str(wake_word or "体感").strip()
        self.mappings = list(mappings)
        self.emergency_phrases = list(emergency_phrases)
        self.wake_window_seconds = float(wake_window_seconds)
        self.mode = "sherpa-wake+funasr-seaco"
        self.hotwords = build_hotwords(self.wake_word, self.mappings, self.emergency_phrases)
        self.wake_recognizer = wake_recognizer or SherpaWakeWordRecognizer(
            wake_model_path, self.wake_word, sample_rate=self.sample_rate
        )
        self.asr_client = asr_client or FunAsrWorkerClient(
            self.root, funasr_python, command_model_path, vad_model_path
        )
        self.asr_client.start_async()

        self.state = "idle"
        self.wake_until = 0.0
        self.last_raw_text = ""
        self.last_latency_ms: float | None = None
        self.last_error: str | None = None
        self.command_audio_seconds = 0.0

        self._pre_roll: deque[bytes] = deque()
        self._pre_roll_bytes = 0
        self._pre_roll_limit = int(self.sample_rate * 2 * PRE_ROLL_SECONDS)
        self._command_parts: list[bytes] = []
        self._command_bytes = 0
        self._wake_at = 0.0
        self._speech_started_at = 0.0
        self._last_speech_at = 0.0
        self._noise_rms = 220.0
        self._probe_started = False
        self._final_started = False
        self._session = 0
        self._events: queue.Queue[dict] = queue.Queue()

    @staticmethod
    def _rms(pcm16: bytes) -> float:
        samples = array("h")
        samples.frombytes(pcm16)
        if not samples:
            return 0.0
        return math.sqrt(sum(float(x) * float(x) for x in samples) / len(samples))

    def _append_pre_roll(self, pcm16: bytes) -> None:
        self._pre_roll.append(pcm16)
        self._pre_roll_bytes += len(pcm16)
        while self._pre_roll and self._pre_roll_bytes > self._pre_roll_limit:
            self._pre_roll_bytes -= len(self._pre_roll.popleft())

    def _append_command(self, pcm16: bytes) -> None:
        self._command_parts.append(pcm16)
        self._command_bytes += len(pcm16)
        self.command_audio_seconds = self._command_bytes / float(self.sample_rate * 2)

    def _reset_to_idle(self, *, invalidate_session: bool = True) -> None:
        if invalidate_session:
            self._session += 1
        self.state = "idle"
        self.wake_until = 0.0
        self._command_parts = []
        self._command_bytes = 0
        self.command_audio_seconds = 0.0
        self._wake_at = 0.0
        self._speech_started_at = 0.0
        self._last_speech_at = 0.0
        self._probe_started = False
        self._final_started = False
        self.wake_recognizer.reset()

    def reset(self) -> None:
        self._reset_to_idle()
        self._pre_roll.clear()
        self._pre_roll_bytes = 0

    def _start_decode(self, *, probe: bool) -> None:
        if probe:
            if self._probe_started:
                return
            self._probe_started = True
        else:
            if self._final_started:
                return
            self._final_started = True
            self.state = "decoding"
        pcm = b"".join(self._command_parts)
        session = self._session
        hotwords = list(self.hotwords)

        def run() -> None:
            try:
                answer = self.asr_client.transcribe(
                    pcm, sample_rate=self.sample_rate, hotwords=hotwords
                )
                raw = str(answer.get("text") or "").strip()
                canonical = resolve_command_candidate(
                    raw, self.wake_word, self.mappings, self.emergency_phrases
                )
                self._events.put({
                    "session": session,
                    "kind": "probe" if probe else "decode",
                    "raw_text": raw,
                    "text": canonical,
                    "latency_ms": float(answer.get("latency_ms") or 0.0),
                    "error": None,
                })
            except Exception as exc:
                self._events.put({
                    "session": session,
                    "kind": "probe" if probe else "decode",
                    "raw_text": "",
                    "text": None,
                    "latency_ms": 0.0,
                    "error": str(exc),
                })

        threading.Thread(target=run, name="voice-funasr-decode", daemon=True).start()

    def _consume_async_event(self) -> dict | None:
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                return None
            if int(event.get("session", -1)) != self._session:
                continue
            error = event.get("error")
            if error:
                self.last_error = str(error)
                if event.get("kind") == "decode":
                    self._reset_to_idle()
                    return {"kind": "error", "text": "", "error": self.last_error}
                continue
            self.last_raw_text = str(event.get("raw_text") or "")
            self.last_latency_ms = float(event.get("latency_ms") or 0.0)
            canonical = event.get("text")
            if canonical:
                self.last_error = None
                result = {
                    "kind": "final",
                    "text": str(canonical),
                    "raw_text": self.last_raw_text,
                    "latency_ms": self.last_latency_ms,
                }
                self._reset_to_idle()
                return result
            if event.get("kind") == "decode":
                # The strong ASR heard something, but not a configured command.
                raw = self.last_raw_text
                self._reset_to_idle()
                return {
                    "kind": "unmatched",
                    "text": raw,
                    "raw_text": raw,
                    "latency_ms": self.last_latency_ms,
                }
            # Probe contained only the wake word/noise. Keep the command window.

    def accept(self, pcm16: bytes) -> dict | None:
        async_event = self._consume_async_event()
        if async_event is not None:
            return async_event
        if not pcm16:
            return None

        now = time.monotonic()
        rms = self._rms(pcm16)
        if self.state == "idle":
            self._append_pre_roll(pcm16)
            # Track room noise conservatively; loud speech should not rapidly
            # redefine the floor immediately before the wake detector fires.
            if rms < max(900.0, self._noise_rms * 2.2):
                alpha = 0.07 if rms < self._noise_rms else 0.015
                self._noise_rms = (1.0 - alpha) * self._noise_rms + alpha * rms
            if not self.wake_recognizer.accept(pcm16):
                return None
            self._session += 1
            self.state = "armed"
            self._wake_at = now
            self.wake_until = now + self.wake_window_seconds
            self._command_parts = list(self._pre_roll)
            self._command_bytes = sum(len(x) for x in self._command_parts)
            self.command_audio_seconds = self._command_bytes / float(self.sample_rate * 2)
            self._pre_roll.clear()
            self._pre_roll_bytes = 0
            self._speech_started_at = 0.0
            self._last_speech_at = 0.0
            self._probe_started = False
            self._final_started = False
            self.last_error = None
            return {"kind": "wake", "text": self.wake_word}

        if self.state == "decoding":
            return None

        # Armed command window.
        self._append_command(pcm16)
        threshold = max(330.0, min(1800.0, self._noise_rms * 2.2))
        post_wake = now - self._wake_at
        if post_wake >= 0.10 and rms >= threshold:
            if not self._speech_started_at:
                self._speech_started_at = now
            self._last_speech_at = now

        # A continuous phrase such as "体感截图" may have placed most of the
        # command in the pre-roll before KWS emitted the wake event. Probe that
        # audio once, but do not close the 3.5 s command window if it contains
        # only the wake word.
        if not self._probe_started and post_wake >= PROBE_DELAY_SECONDS:
            self._start_decode(probe=True)

        if self._speech_started_at:
            if now - self._last_speech_at >= END_SILENCE_SECONDS:
                self._start_decode(probe=False)
            elif now - self._speech_started_at >= MAX_COMMAND_AFTER_SPEECH_SECONDS:
                self._start_decode(probe=False)
        elif now >= self.wake_until:
            # Energy detection is only an endpoint accelerator, not a hard
            # acceptance condition.  At timeout ask SeACo about the full buffer
            # once, so a quiet command is not silently lost.
            self._start_decode(probe=False)
        return None

    def status(self) -> dict:
        worker = self.asr_client.status()
        return {
            "mode": self.mode,
            "state": self.state,
            "wake_window_active": self.state in {"armed", "decoding"},
            "wake_window_remaining_ms": max(0, int((self.wake_until - time.monotonic()) * 1000)) if self.wake_until else 0,
            "command_audio_seconds": round(self.command_audio_seconds, 2),
            "noise_rms": round(self._noise_rms, 1),
            "last_raw_text": self.last_raw_text,
            "last_latency_ms": self.last_latency_ms,
            "last_error": self.last_error,
            "worker": worker,
        }

    def close(self) -> None:
        try:
            self.asr_client.close()
        finally:
            self.reset()
