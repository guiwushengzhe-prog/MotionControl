from __future__ import annotations

import base64
import json
import math
import os
import queue
import sys
import threading
import time
import copy
from contextlib import contextmanager
import tempfile
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SYSTEM_HEAD_CALIBRATION_START = "HEAD_CALIBRATION_START"
VOICE_TIMEOUT_SECONDS = 1.5
WAKE_COMMAND_WINDOW_SECONDS = 3.5
# 每个游戏自己的 12 句口令。其余内置口令都是系统功能，不跟游戏走。
PROFILE_SLOT_PREFIX = "game.profile_slot_"
MAX_AUDIO_FRAME_BYTES = 256 * 1024
MAX_AUDIO_AGE_SECONDS = 0.5
AUDIO_QUEUE_CAPACITY = 8


@dataclass(frozen=True)
class _AudioFrame:
    source_id: str
    device_id: str
    pcm16: bytes
    queued_at: float
    generation: int
    sequence: int
    callback: Callable | None = None



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


# 小模型的词表里单字不全：常用 3755 个汉字里有 670 个查不到单字，其中很多只以整词
# 出现——「堡」只在「城堡」里、「啡」只在「咖啡」里。整词最长试到几个字。
MAX_GRAMMAR_WORD_CHARS = 4


def _is_cjk(char: str) -> bool:
    return "一" <= char <= "鿿"


def grammar_tokens(text: str, known: Callable[[str], bool]) -> tuple[list[str], list[str]]:
    """把一句口令拆成模型词表里的词，返回 (词, 认不出的字)。

    能按单字就按单字：这是 2026-08-28 对比实验验过的拆法，词表齐全的口令拆出来和
    以前一模一样。某个字单独查不到，才试着把它和前后的字拼成词表里的整词。怎么拼
    都拼不上的字进第二个列表——带着它的口令说多少遍都不会被听到。
    """
    text = compact_text(text)
    if not text:
        return [], []
    if not any(_is_cjk(char) for char in text):
        return ([text], []) if known(text) else ([], [text])
    # best[i]：前 i 个字最好的拆法。先比认不出的字少，再比用的整词少。
    best: list[tuple[int, int, list[str], list[str]] | None] = [None] * (len(text) + 1)
    best[0] = (0, 0, [], [])
    for start in range(len(text)):
        if best[start] is None:
            continue
        missing, words, tokens, unheard = best[start]
        for end in range(start + 1, min(len(text), start + MAX_GRAMMAR_WORD_CHARS) + 1):
            piece = text[start:end]
            if end == start + 1:
                heard = known(piece)
                candidate = (missing + (not heard), words, [*tokens, piece], unheard if heard else [*unheard, piece])
            elif known(piece):
                candidate = (missing, words + 1, [*tokens, piece], unheard)
            else:
                continue
            if best[end] is None or candidate[:2] < best[end][:2]:
                best[end] = candidate
    _missing, _words, tokens, unheard = best[len(text)]
    return tokens, list(dict.fromkeys(unheard))


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
        # Vosk's loader opens files through the narrow-character Windows API, so
        # a model under D:\游戏 or C:\Users\张三 fails with "does not contain
        # model files" even though it is complete.  Hand it a path it can open.
        loadable, tier = resolve_loadable_model_path(model_path)
        self.model_path_tier = tier
        if tier == "mirror":
            print(f"语音模型路径含非 ASCII 字符，已镜像到：{loadable}")
        elif tier == "unavailable":
            raise RuntimeError(
                f"语音模型路径含中文且无法镜像到纯英文路径：{model_path}。"
                "请把 MotionControl 或模型放在不含中文的路径下，例如 C:/MotionControl。")
        self._model = Model(str(loadable))
        self._known: dict[str, bool] = {}
        self.sample_rate = int(sample_rate)
        # The small Chinese model grammar is word-token based and its words are
        # mostly single characters.  The robustness run proved that unspaced
        # whole phrases are discarded as unknown vocabulary, while parser-side
        # compact_text safely rejoins the spaced tokens.
        #
        # \u8bcd\u8868\u91cc\u6ca1\u6709\u7684\u8bcd Vosk \u53ea\u662f\u6084\u6084\u4e22\u6389\uff0c\u4e0d\u62a5\u9519\uff1b\u5e26\u7740\u5b83\u7684\u53e3\u4ee4\u5c31\u6c38\u8fdc\u542c\u4e0d\u5230\u3002\u6240\u4ee5
        # \u8fd9\u79cd\u53e3\u4ee4\u5e72\u8106\u4e0d\u8fdb grammar\uff0c\u8bb0\u5728 unheard \u91cc\u8ba9\u754c\u9762\u7167\u5b9e\u8bf4\u3002
        self.supported: list[str] = []
        self.unsupported: list[str] = []
        self.unheard: dict[str, list[str]] = {}
        for item in dict.fromkeys(phrases):
            if not compact_text(item):
                continue
            tokens, missing = self.tokens_for(item)
            if missing:
                self.unsupported.append(item)
                self.unheard[item] = missing
            else:
                self.supported.append(" ".join(tokens))
        self.supported = list(dict.fromkeys(self.supported))
        self.mode = "vosk_constrained_grammar"
        self._grammar = json.dumps([*self.supported, "[unk]"], ensure_ascii=False)
        self._recognizer = None
        self.last_partial = ""
        self.reset()

    def knows(self, word: str) -> bool:
        """\u8fd9\u4e2a\u8bcd\u5728\u4e0d\u5728\u6a21\u578b\u8bcd\u8868\u91cc\u3002\u8001\u7248\u672c\u7684 vosk \u67e5\u4e0d\u4e86\uff0c\u5c31\u5f53\u90fd\u5728\uff0c\u548c\u4ee5\u524d\u4e00\u6837\u3002"""
        cached = self._known.get(word)
        if cached is None:
            finder = getattr(self._model, "vosk_model_find_word", None)
            cached = self._known[word] = finder is None or finder(word) >= 0
        return cached

    def tokens_for(self, phrase: str) -> tuple[list[str], list[str]]:
        return grammar_tokens(phrase, self.knows)

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


from motioncontrol.ascii_model_path import resolve_loadable_model_path
from motioncontrol.user_paths import user_path
from motioncontrol_shared.text_norm import compact_text
from motioncontrol_shared.mapping_schema import (
    DEFAULT_EMERGENCY_STOP,
    DEFAULT_WAKE_WORD,
    normalize_emergency_phrases,
    normalize_voice_mappings,
    normalize_wake_word,
)


class VoiceService:
    """Shared voice parser for the computer microphone and phone voice_text."""

    def __init__(
        self,
        root: Path,
        execute_action: Callable[[dict], dict],
        emergency_stop: Callable[[], dict] | None = None,
        clear_source: Callable[[str], dict] | None = None,
        on_system_command: Callable[[dict], dict] | None = None,
    ) -> None:
        self.root = root
        # User data: kept out of the program folder so an upgrade does not
        # discard custom phrases.
        self.config_path = user_path("voice_mappings")
        # 唤醒词和急停口令单独一份。见 _load_personal 的说明。
        self.personal_path = user_path("personal_voice")
        self.execute_action = execute_action
        self.emergency_stop = emergency_stop or (lambda: {"executed": True})
        self.clear_source = clear_source or (lambda _source: {})
        self.on_system_command = on_system_command
        self.sample_rate = 16_000
        self.mappings: list[dict] = []
        self.wake_word = ""
        self.wake_system_commands = True
        self.emergency_stop_phrases: list[str] = [DEFAULT_EMERGENCY_STOP]
        self.model_path = find_vosk_model(root)
        self.action_map_file = root / "config" / "generated_voice" / "voice_action_map.json"
        self.command_registry: dict[str, dict] = {}
        self._catalog: list[dict] = []
        self._profile_bindings: dict = {}
        self._phrase_index: dict[str, dict] = {}
        self._configuration_conflicts: tuple[str, ...] = ()
        self.recognizer: VoskCommandRecognizer | None = None
        self.recognizer_mode = "vosk_constrained_grammar"
        self.supported_count = 0
        self.unsupported: list[str] = []
        # 口令 → 里面模型认不出的字。带着这些字的口令不进 grammar，永远听不到。
        self.unheard: dict[str, list[str]] = {}
        self.last_partial = ""
        self.last_final = ""
        self.last_command: str | None = None
        # 听到并认出了几句口令（只说唤醒词不算）。界面靠它知道「刚刚又说了一句」——
        # 光看 last_command 不行，同一句说两遍它不会变。
        self.commands_heard = 0
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
        self.audio_device: int | None = None
        self.audio_device_name: str | None = None
        self._mic_stream = None
        self._mic_queue: queue.Queue[_AudioFrame] | None = None
        self._mic_stop = threading.Event()
        self._mic_thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._audio_submit_lock = threading.Lock()
        self._phone_queue: queue.Queue[_AudioFrame] = queue.Queue(maxsize=AUDIO_QUEUE_CAPACITY)
        self._phone_stop = threading.Event()
        self._phone_thread: threading.Thread | None = None
        self._audio_generation = 0
        self._invalidated_phone_sources: dict[str, int] = {}
        self._mic_sequence = 0
        self._phone_sequence = 0
        self._mic_gap = threading.Event()
        self._callback_error: str | None = None
        self.audio_dropped_frames = 0
        self.audio_stream_resets = 0
        self._voice_journal = self.config_path.parent / ".voice-config-transaction.json"
        self._recover_configuration()
        self._load()
        self._load_command_registry()
        self._drop_shadowed_mappings()
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
                self._load_personal({})
                return
        legacy: dict = {}
        try:
            legacy = json.loads(source.read_text(encoding="utf-8"))
            self.mappings = self._validate_mappings(legacy.get("mappings", []))
        except Exception as exc:
            self.last_error = f"语音配置读取失败：{exc}"
        self._load_personal(legacy)
        if source != self.config_path:
            self._write_config()

    def _load_personal(self, legacy) -> None:
        """读唤醒词和急停口令。它们存在自己那一份里，不和口令映射放一起。

        这两项是**你的**，不是某个游戏的：换游戏不变，而且别人下载你分享的语音
        配置时，不该把他的唤醒词换成你的。以前它们和口令映射挤在同一个文件里，
        于是装一份别人分享的配置就会顺手把唤醒词覆盖掉——那台机器的主人只会
        发现"我的唤醒词自己变了"，根本想不到是装配置装的。

        老安装里这两项还在旧文件里，所以读不到新文件时从旧的那份搬过来并落盘。
        搬家对用户是无感的：唤醒词还是那个唤醒词。
        """
        migrating = not self.personal_path.is_file()
        if migrating:
            data = legacy if isinstance(legacy, dict) else {}
        else:
            try:
                data = json.loads(self.personal_path.read_text(encoding="utf-8"))
            except Exception as exc:
                self.last_error = f"个人语音设置读取失败：{exc}"
                return
        try:
            self.wake_word = self._validate_wake_word(data.get("wake_word", ""))
            # 系统口令和通用口令统一使用用户填写的前缀，留空就直接说。
            # 旧开关只保留为配置兼容字段，不再固定要求“体感”。
            system_rule_changed = data.get("wake_system_commands") is not True
            self.wake_system_commands = True
            self.emergency_stop_phrases = self._validate_emergency_phrases(
                data.get("emergency_stop_phrases", []))
            # 旧的自定义急停以“体感”存前缀；新版保存用户填写的部分，前缀单独应用。
            legacy_rules = data.get("voice_rules_version") != 2
            if legacy_rules:
                self.emergency_stop_phrases = [
                    item if item == DEFAULT_EMERGENCY_STOP else self._without_wake_word(item)
                    for item in self.emergency_stop_phrases]
        except Exception as exc:
            self.last_error = f"个人语音设置读取失败：{exc}"
            return
        if migrating or legacy_rules or system_rule_changed:
            self._write_personal()

    # The rules live in motioncontrol_shared.mapping_schema so the cloud applies
    # exactly the same ones; these stay as the desktop's entry points.
    _validate_wake_word = staticmethod(normalize_wake_word)
    _validate_emergency_phrases = staticmethod(normalize_emergency_phrases)
    _validate_mappings = staticmethod(normalize_voice_mappings)

    def _write_config(self) -> None:
        """只写口令映射。唤醒词和急停口令在 _write_personal 那一份里。"""
        self._atomic_bytes(self.config_path, json.dumps(
            {"mappings": self.mappings}, ensure_ascii=False, indent=2).encode("utf-8"))

    def _write_personal(self) -> None:
        self._atomic_bytes(self.personal_path, json.dumps({
                "wake_word": self.wake_word,
                "wake_system_commands": self.wake_system_commands,
                "voice_rules_version": 2,
                "emergency_stop_phrases": self.emergency_stop_phrases,
            }, ensure_ascii=False, indent=2).encode("utf-8"))

    @staticmethod
    def _stage_bytes(path: Path, content: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                             suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            return temporary
        except Exception:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise

    @classmethod
    def _atomic_bytes(cls, path: Path, content: bytes) -> None:
        temporary = cls._stage_bytes(path, content)
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _restore_configuration(self, previous: dict) -> None:
        for name, path in (("mappings", self.config_path), ("personal", self.personal_path)):
            content = previous[name]
            if content is None:
                path.unlink(missing_ok=True)
            else:
                restored = base64.b64decode(content, validate=True)
                if not path.is_file() or path.read_bytes() != restored:
                    self._atomic_bytes(path, restored)

    def _recover_configuration(self) -> None:
        # An interrupted two-file commit must be resolved before either file is
        # loaded. Journal keys map to fixed paths; the journal cannot select paths.
        if self._voice_journal.is_file():
            previous = json.loads(self._voice_journal.read_text(encoding="utf-8"))
            self._restore_configuration(previous)
            self._voice_journal.unlink()

    def _save_configuration(self) -> None:
        paths = {"mappings": self.config_path, "personal": self.personal_path}
        contents = {
            "mappings": json.dumps({"mappings": self.mappings}, ensure_ascii=False, indent=2).encode("utf-8"),
            "personal": json.dumps({"wake_word": self.wake_word,
                                     "wake_system_commands": self.wake_system_commands,
                                     "voice_rules_version": 2,
                                     "emergency_stop_phrases": self.emergency_stop_phrases},
                                    ensure_ascii=False, indent=2).encode("utf-8"),
        }
        previous = {name: base64.b64encode(path.read_bytes()).decode("ascii") if path.exists() else None
                    for name, path in paths.items()}
        staged: dict[str, Path] = {}
        journal_written = False
        try:
            for name, path in paths.items():
                staged[name] = self._stage_bytes(path, contents[name])
            self._atomic_bytes(self._voice_journal, json.dumps(previous).encode("utf-8"))
            journal_written = True
            for name, path in paths.items():
                os.replace(staged[name], path)
            self._voice_journal.unlink()
            journal_written = False
        except Exception:
            if journal_written:
                # Keep the journal when recovery itself fails, so a subsequent
                # launch can retry instead of treating a mixed pair as committed.
                self._restore_configuration(previous)
                self._voice_journal.unlink()
            raise
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)

    def configure(self, items, *, wake_word=None, wake_system_commands=None, emergency_stop_phrases=None) -> dict:
        with self._lock:
            mappings = self._validate_mappings(items)
            wake = self._validate_wake_word(wake_word) if wake_word is not None else self.wake_word
            stops = (self._validate_emergency_phrases(emergency_stop_phrases)
                     if emergency_stop_phrases is not None else self.emergency_stop_phrases)
            # Build and persist a candidate without publishing it to lock-free
            # audio producers/status readers. A failed save leaves the live
            # settings, recognizer and held outputs exactly as they were.
            candidate = copy.copy(self)
            candidate.mappings, candidate.wake_word, candidate.emergency_stop_phrases = mappings, wake, stops
            # 接受旧客户端传来的开关字段，但所有系统口令均使用同一唤醒词。
            candidate.wake_system_commands = True
            candidate._build_registry()
            problems = candidate._configuration_conflicts
            if problems:
                raise ValueError(f"{problems[0]}，换一个说法")
            candidate._save_configuration()
            self.mappings, self.wake_word, self.emergency_stop_phrases = mappings, wake, stops
            self.wake_system_commands = candidate.wake_system_commands
            self.command_registry, self._phrase_index = candidate.command_registry, candidate._phrase_index
            self._configuration_conflicts = candidate._configuration_conflicts
            self._release_locked(self.source_id)
            with self._audio_submit_lock:
                self._audio_generation += 1
            self._rebuild_recognizer()
            return self.status()

    def _load_command_registry(self) -> None:
        """Read the shipped built-in commands.  Kept as read, never edited in place:
        the current game's phrases are laid over a fresh copy each time (see
        _commands_for), so switching games cannot carry one game's phrases into
        the next, and saving the shared phrases cannot wipe the game's."""
        self._catalog = []
        if self.action_map_file.is_file():
            try:
                data = json.loads(self.action_map_file.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    for phrase, raw in data.items():
                        if not isinstance(raw, dict): continue
                        command = dict(raw); command["phrase"] = str(phrase)
                        command["synonyms"] = list(command.get("synonyms", []) or [])
                        self._catalog.append(command)
            except Exception as exc:
                self.last_error = f"语音命令注册表读取失败：{exc}"
        self._build_registry()

    @staticmethod
    def _is_game_profile_command(command_id: str) -> bool:
        """本游戏口令按栏目免唤醒；栏目外的命令仍走唤醒词门禁。"""
        return str(command_id or "").startswith(PROFILE_SLOT_PREFIX)

    def _without_wake_word(self, phrase: str) -> str:
        """去掉旧版本已经保存进本游戏口令的唤醒词前缀。"""
        value = compact_text(str(phrase or ""))
        for prefix in (self.wake_word, DEFAULT_WAKE_WORD):
            prefix = compact_text(prefix)
            if prefix and value.startswith(prefix) and value != prefix:
                return value[len(prefix):]
        return value

    def _commands_for(self, bindings: dict | None) -> list[dict]:
        """Built-in commands with a game's phrases laid over them.

        本游戏口令保留为不带唤醒词的短语；栏目外的内置命令仍带唤醒词。

        A list rather than a dict keyed by phrase: two commands given the same
        phrase must both stay visible, or the check that forbids it cannot see it.
        """
        commands = copy.deepcopy(self._catalog)
        voice_bindings = bindings.get("voice", {}) if isinstance(bindings, dict) else {}
        by_id = {str(item.get("id")): item for item in commands}
        for command_id, binding in (voice_bindings.items() if isinstance(voice_bindings, dict) else []):
            command = by_id.get(str(command_id))
            if command is None or not isinstance(binding, dict): continue
            phrase = str(binding.get("phrase", "")).strip()
            if phrase:
                if self._is_game_profile_command(command_id):
                    phrase = self._without_wake_word(phrase)
                elif self.system_wake_word and not compact_text(phrase).startswith(compact_text(self.system_wake_word)):
                    phrase = f"{self.system_wake_word}{phrase}"
                command["phrase"] = phrase
            aliases = binding.get("synonyms", []); command["synonyms"] = []
            for item in aliases if isinstance(aliases, list) else []:
                alias = str(item).strip()
                if alias:
                    if self._is_game_profile_command(command_id):
                        alias = self._without_wake_word(alias)
                    elif self.system_wake_word and not compact_text(alias).startswith(compact_text(self.system_wake_word)):
                        alias = f"{self.system_wake_word}{alias}"
                    command["synonyms"].append(alias)
        for command in commands:
            if self._is_game_profile_command(command.get("id")):
                command["phrase"] = self._without_wake_word(str(command.get("phrase", "")))
                aliases = [self._without_wake_word(str(alias))
                           for alias in command.get("synonyms", []) or []]
                # 保留旧版本“唤醒词 + 本游戏口令”的语法别名；主短语仍显示为免唤醒写法。
                if command["phrase"]:
                    aliases.append(f"{DEFAULT_WAKE_WORD}{command['phrase']}")
                    aliases.append(f"{self.wake_word}{command['phrase']}")
                command["synonyms"] = list(dict.fromkeys(alias for alias in aliases if alias))
            else:
                command["phrase"] = self._with_wake_word(str(command.get("phrase", "")))
                command["synonyms"] = [self._with_wake_word(str(alias)) for alias in command.get("synonyms", []) or []]
        return commands

    def _build_registry(self) -> None:
        commands = self._commands_for(self._profile_bindings)
        self.command_registry = {compact_text(item["phrase"]): item
                                 for item in commands}
        # Conflicts only change with configuration. Compute from the complete
        # list before equal primary phrases are collapsed by the registry;
        # status/audio reads can then copy a small immutable result.
        self._configuration_conflicts = tuple(self._phrase_conflicts(commands))
        self._rebuild_phrase_index()

    def _phrase_conflicts(self, commands: list[dict]) -> list[str]:
        """每一句口令只能有一个主人。

        以前内置口令、本游戏口令、通用口令可以同名，识别时内置的先匹配，于是
        通用口令里和它同名的那几条从来没生效过，界面上却看着能改——表上写闪避
        是空格，实际按的是 Shift。现在同名直接不让存。
        """
        owners: dict[str, tuple[str, str]] = {}
        problems: list[str] = []

        def claim(phrase: str, label: str, ident: str) -> None:
            key = compact_text(phrase)
            if not key:
                return
            other = owners.get(key)
            if other is None:
                owners[key] = (label, ident)
            elif other[1] != ident:
                both = f"{other[0]}和{label}" if other[0] != label else f"两条{label}"
                problems.append(f"「{phrase}」同时是{both}")

        for phrase in self.spoken_emergency_phrases():
            claim(phrase, "急停口令", "emergency")
        for command in commands:
            cid = str(command.get("id", ""))
            # 急停那条和上面的急停口令是同一件事，内置的那句总会被加回急停口令里。
            if cid == "system.emergency_stop":
                continue
            label = "本游戏口令" if cid.startswith(PROFILE_SLOT_PREFIX) else "内置口令"
            for phrase in [command.get("phrase", ""), *(command.get("synonyms") or [])]:
                claim(str(phrase), label, cid)
                if (self._is_game_profile_command(cid)
                        and not compact_text(str(phrase)).startswith(compact_text(self.wake_word))):
                    # 兼容用户仍然说旧的“唤醒词 + 游戏口令”时，不能把同一句
                    # 话同时留给通用口令。
                    claim(f"{self.wake_word}{phrase}", label, cid)
        for index, mapping in enumerate(self.mappings):
            for phrase in [mapping["phrase"], *mapping.get("synonyms", [])]:
                claim(f"{self.wake_word}{phrase}", "通用口令", f"mapping:{index}")
        return problems

    def check_profile_phrases(self, bindings: dict | None) -> None:
        """在存一个游戏的口令之前调用：和内置口令、通用口令、其他口令重名就不存。"""
        with self._lock:
            problems = self._phrase_conflicts(self._commands_for(bindings))
        if problems:
            raise ValueError(f"{problems[0]}，换一个说法")

    def _drop_shadowed_mappings(self) -> None:
        """老版本带的通用口令里有几条和内置口令同名（开始校准、截图……）。内置的
        先匹配，它们从来没生效过。说法现在要求互斥，读进来时把这几条去掉。"""
        taken = {compact_text(phrase) for phrase in self.spoken_emergency_phrases()}
        for command in self.command_registry.values():
            if str(command.get("id", "")).startswith(PROFILE_SLOT_PREFIX):
                continue
            taken.update(compact_text(phrase) for phrase in [command.get("phrase", ""), *(command.get("synonyms") or [])])
        kept = []
        for mapping in self.mappings:
            if compact_text(f"{self.wake_word}{mapping['phrase']}") in taken:
                continue
            synonyms = [alias for alias in mapping.get("synonyms", [])
                        if compact_text(f"{self.wake_word}{alias}") not in taken]
            entry = {key: value for key, value in mapping.items() if key != "synonyms"}
            if synonyms:
                entry["synonyms"] = synonyms
            kept.append(entry)
        if kept != self.mappings:
            self.mappings = kept
            self._build_registry()
            try:
                self._write_config()
            except OSError as exc:
                self.last_error = f"语音配置保存失败：{exc}"

    def _rebuild_phrase_index(self) -> None:
        self._phrase_index = {}
        for command in self.command_registry.values():
            phrase = compact_text(command.get("phrase", ""))
            if phrase: self._phrase_index[phrase] = command
            for alias in command.get("synonyms", []) or []:
                alias = compact_text(alias)
                if alias: self._phrase_index[alias] = command

    def _with_wake_word(self, phrase: str) -> str:
        if not phrase.startswith(DEFAULT_WAKE_WORD): return phrase
        return self.system_wake_word + phrase[len(DEFAULT_WAKE_WORD):]

    @property
    def system_wake_word(self) -> str:
        return self.wake_word

    def spoken_emergency_phrases(self) -> list[str]:
        return [self._with_wake_word(item) if item == DEFAULT_EMERGENCY_STOP else self.wake_word + item
                for item in self.emergency_stop_phrases]

    def configure_profile_bindings(self, bindings: dict | None) -> None:
        """Overlay editable per-game trigger words on shipped command IDs."""
        with self._lock:
            self._profile_bindings = copy.deepcopy(bindings) if isinstance(bindings, dict) else {}
            self._build_registry()
            with self._audio_submit_lock:
                self._audio_generation += 1
            self._rebuild_recognizer()

    def grammar_phrases(self) -> list[str]:
        """Every phrase the constrained grammar must accept.

        The phone runs its own recognizer over the same small Chinese model, so
        it needs this exact list to hear anything the desktop can act on.  It
        used to hold a hard-coded copy, which silently drifted: a phrase added
        here was recognised by the computer microphone and by nothing else.
        """
        # 唤醒词是可选前缀，不是一个独立动作。
        # 把裸「体感」放进 Choices 会让 Windows 语音引擎在完整口令
        # （例如「体感跳跃」）还没说完时就提前选中它，造成前缀抢占。
        phrases = [*self.spoken_emergency_phrases()]
        for mapping in self.mappings:
            commands = [mapping["phrase"], *mapping.get("synonyms", [])]
            phrases.extend(f"{self.wake_word}{command}" for command in commands)
        # The user-facing command catalog is canonical. Legacy mappings
        # remain aliases, but they are no longer a separate behavior path.
        for command in self.command_registry.values():
            phrases.append(command.get("phrase", ""))
            phrases.extend(command.get("synonyms", []) or [])
        return [phrase for phrase in dict.fromkeys(phrases) if compact_text(phrase)]

    def grammar_entries(self) -> list[str]:
        """这台电脑实际交给模型的 grammar：每句口令已经按词表拆好、空格隔开。

        手机用的是同一个模型，照这份建 grammar 就和电脑听到的一样。以前手机拿
        grammar_phrases 自己逐字拆，「城堡」这种只能按整词认的口令电脑听得到、
        手机听不到。电脑上没有模型时是空的，手机退回自己拆。
        """
        recognizer = self.recognizer
        return list(recognizer.supported) if recognizer is not None else []

    def check_phrases(self, phrases: list[str]) -> dict:
        """界面上正在填的口令里，有没有模型认不出的字。"""
        recognizer = self.recognizer
        if recognizer is None:
            return {"available": False, "results": []}
        results = []
        for phrase in phrases:
            phrase = str(phrase or "").strip()
            _tokens, missing = recognizer.tokens_for(phrase) if compact_text(phrase) else ([], [])
            results.append({"phrase": phrase, "unheard": missing})
        return {"available": True, "results": results}

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
        self.unheard = {}
        self.audio_ready = False
        # 受限 Vosk 语法是电脑和手机共用的识别路径。Windows 系统语音的多选项
        # 语法在中文口令上会把多个候选合成一张不可用的语法图，因此不再使用。
        self.model_path = find_vosk_model(self.root)
        try:
            if self.model_path is None:
                raise RuntimeError("Vosk 中文模型不存在")
            self.recognizer = VoskCommandRecognizer(self.model_path, self.grammar_phrases(), self.sample_rate)
            self.recognizer_mode = self.recognizer.mode
            self.supported_count = len(self.recognizer.supported)
            self.unsupported = list(self.recognizer.unsupported)
            self.unheard = dict(self.recognizer.unheard)
            self.last_error = None
            self.audio_ready = True
        except Exception as exc:
            self.recognizer = None
            self.recognizer_mode = "off"
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

    def _activate_locked(self, source_id: str, device_id: str, source_kind: str, *,
                         expected_generation: int | None = None) -> bool:
        with self._audio_submit_lock:
            if expected_generation is not None and expected_generation != self._audio_generation:
                return False
            if source_kind == "phone" and source_id in self._invalidated_phone_sources:
                self._invalidated_phone_sources.pop(source_id)
                self._audio_generation += 1
            previous_source = self.source_id
            changed = previous_source != source_id or not self.connected
            self.source_id = source_id
            self.device_id = device_id
            self.source_kind = source_kind
            self.connected = True
        if previous_source and changed:
            self._release_locked(previous_source)
        self.audio_ready = self.recognizer is not None
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
        return True

    def connect_phone_text(self, source_id: str, device_id: str) -> dict:
        with self._lock:
            self._activate_locked(str(source_id), str(device_id), "phone")
            return self.status()

    def accept_phone_text(self, source_id: str, device_id: str, text: str, confidence: float | None = None,
                          *, expected_generation: int | None = None) -> tuple[dict, dict | None]:
        text = str(text or "").strip()
        if not text or len(text) > 96:
            raise ValueError("voice_text 必须是 1 到 96 个字符")
        with self._lock:
            if self.source_kind == "computer":
                return self.status(), {"matched": False, "reason": "computer_voice_source_active"}
            if not self._activate_locked(str(source_id), str(device_id), "phone",
                                         expected_generation=expected_generation):
                return self.status(), {"matched": False, "reason": "voice_source_generation_changed"}
            self.last_final = text
            self.last_partial = ""
            self.last_audio_at = time.monotonic()
            result = self._match_and_execute(text, source_id=str(source_id), enforce_wake=True)
            return self.status(), result

    def accept_phone_audio(
        self,
        source_id: str,
        device_id: str,
        pcm16: bytes,
        *,
        sequence: int | None = None,
        captured_at_ms: float | None = None,
        result_callback: Callable[[dict | None, dict | None], None] | None = None,
        expected_generation: int | None = None,
    ) -> tuple[dict, dict | None, dict | None]:
        """Queue phone PCM without putting recognition on its WebSocket reader.

        The three-item return remains compatible with older bridge callers;
        asynchronous results use the optional callback outside the voice lock.
        """
        if not pcm16 or len(pcm16) % 2 or len(pcm16) > MAX_AUDIO_FRAME_BYTES:
            raise ValueError("音频必须是有界的 16kHz 单声道 PCM16")
        if self.source_kind == "computer":
            return self.status(), None, {"matched": False, "reason": "computer_voice_source_active"}
        capture_age = max(0.0, time.time() - captured_at_ms / 1000.0) if captured_at_ms is not None else 0.0
        with self._audio_submit_lock:
            if expected_generation is not None and expected_generation != self._audio_generation:
                return self.status(), None, {"matched": False, "reason": "voice_source_generation_changed"}
            if self._phone_stop.is_set():
                return self.status(), None, {"matched": False, "reason": "voice_service_closed"}
            self._phone_sequence += 1
            if capture_age > MAX_AUDIO_AGE_SECONDS:
                self.audio_dropped_frames += 1
                return self.status(), None, {"matched": False, "reason": "audio_frame_stale"}
            if str(source_id) in self._invalidated_phone_sources:
                self._invalidated_phone_sources.pop(str(source_id))
                self._audio_generation += 1
            frame = _AudioFrame(str(source_id), str(device_id), bytes(pcm16), time.monotonic() - capture_age,
                                self._audio_generation, sequence if sequence is not None else self._phone_sequence,
                                result_callback)
            try:
                self._phone_queue.put_nowait(frame)
            except queue.Full:
                self.audio_dropped_frames += 1
                return self.status(), None, None
            if self._phone_thread is None or not self._phone_thread.is_alive():
                self._phone_thread = threading.Thread(target=self._phone_loop,
                                                      name="voice-phone-audio", daemon=True)
                self._phone_thread.start()
        return self.status(), None, None

    def _phone_loop(self) -> None:
        previous: tuple[str, int, int] | None = None
        gap = False
        while not self._phone_stop.is_set():
            try:
                frame = self._phone_queue.get(timeout=0.20)
            except queue.Empty:
                continue
            try:
                ident = (frame.source_id, frame.generation, frame.sequence)
                if previous is not None and ident[:2] == previous[:2] and ident[2] <= previous[2]:
                    self.audio_dropped_frames += 1
                    continue
                discontinuous = (previous is not None and
                                 (ident[:2] != previous[:2] or ident[2] != previous[2] + 1))
                outcome = self._consume_audio(frame, "phone", reset=gap or discontinuous)
                if outcome is None:
                    gap = True
                    continue
                previous, gap = ident, False
                event, result = outcome
                if frame.callback is not None and (event or result):
                    frame.callback(event, result)
            except Exception as exc:
                gap = True
                with self._lock:
                    self.last_error = str(exc)
            finally:
                self._phone_queue.task_done()

    def _consume_audio(self, frame: _AudioFrame, kind: str, *, reset: bool = False):
        if frame.generation != self._audio_generation or time.monotonic() - frame.queued_at > MAX_AUDIO_AGE_SECONDS:
            self.audio_dropped_frames += 1
            return None
        with self._lock:
            if frame.generation != self._audio_generation or time.monotonic() - frame.queued_at > MAX_AUDIO_AGE_SECONDS:
                self.audio_dropped_frames += 1
                return None
            if kind == "phone":
                if self.source_kind == "computer" or self._phone_stop.is_set():
                    return None
                if not self._activate_locked(frame.source_id, frame.device_id, kind,
                                             expected_generation=frame.generation):
                    self.audio_dropped_frames += 1
                    return None
            elif self.source_id != frame.source_id or not self.connected:
                return None
            if reset:
                self._reset_stream_recognizer()
                self.wake_until = 0.0
                self.audio_stream_resets += 1
            return self._ingest_pcm_locked(frame.source_id, frame.pcm16, queued_at=frame.queued_at,
                                           generation=frame.generation)

    def accept_phone_command(self, source_id: str, device_id: str, command_id: str, phrase: str = "",
                             *, expected_generation: int | None = None) -> tuple[dict, dict | None]:
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
            if not self._activate_locked(str(source_id), str(device_id), "phone",
                                         expected_generation=expected_generation):
                return self.status(), {"matched": False, "reason": "voice_source_generation_changed"}
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

    def _ingest_pcm_locked(self, source_id: str, pcm16: bytes, *, queued_at: float | None = None,
                           generation: int | None = None) -> tuple[dict | None, dict | None]:
        if not pcm16 or len(pcm16) % 2:
            raise ValueError("音频必须是 16kHz 单声道 PCM16 双数字节")
        if len(pcm16) > MAX_AUDIO_FRAME_BYTES:
            raise ValueError("单个音频帧过大")
        if self.recognizer is None or not self.audio_ready:
            raise RuntimeError(self.last_error or "Windows 系统语音识别未就绪")
        self.audio_bytes += len(pcm16)
        self.last_audio_at = time.monotonic()
        self.last_rms = self._pcm_rms(pcm16)
        self.peak_rms = max(self.peak_rms, self.last_rms)
        event = self.recognizer.accept(pcm16)
        if ((generation is not None and generation != self._audio_generation) or
                (queued_at is not None and time.monotonic() - queued_at > MAX_AUDIO_AGE_SECONDS)):
            self.audio_dropped_frames += 1
            self.audio_stream_resets += 1
            self._reset_stream_recognizer()
            self.wake_until = 0.0
            return None, None
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
        # Game slots keep their shipped keyboard catalog kind. Resolve an
        # enabled system override before handing the action to the injected
        # dispatcher, so it captures cancellation at recognition time too.
        # Legacy callers without a dispatcher still resolve at execute_action.
        voice_bindings = self._profile_bindings.get("voice", {})
        binding = voice_bindings.get(cid) if isinstance(voice_bindings, dict) else None
        bound_action = binding.get("action") if isinstance(binding, dict) else None
        if (self.on_system_command is not None and kind != "system" and
                isinstance(bound_action, dict) and bound_action.get("type") == "system" and
                not binding.get("disabled")):
            kind, target = "system", str(bound_action.get("target", ""))
        if not cid or not target:
            return {"matched": False, "reason": "invalid_command"}
        if cid == "system.emergency_stop":
            self.last_command = phrase or DEFAULT_EMERGENCY_STOP
            self.commands_heard += 1
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
        # phrase 只给界面看：记触发时写"刚才说的是哪一句"。
        action = {"type": kind, "target": target, "command_id": cid, "phrase": phrase}
        action["source"] = f"voice:{source_id}" if source_id else "voice"
        if kind == "system":
            action["voice_source_id"] = source_id
            action["voice_source_kind"] = self.source_kind
            action["voice_source_generation"] = self._audio_generation
        self.last_command = phrase or cid
        self.commands_heard += 1
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

        if action["type"] == "system":
            self._submit_system_action(action, run)
        else:
            # Output uses timer-backed pulses, so submitting under the voice lock
            # preserves command order and prevents a late hold after disconnect.
            with self._lock:
                if source_id is not None and not self.source_is_active(source_id):
                    return {"matched": False, "reason": "voice_source_inactive"}
                run()
        return {"matched": True, "command": self.last_command, "command_id": cid, "pending": True}

    def _match_and_execute(self, recognized: str, *, source_id: str | None = None, enforce_wake: bool = False) -> dict | None:
        got = compact_text(recognized)
        wake = compact_text(self.system_wake_word)
        if got in {compact_text(item) for item in self.spoken_emergency_phrases()}:
            self.last_command = self._with_wake_word(DEFAULT_EMERGENCY_STOP)
            self.commands_heard += 1
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
        # 本游戏口令是独立的一栏，直接说配置的短语即可。先查注册表再做唤醒词门禁，
        # 这样同名的通用口令和系统口令仍不会裸奔。
        registry_command = self._phrase_index.get(got)
        shared_match = next((mapping for mapping in self.mappings
                             if got in {compact_text(self.wake_word + phrase)
                                        for phrase in [mapping["phrase"], *mapping.get("synonyms", [])]}), None)
        game_command_without_wake = (registry_command is not None and
                                      self._is_game_profile_command(registry_command.get("id")))
        if game_command_without_wake or shared_match is not None:
            # 一句本游戏口令本身就是完整指令，不把之前单独说过的唤醒词窗口
            # 留给下一句通用/系统口令。
            self.wake_until = 0.0
        command = got
        if enforce_wake and wake and not game_command_without_wake and shared_match is None:
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
        # Resolve the canonical catalog first so the current Game Profile is
        # honored for both computer audio and phone voice_text.
        registry_command = self._phrase_index.get(got)
        if registry_command is None and wake:
            registry_command = self._phrase_index.get(compact_text(f"{self.system_wake_word}{command}"))
        if registry_command is None:
            candidate = self._phrase_index.get(command)
            if candidate is not None and self._is_game_profile_command(candidate.get("id")):
                registry_command = candidate
        if registry_command is not None:
            return self._execute_command_action(registry_command, source_id=source_id)

        match = shared_match
        if match is None and (not enforce_wake or not self.wake_word or got.startswith(compact_text(self.wake_word))):
            for mapping in self.mappings:
                phrases = [mapping["phrase"], *mapping.get("synonyms", [])]
                if command in {compact_text(item) for item in phrases}:
                    match = mapping
                    break
        if match is None:
            return {"matched": False, "reason": "command_not_in_mapping"}
        action = {**{key: value for key, value in match.items() if key not in {"phrase", "synonyms"}},
                  "phrase": f"{self.wake_word}{match['phrase']}"}
        action["source"] = f"voice:{source_id}" if source_id else "voice"
        if match["type"] == "system":
            action["voice_source_id"] = source_id
            action["voice_source_kind"] = self.source_kind
            action["voice_source_generation"] = self._audio_generation
        self.last_command = match["phrase"]
        self.commands_heard += 1
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

        if action["type"] == "system":
            self._submit_system_action(action, run)
        else:
            # Output uses timer-backed pulses, so submitting under the voice lock
            # preserves command order and prevents a late hold after disconnect.
            with self._lock:
                if source_id is not None and not self.source_is_active(source_id):
                    return {"matched": False, "reason": "voice_source_inactive"}
                run()
        return {"matched": True, "command": match["phrase"], "pending": True}

    def _submit_system_action(self, action: dict, fallback: Callable[[], None]) -> None:
        if self.on_system_command is None:
            threading.Thread(target=fallback, name="voice-command", daemon=True).start()
            return
        try:
            result = self.on_system_command(copy.deepcopy(action)) or {}
            queued = bool(result.get("queued") or result.get("pending"))
            self.last_executed = None if queued else bool(result.get("executed", False))
            self.last_error = None if queued or self.last_executed else str(result.get("reason", "输出未开启"))
        except Exception as exc:
            self.last_executed = False
            self.last_error = str(exc)

    def _mic_callback(self, indata, frames, time_info, status, *, frame_queue=None,
                      generation=None, gap_event=None) -> None:
        # PortAudio must never acquire the recognition/configuration lock. It
        # hands off immutable bytes and leaves reporting to the consumer.
        data = bytes(indata)
        target = self._mic_queue if frame_queue is None else frame_queue
        gap = self._mic_gap if gap_event is None else gap_event
        if status:
            self._callback_error = f"电脑麦克风状态：{status}"
            gap.set()
        if not data or target is None:
            return
        self._mic_sequence += 1
        sampled_at = time.monotonic()
        try:
            # PortAudio timestamps use their own clock; subtracting their two
            # values gives the hardware-buffer age in our monotonic clock.
            age = float(time_info.currentTime) - float(time_info.inputBufferAdcTime)
            if math.isfinite(age) and age >= 0.0:
                sampled_at -= age
        except (AttributeError, TypeError, ValueError):
            pass
        frame = _AudioFrame("computer_microphone", "computer", data, sampled_at,
                            self._audio_generation if generation is None else generation, self._mic_sequence)
        try:
            target.put_nowait(frame)
        except queue.Full:
            self.audio_dropped_frames += 1
            self._callback_error = "电脑麦克风队列已满，已丢弃音频并重置识别"
            gap.set()

    def start_local_microphone(self, device: int | None = None) -> dict:
        self.stop_local_microphone()
        with self._lock:
            if self.recognizer is None:
                self._rebuild_recognizer()
            if self.recognizer is None:
                with self._audio_submit_lock:
                    self.connected = False
                return self.status()
        try:
            import sounddevice as sd
        except ImportError:
            with self._lock:
                self.last_error = "电脑语音需要 sounddevice；当前 Python 环境未安装"
                with self._audio_submit_lock:
                    self.connected = False
                return self.status()
        try:
            info = sd.query_devices(device, "input")
            sd.check_input_settings(device=device, samplerate=self.sample_rate, channels=1, dtype="int16")
            device_name = str(info.get("name", "")) if isinstance(info, dict) else ""
        except Exception as exc:
            with self._lock:
                self.audio_device = device
                self.audio_device_name = None
                self.last_error = f"电脑麦克风设备不可用：{exc}"
                with self._audio_submit_lock:
                    self.connected = False
            return self.status()
        source_id = "computer_microphone"
        frame_queue: queue.Queue[_AudioFrame] = queue.Queue(maxsize=AUDIO_QUEUE_CAPACITY)
        self._mic_queue = frame_queue
        stop_event = threading.Event()
        gap_event = threading.Event()
        self._mic_stop = stop_event
        self._mic_gap = gap_event
        with self._lock:
            self._activate_locked(source_id, "computer", "computer")
            self.audio_device = device
            self.audio_device_name = device_name or ("系统默认设备" if device is None else str(device))
            self.audio_ready = True
            self.last_error = None
        self._mic_thread = threading.Thread(target=self._mic_loop, args=(source_id, frame_queue, stop_event, gap_event),
                                            name="voice-microphone", daemon=True)
        self._mic_thread.start()
        try:
            self._mic_stream = sd.RawInputStream(
                samplerate=self.sample_rate, channels=1, dtype="int16", blocksize=1600,
                device=device,
                callback=lambda data, frames, info, status: None if stop_event.is_set() else self._mic_callback(
                    data, frames, info, status, frame_queue=frame_queue, gap_event=gap_event),
            )
            self._mic_stream.start()
        except Exception as exc:
            self._mic_stop.set()
            with self._lock:
                self.last_error = f"电脑麦克风启动失败：{exc}"
            self.stop_local_microphone()
        return self.status()

    def _mic_loop(self, source_id: str, frame_queue=None, stop_event=None, gap_event=None) -> None:
        frame_queue = self._mic_queue if frame_queue is None else frame_queue
        stop_event = self._mic_stop if stop_event is None else stop_event
        gap_event = self._mic_gap if gap_event is None else gap_event
        previous_sequence: int | None = None
        gap = False
        while not stop_event.is_set():
            try:
                frame = frame_queue.get(timeout=0.20) if frame_queue is not None else None
            except queue.Empty:
                with self._lock:
                    if self.source_id == source_id and self.last_audio_at and time.monotonic() - self.last_audio_at > VOICE_TIMEOUT_SECONDS:
                        self._release_locked(source_id)
                continue
            if frame is None:
                continue
            try:
                if stop_event.is_set():
                    continue
                gap = gap or gap_event.is_set()
                gap_event.clear()
                if previous_sequence is not None and frame.sequence != previous_sequence + 1:
                    gap = True
                with self._lock:
                    if self._callback_error:
                        self.last_error, self._callback_error = self._callback_error, None
                outcome = self._consume_audio(frame, "computer", reset=gap)
                gap = outcome is None
                if outcome is not None:
                    previous_sequence = frame.sequence
            except Exception as exc:
                gap = True
                with self._lock:
                    self._release_locked(source_id)
                    self.last_error = str(exc)
            finally:
                frame_queue.task_done()

    def stop_local_microphone(self) -> dict:
        with self._audio_submit_lock:
            self._audio_generation += 1
        self._mic_stop.set()
        self._mic_queue = None
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
                self.audio_ready = False
                with self._audio_submit_lock:
                    self.connected = False
                    self.source_id = None
                    self.device_id = None
                    self.source_kind = None
        return self.status()

    def phone_source_generation(self, source_id: str) -> int:
        """Capture admission before releasing the bridge's source-owner lock."""
        with self._audio_submit_lock:
            return self._audio_generation

    @contextmanager
    def command_source_guard(self, source_id: str | None, expected_generation: int | None = None):
        """Hold only the short source lock through a deferred command's commit.

        Callers acquire their final mutation lock first. This never acquires the
        recognizer lock or calls output, and invalidation waits until commit ends.
        """
        with self._audio_submit_lock:
            active = bool(source_id and self.connected and self.source_id == str(source_id) and
                          str(source_id) not in self._invalidated_phone_sources and
                          (expected_generation is None or expected_generation == self._audio_generation))
            yield active

    def invalidate_phone_source(self, source_id: str) -> int:
        """Invalidate in-flight recognition immediately; cleanup can take its lock later."""
        source_id = str(source_id)
        with self._audio_submit_lock:
            if self.source_id not in {None, source_id}:
                return self._audio_generation
            self._audio_generation += 1
            generation = self._audio_generation
            self._invalidated_phone_sources[source_id] = generation
            if self.source_id == source_id:
                self.connected = False
            return generation

    def disconnect(self, source_id: str | None = None, *, expected_generation: int | None = None) -> dict:
        with self._lock:
            with self._audio_submit_lock:
                if expected_generation is not None and self._audio_generation != expected_generation:
                    return self.status()
                if source_id is not None and self.source_id not in {None, str(source_id)}:
                    return self.status()
                # Fast invalidation already advanced the token. Do not reject
                # the next owner's correctly admitted frame during cleanup.
                if expected_generation is None:
                    self._audio_generation += 1
                previous_source = self.source_id
                self.connected = False
                self.source_id = None
                self.device_id = None
                self.source_kind = None
            self._release_locked(previous_source)
            self.audio_ready = False
            self.last_partial = ""
            return self.status()

    def source_is_active(self, source_id: str | None, *, expected_generation: int | None = None) -> bool:
        """Check the exact voice source before a deferred system action runs."""
        with self.command_source_guard(source_id, expected_generation) as active:
            return active

    def status(self) -> dict:
        now = time.monotonic()
        alive = bool(self.connected and self.last_audio_at and now - self.last_audio_at < VOICE_TIMEOUT_SECONDS)
        display_model_path = (
            str(self.model_path) if self.model_path else None
        )
        result = {
            "available": self.recognizer is not None,
            "model_ready": bool(self.recognizer is not None),
            "model_path": display_model_path,
            "recognizer_mode": self.recognizer_mode,
            "supported_count": self.supported_count,
            "unsupported": list(self.unsupported),
            "unheard": [{"phrase": phrase, "chars": list(chars)} for phrase, chars in self.unheard.items()],
            "mappings": list(self.mappings),
            # 换了游戏、装了别人的配置，都可能带进来一句和通用口令同名的。存的时候
            # 拦得住，这两条路拦不住，只能照实告诉界面。
            "phrase_conflicts": list(self._configuration_conflicts),
            "wake_word": self.wake_word,
            "wake_system_commands": self.wake_system_commands,
            "system_wake_word": self.system_wake_word,
            "builtin_emergency_phrase": self._with_wake_word(DEFAULT_EMERGENCY_STOP),
            "custom_emergency_stop_phrases": [item for item in self.emergency_stop_phrases
                                              if item != DEFAULT_EMERGENCY_STOP],
            # 界面上要显示的是"要怎么说"，不是盘上存的那个写法。
            "emergency_stop_phrases": self.spoken_emergency_phrases(),
            "connected": self.connected,
            "source": self.source_id,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "device_id": self.device_id,
            "audio_device": self.audio_device,
            "audio_device_name": self.audio_device_name,
            "audio_ready": self.audio_ready,
            "partial": self.last_partial,
            "final": self.last_final,
            "last_partial": self.last_partial,
            "last_final": self.last_final,
            "last_command": self.last_command,
            "commands_heard": self.commands_heard,
            "last_action": self.last_action,
            "last_executed": self.last_executed,
            "last_error": self.last_error,
            "audio_alive": alive,
            "stream_alive": alive,
            "audio_queue_depth": self._phone_queue.qsize() if self.source_kind == "phone" else
                                 self._mic_queue.qsize() if self._mic_queue is not None else 0,
            "audio_dropped_frames": self.audio_dropped_frames,
            "audio_stream_resets": self.audio_stream_resets,
        }
        if self.source_kind in {"computer", "phone"}:
            result.update({
                "bytes_received": self.audio_bytes,
                "rms": round(self.last_rms, 1),
                "peak_rms": round(self.peak_rms, 1),
                "audio_seconds": round(self.audio_bytes / (self.sample_rate * 2), 2),
            })
        return result

    def close(self) -> None:
        self._phone_stop.set()
        with self._audio_submit_lock:
            self._audio_generation += 1
        thread = self._phone_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self.stop_local_microphone()
        recognizer = self.recognizer
        self.recognizer = None
        if recognizer is not None:
            try:
                recognizer.close()
            except Exception:
                pass
        self.disconnect()
