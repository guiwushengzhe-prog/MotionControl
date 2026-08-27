from __future__ import annotations

import json
import math
import os
import queue
import re
import sys
import threading
import time
from array import array
from pathlib import Path
from typing import Callable

from output_backend import KEY_CODES, XUSB_GAMEPAD_BUTTONS, KeyboardOutput
DEFAULT_WAKE_WORD = "体感"
DEFAULT_EMERGENCY_STOP = "体感紧急停止"
SYSTEM_HEAD_CALIBRATION_START = "HEAD_CALIBRATION_START"
VOICE_TIMEOUT_SECONDS = 1.5
WAKE_COMMAND_WINDOW_SECONDS = 3.5
MAX_AUDIO_FRAME_BYTES = 256 * 1024


def compact_text(value: str) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[\s\u3000，。！？、,.!?;；:：]+", "", text)


def find_vosk_model(root: Path) -> Path | None:
    """Resolve the portable Vosk model configured for the production runtime."""
    candidates: list[Path] = []
    configured_env = os.environ.get("VOSK_MODEL_PATH", "").strip().strip('"')
    if configured_env:
        candidates.append(Path(configured_env))
    config_path = root / "config" / "vosk_model_path.txt"
    if config_path.is_file():
        try:
            configured = config_path.read_text(encoding="utf-8-sig").strip().strip('"')
            if configured:
                candidate = Path(configured)
                candidates.append(candidate if candidate.is_absolute() else root / candidate)
        except OSError:
            pass
    candidates.append(root / "models" / "vosk-model-small-cn-0.22")
    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate.resolve()
        except OSError:
            pass
    return None


class VoskCommandRecognizer:
    """Streaming Vosk recognizer limited to the configured command phrases."""

    def __init__(self, model_path: Path, phrases: list[str], sample_rate: int = 16_000) -> None:
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel
        except ImportError as exc:
            raise RuntimeError("Vosk 未安装") from exc
        if not model_path.is_dir():
            raise RuntimeError(f"Vosk 中文模型不存在：{model_path}")
        SetLogLevel(-1)
        self._KaldiRecognizer = KaldiRecognizer
        self._model = Model(str(model_path))
        self.sample_rate = int(sample_rate)
        # The small Chinese model grammar is character-token based.  The
        # robustness run proved that unspaced whole phrases are discarded as
        # unknown vocabulary, while parser-side compact_text safely rejoins it.
        self.supported = [self._grammar_entry(item) for item in dict.fromkeys(phrases) if compact_text(item)]
        self.unsupported: list[str] = []
        self.mode = "vosk_constrained_grammar"
        self._grammar = json.dumps([*self.supported, "[unk]"], ensure_ascii=False)
        self._recognizer = None
        self.last_partial = ""
        self.reset()

    @staticmethod
    def _grammar_entry(value: str) -> str:
        text = str(value or "").strip()
        if any("\u4e00" <= char <= "\u9fff" for char in text):
            return " ".join(char for char in text if not char.isspace())
        return text

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

    def reset(self) -> None:
        self._recognizer = self._KaldiRecognizer(self._model, self.sample_rate, self._grammar)
        self.last_partial = ""

    def close(self) -> None:
        self._recognizer = None


class VoiceService:
    """Shared voice parser for the computer microphone and phone voice_text."""

    def __init__(
        self,
        root: Path,
        execute_action: Callable[[dict], dict],
        emergency_stop: Callable[[], dict] | None = None,
        clear_source: Callable[[str], dict] | None = None,
    ) -> None:
        self.root = root
        self.config_path = root / "config" / "voice_mappings.json"
        self.execute_action = execute_action
        self.emergency_stop = emergency_stop or (lambda: {"executed": True})
        self.clear_source = clear_source or (lambda _source: {})
        self.sample_rate = 16_000
        self.mappings: list[dict] = []
        self.wake_word = DEFAULT_WAKE_WORD
        self.emergency_stop_phrases: list[str] = [DEFAULT_EMERGENCY_STOP]
        self.model_path = find_vosk_model(root)
        self.action_map_file = root / "config" / "generated_voice" / "voice_action_map.json"
        self.command_registry: dict[str, dict] = {}
        self.recognizer: VoskCommandRecognizer | None = None
        self.recognizer_mode = "vosk_constrained_grammar"
        self.supported_count = 0
        self.unsupported: list[str] = []
        self.last_partial = ""
        self.last_final = ""
        self.last_command: str | None = None
        self.last_action: str | None = None
        self.wake_until = 0.0
        self.last_wake_at = 0.0
        self.last_executed: bool | None = None
        self.last_error: str | None = None
        self.audio_bytes = 0
        self.last_audio_at = 0.0
        self.last_rms = 0.0
        self.peak_rms = 0.0
        self.connected = False
        self.audio_ready = False
        self.source_id: str | None = None
        self.device_id: str | None = None
        self.source_kind: str | None = None
        self._mic_stream = None
        self._mic_queue: queue.Queue[bytes] | None = None
        self._mic_stop = threading.Event()
        self._mic_thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._load()
        self._load_command_registry()
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
            self.wake_word = self._validate_wake_word(data.get("wake_word", DEFAULT_WAKE_WORD))
            self.emergency_stop_phrases = self._validate_emergency_phrases(data.get("emergency_stop_phrases", []))
            self.mappings = self._validate_mappings(data.get("mappings", []))
            if source != self.config_path:
                self._write_config()
        except Exception as exc:
            self.last_error = f"语音配置读取失败：{exc}"

    @staticmethod
    def _validate_wake_word(value) -> str:
        word = str(value or DEFAULT_WAKE_WORD).strip()
        if not word or len(word) > 12:
            raise ValueError("唤醒词必须是 1 到 12 个字符")
        return word

    @staticmethod
    def _validate_emergency_phrases(items) -> list[str]:
        values = [str(x).strip() for x in (items if isinstance(items, list) else []) if str(x).strip()]
        values = list(dict.fromkeys(values[:8]))
        if DEFAULT_EMERGENCY_STOP not in values:
            values.insert(0, DEFAULT_EMERGENCY_STOP)
        if any(len(x) > 24 for x in values):
            raise ValueError("紧急停止命令过长")
        return values

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
            synonyms = raw.get("synonyms", [])
            if not isinstance(synonyms, list):
                synonyms = []
            aliases: list[str] = []
            for item in synonyms[:8]:
                alias = str(item).strip()
                if alias and compact_text(alias) not in {key, *(compact_text(x) for x in aliases)}:
                    aliases.append(alias)
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
            elif action_type == "system":
                allowed_system = {
                    SYSTEM_HEAD_CALIBRATION_START,
                    "HEAD.CALIBRATE", "HEAD.CENTER",
                    "OUTPUT.START", "OUTPUT.STOP",
                    "SCENE.CAPTURE_REFERENCE", "SCENE.REMATCH",
                }
                if target not in allowed_system:
                    raise ValueError(f"暂不支持的系统命令：{target}")
            else:
                raise ValueError(f"未知输出类型：{action_type}")
            item = {"phrase": phrase, "type": action_type, "target": target}
            if aliases:
                item["synonyms"] = aliases
            result.append(item)
        return result

    def _write_config(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps({
                "wake_word": self.wake_word,
                "emergency_stop_phrases": self.emergency_stop_phrases,
                "mappings": self.mappings,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def configure(self, items, *, wake_word=None, emergency_stop_phrases=None) -> dict:
        with self._lock:
            self.mappings = self._validate_mappings(items)
            if wake_word is not None:
                self.wake_word = self._validate_wake_word(wake_word)
            if emergency_stop_phrases is not None:
                self.emergency_stop_phrases = self._validate_emergency_phrases(emergency_stop_phrases)
            self._write_config()
            self._rebuild_recognizer()
            return self.status()

    def _load_command_registry(self) -> None:
        self.command_registry = {}
        if not self.action_map_file.is_file():
            return
        try:
            data = json.loads(self.action_map_file.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                for phrase, raw in data.items():
                    if not isinstance(raw, dict):
                        continue
                    command = dict(raw)
                    command["phrase"] = str(phrase)
                    self.command_registry[compact_text(phrase)] = command
        except Exception as exc:
            self.last_error = f"语音命令注册表读取失败：{exc}"

    def _rebuild_recognizer(self) -> None:
        previous = self.recognizer
        self.recognizer = None
        if previous is not None:
            try:
                previous.close()
            except Exception:
                pass
        self.recognizer_mode = "off"
        self.supported_count = 0
        self.unsupported = []
        self.audio_ready = False
        self.model_path = find_vosk_model(self.root)
        if self.model_path is None:
            self.last_error = "未找到 Vosk 中文模型；请放入 models/vosk-model-small-cn-0.22"
            return
        try:
            phrases = [self.wake_word, *self.emergency_stop_phrases]
            for mapping in self.mappings:
                commands = [mapping["phrase"], *mapping.get("synonyms", [])]
                phrases.extend(f"{self.wake_word}{command}" for command in commands)
            self.recognizer = VoskCommandRecognizer(self.model_path, phrases, self.sample_rate)
            self.recognizer_mode = self.recognizer.mode
            self.supported_count = len(self.recognizer.supported)
            self.unsupported = list(self.recognizer.unsupported)
            self.last_error = None
            self.audio_ready = True
        except Exception as exc:
            self.last_error = str(exc)
            self.audio_ready = False

    def _reset_stream_recognizer(self) -> None:
        if self.recognizer is not None:
            self.recognizer.reset()
        self.last_partial = ""
        self.last_final = ""

    @staticmethod
    def _pcm_rms(pcm16: bytes) -> float:
        samples = array("h")
        samples.frombytes(pcm16)
        if sys.byteorder != "little":
            samples.byteswap()
        if not samples:
            return 0.0
        return math.sqrt(sum(float(x) * float(x) for x in samples) / len(samples))

    def _release_locked(self, source_id: str | None) -> None:
        if not source_id:
            return
        try:
            self.clear_source(f"voice:{source_id}")
        except Exception as exc:
            self.last_error = str(exc)

    def _activate_locked(self, source_id: str, device_id: str, source_kind: str) -> None:
        if self.source_id and self.source_id != source_id:
            self._release_locked(self.source_id)
        changed = self.source_id != source_id
        self.source_id = source_id
        self.device_id = device_id
        self.source_kind = source_kind
        self.connected = True
        self.audio_ready = source_kind == "computer" and self.recognizer is not None and self.model_path is not None
        if changed:
            self.audio_bytes = 0
            self.last_rms = 0.0
            self.peak_rms = 0.0
            self.last_partial = ""
            self.last_final = ""
            self.last_command = None
            self.last_action = None
            self.last_executed = None
            self._reset_stream_recognizer()

    def connect_phone_text(self, source_id: str, device_id: str) -> dict:
        with self._lock:
            self._activate_locked(str(source_id), str(device_id), "phone")
            return self.status()

    def accept_phone_text(self, source_id: str, device_id: str, text: str, confidence: float | None = None) -> tuple[dict, dict | None]:
        text = str(text or "").strip()
        if not text or len(text) > 96:
            raise ValueError("voice_text 必须是 1 到 96 个字符")
        with self._lock:
            if self.source_kind == "computer":
                return self.status(), {"matched": False, "reason": "computer_voice_source_active"}
            self._activate_locked(str(source_id), str(device_id), "phone")
            self.last_final = text
            self.last_partial = ""
            self.last_audio_at = time.monotonic()
            result = self._match_and_execute(text, source_id=str(source_id), enforce_wake=True)
            return self.status(), result

    def accept_phone_command(self, source_id: str, device_id: str, command_id: str, phrase: str = "") -> tuple[dict, dict | None]:
        """Execute a phone KWS command by stable command_id.

        v0.9.6 phones normally transmit only command_id.  ``phrase`` remains
        optional for backward compatibility and diagnostics; the canonical
        phrase is always resolved from the local registry before execution.
        """
        cid = str(command_id or "").strip()
        phrase = str(phrase or "").strip()
        if not cid:
            raise ValueError("voice_command command_id 不能为空")
        with self._lock:
            if self.source_kind == "computer":
                return self.status(), {"matched": False, "reason": "computer_voice_source_active"}
            self._activate_locked(str(source_id), str(device_id), "phone")
            self.last_final = phrase
            self.last_partial = ""
            self.last_audio_at = time.monotonic()
            # Keep stable command_id compatibility for phones already on the
            # previous app build; new Vosk phones send final voice_text.
            command = None
            if phrase:
                command = self.command_registry.get(compact_text(phrase))
            if command is None:
                for v in self.command_registry.values():
                    if v.get("id") == cid:
                        command = v
                        break
            if command is None:
                self.last_command = None
                self.last_action = None
                self.last_executed = False
                result = {"matched": False, "reason": "command_not_in_registry", "command_id": cid, "phrase": phrase}
                return self.status(), result
            canonical_phrase = str(command.get("phrase", phrase or cid)).strip()
            self.last_final = canonical_phrase
            result = self._execute_command_action(command, source_id=str(source_id))
            return self.status(), result

    def _ingest_pcm_locked(self, source_id: str, pcm16: bytes) -> tuple[dict | None, dict | None]:
        if not pcm16 or len(pcm16) % 2:
            raise ValueError("音频必须是 16kHz 单声道 PCM16 双数字节")
        if len(pcm16) > MAX_AUDIO_FRAME_BYTES:
            raise ValueError("单个音频帧过大")
        if self.recognizer is None or not self.audio_ready:
            raise RuntimeError(self.last_error or "Vosk 受限语法未就绪")
        self.audio_bytes += len(pcm16)
        self.last_audio_at = time.monotonic()
        self.last_rms = self._pcm_rms(pcm16)
        self.peak_rms = max(self.peak_rms, self.last_rms)
        event = self.recognizer.accept(pcm16)
        result = None
        if event:
            if event["kind"] == "partial":
                self.last_partial = event["text"]
            else:
                self.last_final = event["text"]
                self.last_partial = ""
                result = self._match_and_execute(event["text"], source_id=source_id, enforce_wake=True)
        return event, result

    def ingest(self, pcm16: bytes) -> dict:
        """Compatibility helper for a local test or an older caller."""
        with self._lock:
            self.model_path = find_vosk_model(self.root)
            if self.recognizer is None:
                self._rebuild_recognizer()
            self._activate_locked("computer_microphone", "computer", "computer")
            self.audio_ready = self.recognizer is not None
            self._ingest_pcm_locked("computer_microphone", pcm16)
            return self.status()

    def _execute_command_action(self, command: dict, *, source_id: str | None = None) -> dict | None:
        """Execute a v0.9.4 command from KWS or phone voice_command.
        command contains: id, label, kind, default_target, cooldown_ms, phrase.
        """
        cid = str(command.get("id", ""))
        kind = str(command.get("kind", ""))
        target = str(command.get("default_target", ""))
        phrase = str(command.get("phrase", ""))
        if not cid or not target:
            return {"matched": False, "reason": "invalid_command"}
        if cid == "system.emergency_stop":
            self.last_command = phrase or DEFAULT_EMERGENCY_STOP
            self.last_action = "emergency_stop"
            try:
                result = self.emergency_stop() or {}
                self.last_executed = True
                self.last_error = None
                return {"matched": True, "command": self.last_command, "emergency": True, **result}
            except Exception as exc:
                self.last_executed = False
                self.last_error = str(exc)
                return {"matched": True, "command": self.last_command, "ok": False, "message": str(exc)}
        action = {"type": kind, "target": target, "command_id": cid}
        action["source"] = f"voice:{source_id}" if source_id else "voice"
        if kind == "system":
            action["voice_source_id"] = source_id
            action["voice_source_kind"] = self.source_kind
        self.last_command = phrase or cid
        self.last_action = f"{kind}:{target}"

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

        threading.Thread(target=run, name="voice-command", daemon=True).start()
        return {"matched": True, "command": self.last_command, "command_id": cid, "pending": True}

    def _match_and_execute(self, recognized: str, *, source_id: str | None = None, enforce_wake: bool = False) -> dict | None:
        got = compact_text(recognized)
        wake = compact_text(self.wake_word)
        if got in {compact_text(item) for item in self.emergency_stop_phrases}:
            if enforce_wake and (not wake or not got.startswith(wake)):
                return {"matched": False, "reason": "wake_word_required"}
            self.last_command = DEFAULT_EMERGENCY_STOP
            self.last_action = "emergency_stop"
            try:
                result = self.emergency_stop() or {}
                self.last_executed = True
                self.last_error = None
                return {"matched": True, "command": self.last_command, "emergency": True, **result}
            except Exception as exc:
                self.last_executed = False
                self.last_error = str(exc)
                return {"matched": True, "command": self.last_command, "ok": False, "message": str(exc)}
        command = got
        if enforce_wake:
            now = time.monotonic()
            # Phone voice_text may still arrive as two utterances: "体感" ... "截图".
            # Keep the same short wake window for that compatibility path.
            if wake and command == wake:
                self.last_wake_at = now
                self.wake_until = now + WAKE_COMMAND_WINDOW_SECONDS
                self.last_command = self.wake_word
                self.last_action = "wake"
                self.last_executed = None
                return {"matched": True, "wake": True, "pending_command": True, "window_seconds": WAKE_COMMAND_WINDOW_SECONDS}
            if wake and command.startswith(wake):
                command = command[len(wake):]
                self.wake_until = 0.0
            elif self.wake_until and now <= self.wake_until:
                self.wake_until = 0.0
            else:
                self.wake_until = 0.0
                return {"matched": False, "reason": "wake_word_required"}
        match = None
        for mapping in self.mappings:
            phrases = [mapping["phrase"], *mapping.get("synonyms", [])]
            if command in {compact_text(item) for item in phrases}:
                match = mapping
                break
        if match is None:
            return {"matched": False, "reason": "command_not_in_mapping"}
        action = {"type": match["type"], "target": match["target"]}
        action["source"] = f"voice:{source_id}" if source_id else "voice"
        if match["type"] == "system":
            action["voice_source_id"] = source_id
            action["voice_source_kind"] = self.source_kind
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

        threading.Thread(target=run, name="voice-command", daemon=True).start()
        return {"matched": True, "command": match["phrase"], "pending": True}

    def _mic_callback(self, indata, frames, time_info, status) -> None:
        data = bytes(indata)
        if status:
            with self._lock:
                self.last_error = f"电脑麦克风状态：{status}"
        if not data or self._mic_queue is None:
            return
        try:
            self._mic_queue.put_nowait(data)
        except queue.Full:
            with self._lock:
                self.last_error = "电脑麦克风处理队列已满"

    def start_local_microphone(self) -> dict:
        self.stop_local_microphone()
        with self._lock:
            self.model_path = find_vosk_model(self.root)
            if self.recognizer is None:
                self._rebuild_recognizer()
            if self.recognizer is None:
                self.connected = False
                return self.status()
        try:
            import sounddevice as sd
        except ImportError:
            with self._lock:
                self.last_error = "电脑语音需要 sounddevice；当前 Python 环境未安装"
                self.connected = False
                return self.status()
        source_id = "computer_microphone"
        self._mic_queue = queue.Queue(maxsize=24)
        self._mic_stop.clear()
        with self._lock:
            self._activate_locked(source_id, "computer", "computer")
            self.audio_ready = True
            self.last_error = None
        self._mic_thread = threading.Thread(target=self._mic_loop, args=(source_id,), name="voice-microphone", daemon=True)
        self._mic_thread.start()
        try:
            self._mic_stream = sd.RawInputStream(
                samplerate=self.sample_rate, channels=1, dtype="int16", blocksize=1600,
                callback=self._mic_callback,
            )
            self._mic_stream.start()
        except Exception as exc:
            self._mic_stop.set()
            with self._lock:
                self.last_error = f"电脑麦克风启动失败：{exc}"
            self.stop_local_microphone()
        return self.status()

    def _mic_loop(self, source_id: str) -> None:
        while not self._mic_stop.is_set():
            try:
                data = self._mic_queue.get(timeout=0.20) if self._mic_queue is not None else None
            except queue.Empty:
                continue
            if not data:
                continue
            try:
                with self._lock:
                    if self.source_id != source_id or not self.connected:
                        continue
                    self._ingest_pcm_locked(source_id, data)
            except Exception as exc:
                with self._lock:
                    self.last_error = str(exc)

    def stop_local_microphone(self) -> dict:
        self._mic_stop.set()
        stream = self._mic_stream
        self._mic_stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        thread = self._mic_thread
        self._mic_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._lock:
            if self.source_kind == "computer":
                self._release_locked(self.source_id)
                self.connected = False
                self.audio_ready = False
                self.source_id = None
                self.device_id = None
                self.source_kind = None
        return self.status()

    def disconnect(self, source_id: str | None = None) -> dict:
        with self._lock:
            if source_id is not None and self.source_id not in {None, str(source_id)}:
                return self.status()
            self._release_locked(self.source_id)
            self.connected = False
            self.audio_ready = False
            self.source_id = None
            self.device_id = None
            self.source_kind = None
            self.last_partial = ""
            return self.status()

    def source_is_active(self, source_id: str | None) -> bool:
        """Check the exact voice source before a deferred system action runs."""
        with self._lock:
            return bool(source_id and self.connected and self.source_id == str(source_id))

    def status(self) -> dict:
        now = time.monotonic()
        alive = bool(self.source_kind == "computer" and self.connected and self.last_audio_at and now - self.last_audio_at < VOICE_TIMEOUT_SECONDS)
        result = {
            "available": self.recognizer is not None,
            "model_ready": bool(self.recognizer is not None and self.model_path is not None),
            "model_path": str(self.model_path) if self.model_path else None,
            "recognizer_mode": self.recognizer_mode,
            "supported_count": self.supported_count,
            "unsupported": list(self.unsupported),
            "mappings": list(self.mappings),
            "wake_word": self.wake_word,
            "emergency_stop_phrases": list(self.emergency_stop_phrases),
            "connected": self.connected,
            "source": self.source_id,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "device_id": self.device_id,
            "audio_ready": self.audio_ready,
            "partial": self.last_partial,
            "final": self.last_final,
            "last_partial": self.last_partial,
            "last_final": self.last_final,
            "last_command": self.last_command,
            "last_action": self.last_action,
            "last_executed": self.last_executed,
            "last_error": self.last_error,
            "audio_alive": alive,
            "stream_alive": alive,
        }
        if self.source_kind == "computer":
            result.update({
                "bytes_received": self.audio_bytes,
                "rms": round(self.last_rms, 1),
                "peak_rms": round(self.peak_rms, 1),
                "audio_seconds": round(self.audio_bytes / (self.sample_rate * 2), 2),
            })
        return result

    def close(self) -> None:
        self.stop_local_microphone()
        recognizer = self.recognizer
        self.recognizer = None
        if recognizer is not None:
            try:
                recognizer.close()
            except Exception:
                pass
        self.disconnect()
