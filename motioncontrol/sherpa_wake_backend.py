from __future__ import annotations

from pathlib import Path
from motioncontrol_shared.text_norm import compact_text



class SherpaWakeWordRecognizer:
    """Small sherpa-onnx KWS used only for the wake word.

    MotionControl deliberately does *not* use this 3M model to choose between
    command words.  Its only job is to decide whether the configured wake word
    (normally ``体感``) was spoken.  Command recognition is handled by the much
    larger SeACoParaformer worker after wake-up.
    """

    def __init__(
        self,
        model_path: Path,
        wake_word: str,
        *,
        sample_rate: int = 16_000,
        score: float = 2.0,
        threshold: float = 0.32,
    ) -> None:
        try:
            import numpy as np
            import sherpa_onnx
        except ImportError as exc:
            raise RuntimeError(
                "sherpa-onnx 唤醒词环境未就绪；需要 sherpa-onnx==1.13.5、"
                "sherpa-onnx-bin==1.13.5、sentencepiece、pypinyin"
            ) from exc

        self._np = np
        self._sherpa = sherpa_onnx
        self.model_path = Path(model_path)
        self.sample_rate = int(sample_rate)
        self.wake_word = str(wake_word or "体感").strip()
        self.mode = "sherpa-wake-only"

        encoder = self.model_path / "encoder-epoch-13-avg-2-chunk-16-left-64.onnx"
        decoder = self.model_path / "decoder-epoch-13-avg-2-chunk-16-left-64.onnx"
        joiner = self.model_path / "joiner-epoch-13-avg-2-chunk-16-left-64.onnx"
        tokens = self.model_path / "tokens.txt"
        lexicon = self.model_path / "en.phone"
        for path in (encoder, decoder, joiner, tokens, lexicon):
            if not path.is_file():
                raise RuntimeError(f"sherpa 唤醒模型文件缺失：{path}")

        generated = self.model_path / "motioncontrol_keywords"
        generated.mkdir(parents=True, exist_ok=True)
        keyword_file = generated / "wake_only_keywords.txt"
        try:
            encoded = sherpa_onnx.text2token(
                [self.wake_word],
                tokens=str(tokens),
                tokens_type="phone+ppinyin",
                lexicon=str(lexicon),
            )
            toks = encoded[0] if encoded else []
        except Exception as exc:
            raise RuntimeError(f"唤醒词无法编码：{self.wake_word}：{exc}") from exc
        if not toks:
            raise RuntimeError(f"唤醒词无法编码：{self.wake_word}")
        label = compact_text(self.wake_word)
        keyword_file.write_text(
            " ".join([*toks, f":{score:.2f}", f"#{threshold:.2f}", f"@{label}"]) + "\n",
            encoding="utf-8",
        )

        self._spotter = sherpa_onnx.KeywordSpotter(
            keywords_file=str(keyword_file),
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
        )
        self._stream = self._spotter.create_stream()

    def accept(self, pcm16: bytes) -> bool:
        if not pcm16:
            return False
        samples = self._np.frombuffer(pcm16, dtype=self._np.int16).astype(self._np.float32)
        samples *= 1.0 / 32768.0
        self._stream.accept_waveform(self.sample_rate, samples)
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            detected = compact_text(self._spotter.get_result(self._stream))
            if detected:
                self._spotter.reset_stream(self._stream)
                return detected == compact_text(self.wake_word)
        return False

    def reset(self) -> None:
        self._stream = self._spotter.create_stream()
