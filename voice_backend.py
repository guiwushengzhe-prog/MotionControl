from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from output_backend import KEY_CODES, XUSB_GAMEPAD_BUTTONS, KeyboardOutput


def compact_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def find_vosk_model(root: Path) -> Path | None:
    candidates: list[Path] = []
    env = os.environ.get("VOSK_MODEL_PATH", "").strip().strip('"')
    if env:
        candidates.append(Path(env))
    cfg = root / "config" / "vosk_model_path.txt"
    if cfg.is_file():
        try:
            text = cfg.read_text(encoding="utf-8-sig").strip().strip('"')
            if text:
                configured = Path(text)
                candidates.append(configured if configured.is_absolute() else root / configured)
        except OSError:
            pass
    candidates.append(root / "models" / "vosk-model-small-cn-0.22")
    if not getattr(sys, "frozen", False):
        candidates.extend([
            Path(r"F:\switch\models\vosk-model-small-cn-0.22"),
            Path(r"F:\switch\motionbridge\models\vosk-model-small-cn-0.22"),
            Path(r"F:\switch\motionbridge\data\models\vosk-model-small-cn-0.22"),
        ])
    for path in candidates:
        try:
            if path.is_dir():
                return path.resolve()
        except OSError:
            pass
    return None


class VoskCommandRecognizer:
    """Small command-domain recognizer. The mapping phrases are the grammar."""

    def __init__(self, model_path: Path, phrases: list[str], sample_rate: int = 16_000) -> None:
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel
        except ImportError as exc:
            raise RuntimeError("Vosk 未安装") from exc
        if not model_path.is_dir():
            raise RuntimeError(f"Vosk 中文模型不存在：{model_path}")
        SetLogLevel(-1)
        self._model = Model(str(model_path))
        supported: list[str] = []
        unsupported: list[str] = []
        for phrase in dict.fromkeys(phrases):
            tokenized = self._tokenize_phrase(self._model, phrase)
            if tokenized:
                supported.append(tokenized)
            else:
                unsupported.append(phrase)
        self.supported = supported
        self.unsupported = unsupported
        self.mode = "grammar" if supported else "open"
        if supported:
            grammar = json.dumps([*supported, "[unk]"], ensure_ascii=False)
            try:
                self._recognizer = KaldiRecognizer(self._model, sample_rate, grammar)
            except Exception:
                self.mode = "open"
                self._recognizer = KaldiRecognizer(self._model, sample_rate)
        else:
            self._recognizer = KaldiRecognizer(self._model, sample_rate)
        self.last_partial = ""

    @staticmethod
    def _tokenize_phrase(model: object, phrase: str) -> str | None:
        compact = re.sub(r"\s", "", phrase)
        if not compact:
            return None
        find_word = getattr(model, "vosk_model_find_word", None)
        if find_word is None:
            return compact
        tokens: list[str] = []
        index = 0
        while index < len(compact):
            match: str | None = None
            for length in range(min(6, len(compact) - index), 0, -1):
                candidate = compact[index:index + length]
                if find_word(candidate) >= 0:
                    match = candidate
                    break
            if match is None:
                return None
            tokens.append(match)
            index += len(match)
        return " ".join(tokens)

    def accept(self, pcm16: bytes) -> dict[str, str] | None:
        if self._recognizer.AcceptWaveform(pcm16):
            text = str(json.loads(self._recognizer.Result()).get("text", "")).strip()
            self.last_partial = ""
            return {"kind": "final", "text": text} if text else None
        partial = str(json.loads(self._recognizer.PartialResult()).get("partial", "")).strip()
        if partial and partial != self.last_partial:
            self.last_partial = partial
            return {"kind": "partial", "text": partial}
        return None


class VoiceService:
    def __init__(self, root: Path, execute_action: Callable[[dict], dict]) -> None:
        self.root = root
        self.config_path = root / "config" / "voice_mappings.json"
        self.execute_action = execute_action
        self.sample_rate = 16_000
        self.mappings: list[dict] = []
        self.model_path = find_vosk_model(root)
        self.recognizer: VoskCommandRecognizer | None = None
        self.recognizer_mode = "off"
        self.supported_count = 0
        self.unsupported: list[str] = []
        self.last_partial = ""
        self.last_final = ""
        self.last_command: str | None = None
        self.last_action: str | None = None
        self.last_executed: bool | None = None
        self.last_error: str | None = None
        self.audio_bytes = 0
        self.last_audio_at = 0.0
        self._lock = threading.RLock()
        self._load()
        self._rebuild_recognizer()

    def _load(self) -> None:
        source = self.config_path
        if not source.is_file():
            # Reuse the newest successful sibling config first; no manual re-entry.
            previous_candidates = [
                self.root.parent / "MotionControl-v0.7.2-overlay" / "config" / "voice_mappings.json",
                self.root.parent / "MotionControl-v0.7.1-voice-mapping" / "config" / "voice_mappings.json",
            ]
            previous = next((p for p in previous_candidates if p.is_file()), None)
            if previous is not None:
                source = previous
            else:
                return
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            self.mappings = self._validate_mappings(data.get("mappings", []))
            if source != self.config_path:
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                self.config_path.write_text(
                    json.dumps({"mappings": self.mappings}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except Exception as exc:
            self.last_error = f"语音配置读取失败：{exc}"

    @staticmethod
    def _validate_mappings(items) -> list[dict]:
        if not isinstance(items, list):
            raise ValueError("mappings 必须是数组")
        result: list[dict] = []
        seen: set[str] = set()
        for raw in items[:32]:
            if not isinstance(raw, dict):
                continue
            phrase = str(raw.get("phrase", "")).strip()
            if not phrase:
                continue
            if len(phrase) > 24:
                raise ValueError(f"命令词过长：{phrase}")
            key = compact_text(phrase)
            if key in seen:
                raise ValueError(f"命令词重复：{phrase}")
            seen.add(key)
            action_type = str(raw.get("type", "keyboard")).lower()
            target = str(raw.get("target", "")).strip().upper()
            if action_type == "gamepad":
                if target not in XUSB_GAMEPAD_BUTTONS:
                    raise ValueError(f"暂不支持的 Xbox 键：{target}")
            elif action_type == "keyboard":
                parts = [KeyboardOutput.normalize(x) for x in target.split("+") if x.strip()]
                if not parts or len(parts) > 4:
                    raise ValueError(f"键盘映射格式错误：{target}")
                invalid = [x for x in parts if x not in KEY_CODES]
                if invalid:
                    raise ValueError("不支持的键盘键：" + ", ".join(invalid))
                target = "+".join(parts)
            else:
                raise ValueError(f"未知输出类型：{action_type}")
            result.append({"phrase": phrase, "type": action_type, "target": target})
        return result

    def configure(self, items) -> dict:
        with self._lock:
            self.mappings = self._validate_mappings(items)
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(
                json.dumps({"mappings": self.mappings}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.model_path = find_vosk_model(self.root)
            self._rebuild_recognizer()
            return self.status()

    def _rebuild_recognizer(self) -> None:
        self.recognizer = None
        self.recognizer_mode = "off"
        self.supported_count = 0
        self.unsupported = []
        if not self.mappings:
            return
        if self.model_path is None:
            self.last_error = "未找到 Vosk 中文模型"
            return
        try:
            self.recognizer = VoskCommandRecognizer(
                self.model_path,
                [m["phrase"] for m in self.mappings],
                self.sample_rate,
            )
            self.recognizer_mode = self.recognizer.mode
            self.supported_count = len(self.recognizer.supported)
            self.unsupported = list(self.recognizer.unsupported)
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)

    def ingest(self, pcm16: bytes) -> dict:
        with self._lock:
            if not pcm16 or len(pcm16) % 2:
                raise ValueError("音频必须是 PCM16 双数字节")
            if self.recognizer is None:
                self.model_path = find_vosk_model(self.root)
                self._rebuild_recognizer()
            if self.recognizer is None:
                raise RuntimeError(self.last_error or "请先添加语音命令")
            self.audio_bytes += len(pcm16)
            self.last_audio_at = time.monotonic()
            event = self.recognizer.accept(pcm16)
            if event:
                if event["kind"] == "partial":
                    self.last_partial = event["text"]
                else:
                    self.last_final = event["text"]
                    self.last_partial = ""
                    self._match_and_execute(event["text"])
            return self.status()

    def _match_and_execute(self, recognized: str) -> None:
        got = compact_text(recognized)
        match = next((m for m in self.mappings if compact_text(m["phrase"]) == got), None)
        if match is None:
            return
        action = {"type": match["type"], "target": match["target"], "source": "voice"}
        self.last_command = match["phrase"]
        self.last_action = f'{match["type"]}:{match["target"]}'
        def run() -> None:
            try:
                result = self.execute_action(action)
                with self._lock:
                    self.last_executed = bool(result.get("executed", False))
                    self.last_error = None if self.last_executed else str(result.get("reason", "输出未开启"))
            except Exception as exc:
                with self._lock:
                    self.last_executed = False
                    self.last_error = str(exc)
        # Do not block the audio ingestion request while a key pulse is held.
        threading.Thread(target=run, daemon=True).start()

    def status(self) -> dict:
        alive = bool(self.last_audio_at and time.monotonic() - self.last_audio_at < 1.5)
        return {
            "available": self.recognizer is not None,
            "model_path": str(self.model_path) if self.model_path else None,
            "recognizer_mode": self.recognizer_mode,
            "supported_count": self.supported_count,
            "unsupported": list(self.unsupported),
            "mappings": list(self.mappings),
            "audio_alive": alive,
            "audio_seconds": round(self.audio_bytes / (self.sample_rate * 2), 1),
            "last_partial": self.last_partial,
            "last_final": self.last_final,
            "last_command": self.last_command,
            "last_action": self.last_action,
            "last_executed": self.last_executed,
            "last_error": self.last_error,
        }
