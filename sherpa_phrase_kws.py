from __future__ import annotations
import json
import re
import time
from pathlib import Path

_PUNCT = re.compile(r"[\s\u3000，。！？、,.!?;；:：]+")


def compact(s: str) -> str:
    return _PUNCT.sub("", str(s or "").strip().lower())


class SherpaPhraseKws:
    """Single-stage full-phrase KWS. No wake window, no ASR, no VAD."""

    def __init__(
        self,
        model_dir: Path,
        keywords_file: Path,
        action_map_file: Path,
        sample_rate: int = 16000,
    ):
        import numpy as np
        import sherpa_onnx

        self.np = np
        self.sherpa = sherpa_onnx
        self.sample_rate = sample_rate
        self.actions = json.loads(action_map_file.read_text(encoding="utf-8"))

        def pick(*names):
            for n in names:
                p = model_dir / n
                if p.is_file():
                    return p
            raise RuntimeError("missing KWS model file: " + " / ".join(names))

        encoder = pick(
            "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
            "encoder.int8.onnx",
            "encoder-epoch-13-avg-2-chunk-16-left-64.onnx",
        )
        decoder = pick(
            "decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
            "decoder.onnx",
        )
        joiner = pick(
            "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
            "joiner.int8.onnx",
            "joiner-epoch-13-avg-2-chunk-16-left-64.onnx",
        )
        tokens = model_dir / "tokens.txt"

        self.spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(tokens),
            encoder=str(encoder),
            decoder=str(decoder),
            joiner=str(joiner),
            num_threads=2,
            max_active_paths=4,
            keywords_score=1.0,
            keywords_threshold=0.25,
            num_trailing_blanks=1,
            provider="cpu",
            keywords_file=str(keywords_file),
        )
        self.stream = self.spotter.create_stream()
        self.last_trigger_at = 0.0
        self.last_command_id = None

    def accept_pcm16(self, pcm16: bytes):
        if not pcm16:
            return None
        x = self.np.frombuffer(pcm16, dtype=self.np.int16).astype(self.np.float32) / 32768.0
        self.stream.accept_waveform(self.sample_rate, x)
        while self.spotter.is_ready(self.stream):
            self.spotter.decode_stream(self.stream)
            phrase = str(self.spotter.get_result(self.stream) or "").strip()
            if not phrase:
                continue
            self.spotter.reset_stream(self.stream)
            action = self.actions.get(compact(phrase))
            if not action:
                return None
            now = time.monotonic()
            cooldown = action.get("cooldown_ms", 700) / 1000.0
            if action["id"] != "system.emergency_stop" and now - self.last_trigger_at < cooldown:
                return None
            self.last_trigger_at = now
            self.last_command_id = action["id"]
            return {"phrase": phrase, **action}
        return None

    def reset(self):
        self.stream = self.spotter.create_stream()
        self.last_trigger_at = 0.0
        self.last_command_id = None

    def close(self):
        try:
            self.stream.release()
        except Exception:
            pass
