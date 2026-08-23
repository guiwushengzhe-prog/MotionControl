from __future__ import annotations

import os
import re
import time
from pathlib import Path


MODEL_DIRNAME = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
WAKE_WINDOW_SECONDS = 3.5
MIN_COMMAND_CHARS = 2


def compact_text(value: str) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[\s\u3000，。！？、,.!?;；:：]+", "", text)


def find_sherpa_kws_model(root: Path) -> Path | None:
    """Resolve the explicit sherpa KWS model directory.

    Priority is intentionally explicit-config first.  This mirrors the model-root
    fix in the current MotionControl branch and avoids an unrelated local models
    directory shadowing a configured path.
    """
    candidates: list[Path] = []
    env = os.environ.get("SHERPA_KWS_MODEL_PATH", "").strip().strip('"')
    if env:
        candidates.append(Path(env))

    cfg = root / "config" / "sherpa_kws_model_path.txt"
    if cfg.is_file():
        try:
            text = cfg.read_text(encoding="utf-8-sig").strip().strip('"')
            if text:
                configured = Path(text)
                candidates.append(configured if configured.is_absolute() else root / configured)
        except OSError:
            pass

    candidates.extend([
        root / "models" / MODEL_DIRNAME,
        root / "models" / "speech" / MODEL_DIRNAME,
    ])

    required = (
        "encoder-epoch-13-avg-2-chunk-16-left-64.onnx",
        "decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
        "joiner-epoch-13-avg-2-chunk-16-left-64.onnx",
        "tokens.txt",
        "en.phone",
    )
    for path in candidates:
        try:
            if path.is_dir() and all((path / name).is_file() for name in required):
                return path.resolve()
        except OSError:
            pass
    return None


def _spoken_command_candidates(wake_word: str, mappings: list[dict], emergency_phrases: list[str]) -> list[tuple[str, str]]:
    """Return (spoken phrase, label) pairs used by the command-stage KWS.

    One-character Chinese commands are deliberately excluded.  The existing
    mappings already contain safer alternatives such as 向上/向下/向左/向右;
    short keywords are disproportionately prone to false triggers in KWS.
    """
    wake = compact_text(wake_word)
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()

    for mapping in mappings:
        for phrase in [mapping.get("phrase", ""), *mapping.get("synonyms", [])]:
            phrase = str(phrase or "").strip()
            key = compact_text(phrase)
            if not key or len(key) < MIN_COMMAND_CHARS or key in seen:
                continue
            seen.add(key)
            pairs.append((phrase, phrase))

    for phrase in emergency_phrases:
        full = str(phrase or "").strip()
        key = compact_text(full)
        spoken = full
        if wake and key.startswith(wake):
            spoken = full[len(wake_word):].strip()
        spoken_key = compact_text(spoken)
        if spoken_key and len(spoken_key) >= MIN_COMMAND_CHARS and spoken_key not in seen:
            seen.add(spoken_key)
            # Preserve the full configured emergency phrase as the result label
            # so VoiceService can reuse its existing emergency-stop path.
            pairs.append((spoken, full))
    return pairs


class SherpaKwsCommandRecognizer:
    """Two-stage keyword spotter: wake word first, then a short command window.

    This is not free-form ASR.  It only detects the configured wake word and
    command vocabulary, which is the intended interaction model for MotionControl.
    """

    def __init__(
        self,
        model_path: Path,
        wake_word: str,
        mappings: list[dict],
        emergency_phrases: list[str],
        *,
        sample_rate: int = 16_000,
        wake_window_seconds: float = WAKE_WINDOW_SECONDS,
    ) -> None:
        try:
            import numpy as np
            import sherpa_onnx
        except ImportError as exc:
            raise RuntimeError("sherpa-onnx 未安装；请安装 sherpa-onnx==1.13.5 和 sherpa-onnx-bin==1.13.5") from exc

        self._np = np
        self._sherpa = sherpa_onnx
        self.model_path = Path(model_path)
        self.sample_rate = int(sample_rate)
        self.wake_word = str(wake_word or "体感").strip()
        self.wake_window_seconds = float(wake_window_seconds)
        self.mode = "sherpa-kws"
        self.unsupported: list[str] = []
        self.supported: list[str] = []
        self.wake_until = 0.0

        self.encoder = self.model_path / "encoder-epoch-13-avg-2-chunk-16-left-64.onnx"
        self.decoder = self.model_path / "decoder-epoch-13-avg-2-chunk-16-left-64.onnx"
        self.joiner = self.model_path / "joiner-epoch-13-avg-2-chunk-16-left-64.onnx"
        self.tokens = self.model_path / "tokens.txt"
        self.lexicon = self.model_path / "en.phone"
        for path in (self.encoder, self.decoder, self.joiner, self.tokens, self.lexicon):
            if not path.is_file():
                raise RuntimeError(f"sherpa KWS 模型文件缺失：{path}")

        generated = self.model_path / "motioncontrol_keywords"
        generated.mkdir(parents=True, exist_ok=True)
        wake_file = generated / "wake_keywords.txt"
        command_file = generated / "command_keywords.txt"

        wake_lines = self._encode_pairs([(self.wake_word, self.wake_word)], score=2.0, threshold=0.32)
        command_pairs = _spoken_command_candidates(self.wake_word, mappings, emergency_phrases)
        command_lines = self._encode_pairs(command_pairs, score=1.6, threshold=0.34)
        if not wake_lines:
            raise RuntimeError(f"唤醒词无法编码：{self.wake_word}")
        if not command_lines:
            raise RuntimeError("没有可用的 sherpa KWS 命令词")
        wake_file.write_text("\n".join(wake_lines) + "\n", encoding="utf-8")
        command_file.write_text("\n".join(command_lines) + "\n", encoding="utf-8")

        common = dict(
            tokens=str(self.tokens),
            encoder=str(self.encoder),
            decoder=str(self.decoder),
            joiner=str(self.joiner),
            num_threads=2,
            max_active_paths=4,
            keywords_score=1.0,
            keywords_threshold=0.25,
            num_trailing_blanks=1,
            provider="cpu",
        )
        self._wake_spotter = sherpa_onnx.KeywordSpotter(keywords_file=str(wake_file), **common)
        self._command_spotter = sherpa_onnx.KeywordSpotter(keywords_file=str(command_file), **common)
        self._wake_stream = self._wake_spotter.create_stream()
        self._command_stream = self._command_spotter.create_stream()
        self.last_partial = ""

    def _encode_pairs(self, pairs: list[tuple[str, str]], *, score: float, threshold: float) -> list[str]:
        lines: list[str] = []
        for spoken, label in pairs:
            spoken = str(spoken or "").strip()
            label = str(label or spoken).strip()
            if not spoken:
                continue
            try:
                encoded = self._sherpa.text2token(
                    [spoken],
                    tokens=str(self.tokens),
                    tokens_type="phone+ppinyin",
                    lexicon=str(self.lexicon),
                )
                toks = encoded[0] if encoded else []
                if not toks:
                    raise ValueError("empty token sequence")
                safe_label = compact_text(label).replace(" ", "_")
                lines.append(" ".join([*toks, f":{score:.2f}", f"#{threshold:.2f}", f"@{safe_label}"]))
                self.supported.append(label)
            except Exception:
                self.unsupported.append(spoken)
        return lines

    def _decode(self, spotter, stream) -> str | None:
        result: str | None = None
        while spotter.is_ready(stream):
            spotter.decode_stream(stream)
            detected = str(spotter.get_result(stream) or "").strip()
            if detected:
                result = detected
                spotter.reset_stream(stream)
                break
        return result

    def _float_samples(self, pcm16: bytes):
        samples = self._np.frombuffer(pcm16, dtype=self._np.int16).astype(self._np.float32)
        samples *= (1.0 / 32768.0)
        return samples

    def accept(self, pcm16: bytes) -> dict[str, str] | None:
        samples = self._float_samples(pcm16)
        now = time.monotonic()
        if self.wake_until and now <= self.wake_until:
            self._command_stream.accept_waveform(self.sample_rate, samples)
            detected = self._decode(self._command_spotter, self._command_stream)
            if detected:
                self.wake_until = 0.0
                self._wake_stream = self._wake_spotter.create_stream()
                return {"kind": "final", "text": detected}
            return None

        if self.wake_until and now > self.wake_until:
            self.wake_until = 0.0
            self._command_stream = self._command_spotter.create_stream()

        self._wake_stream.accept_waveform(self.sample_rate, samples)
        detected = self._decode(self._wake_spotter, self._wake_stream)
        if detected:
            self.wake_until = now + self.wake_window_seconds
            self._command_stream = self._command_spotter.create_stream()
            # Feed the current block once into command stage too.  With natural
            # "体感截图" speech this preserves the beginning of the command if
            # the wake detector fires near a 100 ms block boundary.
            self._command_stream.accept_waveform(self.sample_rate, samples)
            return {"kind": "wake", "text": self.wake_word}
        return None

    def reset(self) -> None:
        self._wake_stream = self._wake_spotter.create_stream()
        self._command_stream = self._command_spotter.create_stream()
        self.wake_until = 0.0
        self.last_partial = ""
