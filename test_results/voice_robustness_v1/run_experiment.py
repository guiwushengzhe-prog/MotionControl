"""Offline, output-isolated Chinese voice robustness comparison.

This script deliberately reads production configuration/models but never starts
server.py, VoiceService, OutputManager, keyboard injection, or game output.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import gc
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import time
import wave
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SAMPLE_RATE = 16_000
CHUNK_SAMPLES = 3_200  # 200 ms, close to the real streaming path.
NOISE_TYPES = ("white", "pink", "fan")
SNRS_DB = (20, 10, 5, 0)
CLEAN_LEVELS_DBFS = (-6, -18, -30)
PUNCT = re.compile(r"[\s\u3000，。！？、,.!?;；:：\"'“”‘’（）()【】\[\]<>《》]+")


def compact(value: Any) -> str:
    return PUNCT.sub("", str(value or "").strip().lower())


def json_load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def stable_seed(*parts: Any) -> int:
    raw = "|".join(str(part) for part in parts).encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:8], 16)


def rms(value: Any) -> float:
    import numpy as np

    x = np.asarray(value, dtype=np.float32)
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x), dtype=np.float64)))


def rms_db(value: Any) -> float:
    return 20.0 * math.log10(max(rms(value), 1e-12))


def scale_to_db(value: Any, target_dbfs: float) -> Any:
    import numpy as np

    x = np.asarray(value, dtype=np.float32).copy()
    current = rms(x)
    if current <= 1e-12:
        return x
    factor = (10.0 ** (target_dbfs / 20.0)) / current
    return np.clip(x * factor, -1.0, 1.0).astype(np.float32)


def read_wav(path: Path) -> Any:
    import numpy as np

    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        source_rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if sample_width == 2:
        x = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 4:
        x = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported WAV sample width {sample_width}: {path}")
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    if source_rate != SAMPLE_RATE:
        if len(x) == 0:
            return x.astype(np.float32)
        target_len = max(1, round(len(x) * SAMPLE_RATE / source_rate))
        old_t = np.arange(len(x), dtype=np.float64) / source_rate
        new_t = np.arange(target_len, dtype=np.float64) / SAMPLE_RATE
        x = np.interp(new_t, old_t, x).astype(np.float32)
    return np.clip(x, -1.0, 1.0).astype(np.float32)


def write_wav(path: Path, value: Any, sample_rate: int = SAMPLE_RATE) -> None:
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(np.asarray(value, dtype=np.float32), -1.0, 1.0)
    pcm16 = np.rint(pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm16.tobytes())


def trim_silence(value: Any, sample_rate: int = SAMPLE_RATE) -> Any:
    import numpy as np

    x = np.asarray(value, dtype=np.float32)
    if x.size == 0:
        return x.copy()
    threshold = max(0.008, float(np.max(np.abs(x))) * 0.02)
    active = np.flatnonzero(np.abs(x) > threshold)
    if active.size == 0:
        return x.copy()
    pad = round(sample_rate * 0.08)
    start = max(0, int(active[0]) - pad)
    end = min(len(x), int(active[-1]) + pad + 1)
    return x[start:end].copy()


def high_pass_80hz(value: Any, sample_rate: int = SAMPLE_RATE) -> Any:
    import numpy as np

    x = np.asarray(value, dtype=np.float32)
    if x.size == 0:
        return x.copy()
    alpha = math.exp(-2.0 * math.pi * 80.0 / sample_rate)
    y = np.empty_like(x)
    previous_x = 0.0
    previous_y = 0.0
    for index, current in enumerate(x):
        current_y = alpha * (previous_y + float(current) - previous_x)
        y[index] = current_y
        previous_x = float(current)
        previous_y = current_y
    return y


def light_preprocess(value: Any) -> Any:
    """Small deterministic front-end: trim, high-pass, normalize to -18 dBFS."""

    x = trim_silence(value)
    x = high_pass_80hz(x)
    return scale_to_db(x, -18.0)


def make_noise(kind: str, length: int, seed: int) -> Any:
    import numpy as np

    rng = np.random.default_rng(seed)
    white = rng.normal(0.0, 1.0, length).astype(np.float32)
    if kind == "white":
        result = white
    elif kind == "pink":
        frequencies = np.fft.rfftfreq(length, d=1.0 / SAMPLE_RATE)
        spectrum = np.fft.rfft(white)
        shaping = 1.0 / np.sqrt(np.maximum(frequencies, 1.0 / max(length, 1)))
        result = np.fft.irfft(spectrum * shaping, n=length).real.astype(np.float32)
    elif kind == "fan":
        result = np.empty_like(white)
        state = 0.0
        for index, current in enumerate(white):
            state = 0.985 * state + 0.015 * float(current)
            result[index] = state
        t = np.arange(length, dtype=np.float32) / SAMPLE_RATE
        result += 0.12 * np.sin(2.0 * math.pi * 120.0 * t)
        result += 0.05 * np.sin(2.0 * math.pi * 240.0 * t + 0.4)
    else:
        raise ValueError(f"unknown noise profile: {kind}")
    return scale_to_db(result, -18.0)


def add_noise(speech: Any, kind: str, snr_db: float, seed: int) -> Any:
    import numpy as np

    x = np.asarray(speech, dtype=np.float32)
    active = trim_silence(x)
    speech_rms = max(rms(active), 1e-8)
    noise = make_noise(kind, len(x), seed)
    desired_noise_rms = speech_rms / (10.0 ** (snr_db / 20.0))
    noise = scale_to_db(noise, 20.0 * math.log10(desired_noise_rms))
    return np.clip(x + noise, -1.0, 1.0).astype(np.float32)


def model_tree_info(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    total = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append({"path": relative, "bytes": size, "sha256": file_hash})
        digest.update(relative.encode("utf-8"))
        digest.update(str(size).encode("ascii"))
        digest.update(file_hash.encode("ascii"))
        total += size
    return {"path": str(root), "bytes": total, "sha256": digest.hexdigest(), "files": files}


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def rss_bytes() -> int | None:
    if os.name != "nt":
        return None
    try:
        psapi = ctypes.WinDLL("Psapi.dll")
        kernel32 = ctypes.WinDLL("Kernel32.dll")
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.restype = ctypes.c_void_p
        get_memory = psapi.GetProcessMemoryInfo
        get_memory.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), ctypes.c_ulong]
        get_memory.restype = ctypes.c_int
        ok = get_memory(get_current_process(), ctypes.byref(counters), counters.cb)
        return int(counters.WorkingSetSize) if ok else None
    except Exception:
        return None


def git_snapshot(repo: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), *args],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            return result.stdout.strip()
        except Exception as exc:  # pragma: no cover - diagnostics only
            return f"<unavailable: {exc}>"

    status = run("status", "--porcelain=v1", "--", ":(exclude)test_results/voice_robustness_v1")
    return {
        "head": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "status_outside_this_artifact": status,
        "clean_outside_this_artifact": status == "",
    }


def package_info(names: Iterable[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in names:
        try:
            distribution = importlib.metadata.distribution(name)
            root = Path(distribution.locate_file(""))
            result[name] = {
                "version": distribution.version,
                "metadata_license": distribution.metadata.get("License", ""),
                "files_count": len(distribution.files or []),
                "location": str(root),
            }
        except importlib.metadata.PackageNotFoundError:
            result[name] = {"version": None, "available": False}
    return result


def map_target_to_intent(target: str) -> str | None:
    return {
        "HEAD_CALIBRATION_START": "head.calibrate",
        "ENTER": "ui.confirm",
        "ESC": "ui.back",
        "UP": "nav.up",
        "DOWN": "nav.down",
        "LEFT": "nav.left",
        "RIGHT": "nav.right",
        "W": "game.accelerate",
        "S": "game.brake",
        "J": "game.attack",
        "SPACE": "game.jump",
        "M": "ui.map",
        "SCENE.CAPTURE_REFERENCE": "scene.capture",
        "SCENE.REMATCH": "scene.rematch",
        "OUTPUT.START": "output.start",
        "OUTPUT.STOP": "output.stop",
        "HEAD.CENTER": "head.center",
    }.get(target)


class CommandParser:
    def __init__(self, action_map: dict[str, Any], legacy_config: dict[str, Any]):
        self.action_map = {compact(key): value for key, value in action_map.items()}
        self.legacy = legacy_config
        self.wake = compact(legacy_config.get("wake_word", "体感"))
        self.emergency = {compact(item) for item in legacy_config.get("emergency_stop_phrases", [])}

    def from_action(self, action: dict[str, Any] | None) -> dict[str, Any] | None:
        if not action:
            return None
        intent = str(action.get("id") or "") or None
        phrase = str(action.get("phrase") or "")
        if intent is None:
            return None
        return {"intent": intent, "phrase": phrase, "text": compact(phrase), "source": "kws"}

    def from_text(self, text: str | None, source: str) -> dict[str, Any] | None:
        normalized = compact(text)
        if not normalized:
            return None
        if normalized in self.action_map:
            action = self.action_map[normalized]
            return {
                "intent": action.get("id"),
                "phrase": normalized,
                "text": normalized,
                "source": source,
            }
        if normalized in self.emergency:
            return {
                "intent": "system.emergency_stop",
                "phrase": normalized,
                "text": normalized,
                "source": source,
            }
        if not normalized.startswith(self.wake):
            return None
        body = normalized[len(self.wake) :]
        for mapping in self.legacy.get("mappings", []):
            candidates = [mapping.get("phrase", ""), *mapping.get("synonyms", [])]
            if body in {compact(candidate) for candidate in candidates}:
                intent = map_target_to_intent(str(mapping.get("target", "")))
                if intent:
                    return {
                        "intent": intent,
                        "phrase": normalized,
                        "text": normalized,
                        "source": source,
                    }
        return None


def base_clip(raw: Any) -> Any:
    import numpy as np

    active = trim_silence(raw)
    padding_before = np.zeros(round(SAMPLE_RATE * 0.12), dtype=np.float32)
    padding_after = np.zeros(round(SAMPLE_RATE * 0.18), dtype=np.float32)
    return np.concatenate([padding_before, active, padding_after]).astype(np.float32)


def build_case_audio(item: dict[str, Any], raw: Any, condition: dict[str, Any], root: Path) -> Any:
    import numpy as np

    if item.get("noise_only"):
        return make_noise(item["noise_type"], round(SAMPLE_RATE * 1.8), stable_seed("noise-only", item["noise_type"]))
    base = base_clip(raw)
    if condition["group"] == "clean":
        return scale_to_db(base, float(condition["level_dbfs"]))
    normal = scale_to_db(base, -18.0)
    return add_noise(
        normal,
        condition["noise_type"],
        float(condition["snr_db"]),
        stable_seed(item.get("id"), item.get("voice_id"), condition["noise_type"], condition["snr_db"]),
    )


def make_conditions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conditions: list[dict[str, Any]] = []
    for level in CLEAN_LEVELS_DBFS:
        conditions.append({"id": f"clean_{level:+d}dBFS", "group": "clean", "level_dbfs": level})
    for item in items:
        if not item.get("stress"):
            continue
        for noise_type in NOISE_TYPES:
            for snr_db in SNRS_DB:
                conditions.append(
                    {
                        "id": f"noise_{noise_type}_snr{snr_db}dB",
                        "group": "noise",
                        "noise_type": noise_type,
                        "snr_db": snr_db,
                    }
                )
    return conditions


def build_cases(root: Path, tts_manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    speech_items = list(tts_manifest.get("items", []))
    raw_cache: dict[str, Any] = {}
    for item in speech_items:
        path = root / item["file"]
        raw_cache[item["file"]] = read_wav(path)

    cases: list[dict[str, Any]] = []
    # Keep the pressure corpus bounded: every clip gets the three clean levels,
    # while only items explicitly marked stress receive noise/SNR variants.
    # The earlier draft applied the global noise list to every item, multiplying
    # the corpus into ~20k cases and wasting memory without adding evidence.
    clean_conditions = [
        {"id": f"clean_{level:+d}dBFS", "group": "clean", "level_dbfs": level}
        for level in CLEAN_LEVELS_DBFS
    ]
    noise_conditions = [
        {
            "id": f"noise_{noise_type}_snr{snr_db}dB",
            "group": "noise",
            "noise_type": noise_type,
            "snr_db": snr_db,
        }
        for noise_type in NOISE_TYPES
        for snr_db in SNRS_DB
    ]
    conditions = clean_conditions + noise_conditions
    for item in speech_items:
        raw = raw_cache[item["file"]]
        item_conditions = clean_conditions + (noise_conditions if item.get("stress") else [])
        for condition in item_conditions:
            case = dict(item)
            case["case_id"] = f"{item['id']}__{item['voice_id']}__{condition['id']}"
            case["condition"] = condition["id"]
            case["condition_group"] = condition["group"]
            case["snr_db"] = condition.get("snr_db")
            case["noise_type"] = condition.get("noise_type")
            case["audio"] = build_case_audio(item, raw, condition, root)
            cases.append(case)

    for noise_type in NOISE_TYPES:
        item = {
            "id": f"noise_only_{noise_type}",
            "case_id": f"noise_only_{noise_type}",
            "voice_id": "synthetic",
            "text": "",
            "expected_intent": None,
            "kind": "negative",
            "stress": True,
            "emergency": False,
            "noise_only": True,
            "noise_type": noise_type,
            "condition": f"noise_only_{noise_type}",
            "condition_group": "noise_only",
            "snr_db": None,
            "audio": make_noise(noise_type, round(SAMPLE_RATE * 1.8), stable_seed("noise-only", noise_type)),
        }
        cases.append(item)
    return cases, conditions


def pcm_bytes(audio: Any) -> bytes:
    import numpy as np

    return np.rint(np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def chunks(data: bytes, sample_bytes: int = 2) -> Iterable[bytes]:
    size = CHUNK_SAMPLES * sample_bytes
    for start in range(0, len(data), size):
        yield data[start : start + size]


class CandidateBase:
    candidate_id = ""
    label = ""

    def __init__(self, parser: CommandParser):
        self.parser = parser
        self.load_ms = 0.0
        self.rss_before = rss_bytes()
        self.rss_after = None

    def resource_record(self, model_info: dict[str, Any]) -> dict[str, Any]:
        delta = None
        if self.rss_before is not None and self.rss_after is not None:
            delta = self.rss_after - self.rss_before
        return {
            "candidate": self.candidate_id,
            "label": self.label,
            "model": model_info,
            "load_ms": self.load_ms,
            "rss_before_bytes": self.rss_before,
            "rss_after_bytes": self.rss_after,
            "rss_delta_bytes": delta,
        }

    def close(self) -> None:
        pass


class SherpaCurrentCandidate(CandidateBase):
    candidate_id = "sherpa_phrase_kws_current"
    label = "当前 Sherpa 全短语 KWS"

    def __init__(self, parser: CommandParser, repo: Path, model_dir: Path):
        super().__init__(parser)
        started = time.perf_counter()
        from sherpa_phrase_kws import SherpaPhraseKws

        self.engine = SherpaPhraseKws(
            model_dir=model_dir,
            keywords_file=repo / "config" / "generated_voice" / "keywords.txt",
            action_map_file=repo / "config" / "generated_voice" / "voice_action_map.json",
            sample_rate=SAMPLE_RATE,
        )
        self.load_ms = (time.perf_counter() - started) * 1000.0
        self.rss_after = rss_bytes()

    def reset_stream(self) -> None:
        try:
            self.engine.spotter.reset_stream(self.engine.stream)
            self.engine.last_trigger_at = 0.0
            self.engine.last_command_id = None
        except Exception:
            self.engine.reset()

    def recognize(self, audio: Any) -> dict[str, Any] | None:
        data = pcm_bytes(audio)
        self.reset_stream()
        found = None
        for chunk in chunks(data):
            found = self.engine.accept_pcm16(chunk)
            if found:
                break
        self.reset_stream()
        return self.parser.from_action(found)

    def close(self) -> None:
        try:
            self.engine.close()
        except Exception:
            pass


class VoskCandidate(CandidateBase):
    def __init__(self, parser: CommandParser, model_dir: Path, constrained: bool, grammar: list[str]):
        self.candidate_id = "vosk_constrained" if constrained else "vosk_open"
        self.label = "Vosk 受限词表" if constrained else "Vosk 开放识别"
        super().__init__(parser)
        started = time.perf_counter()
        import vosk

        vosk.SetLogLevel(-1)
        self.vosk = vosk
        self.model = vosk.Model(str(model_dir))
        self.constrained = constrained
        self.grammar_json = json.dumps(grammar, ensure_ascii=False)
        self.load_ms = (time.perf_counter() - started) * 1000.0
        self.rss_after = rss_bytes()

    def recognize(self, audio: Any) -> dict[str, Any] | None:
        recognizer = (
            self.vosk.KaldiRecognizer(self.model, SAMPLE_RATE, self.grammar_json)
            if self.constrained
            else self.vosk.KaldiRecognizer(self.model, SAMPLE_RATE)
        )
        recognizer.SetWords(False)
        data = pcm_bytes(audio)
        candidate = None
        for chunk in chunks(data):
            accepted = recognizer.AcceptWaveform(chunk)
            if accepted:
                payload = json.loads(recognizer.Result())
                candidate = self.parser.from_text(payload.get("text"), self.candidate_id)
                if candidate:
                    break
        if candidate:
            return candidate
        payload = json.loads(recognizer.FinalResult())
        return self.parser.from_text(payload.get("text"), self.candidate_id)


def classify_prediction(
    case: dict[str, Any], prediction: dict[str, Any] | None, expected_text: str
) -> dict[str, Any]:
    expected_intent = case.get("expected_intent")
    is_positive = case.get("kind") == "positive"
    predicted_intent = prediction.get("intent") if prediction else None
    predicted_text = prediction.get("text") if prediction else None
    return {
        "expected_intent": expected_intent,
        "predicted_intent": predicted_intent,
        "predicted_text": predicted_text,
        "triggered": bool(prediction),
        "intent_correct": predicted_intent == expected_intent if is_positive else prediction is None,
        "sentence_exact": bool(is_positive and predicted_text == compact(expected_text)),
        "false_trigger": bool(not is_positive and prediction),
        "is_emergency": bool(case.get("emergency")),
        "emergency_hit": bool(case.get("emergency") and predicted_intent == "system.emergency_stop"),
    }


def run_candidate(
    candidate: CandidateBase,
    candidate_id: str,
    cases: list[dict[str, Any]],
    preprocessing: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    for case in cases:
        audio = case["audio"] if preprocessing == "raw" else light_preprocess(case["audio"])
        started_wall = time.perf_counter()
        started_cpu = time.process_time()
        error = None
        prediction = None
        try:
            prediction = candidate.recognize(audio)
        except Exception as exc:  # retain per-case evidence without hiding candidate failure
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = (time.perf_counter() - started_wall) * 1000.0
        cpu_ms = (time.process_time() - started_cpu) * 1000.0
        scores = classify_prediction(case, prediction, case.get("text", ""))
        rows.append(
            {
                "case_id": case["case_id"],
                "item_id": case["id"],
                "voice_id": case["voice_id"],
                "condition": case["condition"],
                "condition_group": case["condition_group"],
                "noise_type": case.get("noise_type"),
                "snr_db": case.get("snr_db"),
                "kind": case["kind"],
                "text": case.get("text", ""),
                "candidate": candidate_id,
                "preprocessing": preprocessing,
                "latency_ms": latency_ms,
                "cpu_ms": cpu_ms,
                "error": error,
                **scores,
            }
        )
    elapsed = {
        "wall_ms": (time.perf_counter() - wall_start) * 1000.0,
        "cpu_ms": (time.process_time() - cpu_start) * 1000.0,
        "cases": len(cases),
    }
    return rows, elapsed


def aggregate(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[tuple(row.get(key) for key in keys)].append(row)
    result: list[dict[str, Any]] = []
    for bucket_key, bucket in sorted(buckets.items(), key=lambda pair: tuple(str(v) for v in pair[0])):
        positives = [row for row in bucket if row["kind"] == "positive"]
        negatives = [row for row in bucket if row["kind"] != "positive"]
        emergencies = [row for row in bucket if row["is_emergency"]]
        latencies = [float(row["latency_ms"]) for row in bucket]
        cpus = [float(row["cpu_ms"]) for row in bucket]
        positive_correct = sum(bool(row["predicted_intent"] == row["expected_intent"]) for row in positives)
        positive_exact = sum(bool(row["sentence_exact"]) for row in positives)
        false_triggers = sum(bool(row["false_trigger"]) for row in negatives)
        rejected = sum(not bool(row["triggered"]) for row in negatives)
        emergency_hits = sum(bool(row["emergency_hit"]) for row in emergencies)
        total = len(bucket)
        accepted_or_rejected = positive_correct + rejected
        data: dict[str, Any] = {key: value for key, value in zip(keys, bucket_key)}
        data.update(
            {
                "total": total,
                "positive_total": len(positives),
                "positive_intent_correct": positive_correct,
                "positive_intent_accuracy": positive_correct / len(positives) if positives else None,
                "positive_sentence_exact": positive_exact,
                "full_sentence_accuracy": positive_exact / len(positives) if positives else None,
                "negative_total": len(negatives),
                "negative_rejected": rejected,
                "rejection_rate": rejected / len(negatives) if negatives else None,
                "false_trigger_count": false_triggers,
                "false_trigger_rate": false_triggers / len(negatives) if negatives else None,
                "emergency_total": len(emergencies),
                "emergency_hits": emergency_hits,
                "emergency_recall": emergency_hits / len(emergencies) if emergencies else None,
                "accept_reject_accuracy": accepted_or_rejected / total if total else None,
                "median_latency_ms": statistics.median(latencies) if latencies else None,
                "p95_latency_ms": percentile(latencies, 95.0) if latencies else None,
                "mean_cpu_ms": statistics.mean(cpus) if cpus else None,
            }
        )
        result.append(data)
    return result


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def weighted_quality(row: dict[str, Any]) -> float:
    """Transparent screening score; false triggers and emergency recall lead."""

    false_rate = float(row.get("false_trigger_rate") or 0.0)
    emergency = float(row.get("emergency_recall") or 0.0)
    intent = float(row.get("positive_intent_accuracy") or 0.0)
    latency = float(row.get("p95_latency_ms") or 9999.0)
    latency_score = max(0.0, 1.0 - min(latency / 1000.0, 1.0))
    return 100.0 * (0.40 * (1.0 - false_rate) + 0.30 * emergency + 0.20 * intent + 0.10 * latency_score)


def ranking_view(overall: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in overall:
        item = dict(row)
        item["screening_score"] = weighted_quality(row)
        item["screen_pass"] = bool(
            float(row.get("false_trigger_rate") or 1.0) <= 0.05
            and float(row.get("emergency_recall") or 0.0) >= 0.95
            and float(row.get("p95_latency_ms") or 9999.0) <= 500.0
        )
        result.append(item)
    return sorted(
        result,
        key=lambda row: (
            not row["screen_pass"],
            float(row.get("false_trigger_rate") if row.get("false_trigger_rate") is not None else 1.0),
            -(float(row.get("emergency_recall") or 0.0)),
            float(row.get("p95_latency_ms") or 9999.0),
            -(float(row.get("positive_intent_accuracy") or 0.0)),
        ),
    )


def display_pct(value: Any) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.1f}%"


def display_ms(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.1f}"


def render_table(rows: list[dict[str, Any]], title_key: str = "label") -> str:
    lines = [
        "| 方案 | 预处理 | 样本 | 意图准确率 | 紧停召回 | 误触率 | 拒识率 | P95 ms | 评分 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        label = row.get(title_key) or row.get("candidate")
        lines.append(
            "| {label} | {prep} | {total} | {intent} | {emergency} | {false_rate} | {reject} | {p95} | {score:.1f} |".format(
                label=label,
                prep=row.get("preprocessing", "-"),
                total=row.get("total", "-"),
                intent=display_pct(row.get("positive_intent_accuracy")),
                emergency=display_pct(row.get("emergency_recall")),
                false_rate=display_pct(row.get("false_trigger_rate")),
                reject=display_pct(row.get("rejection_rate")),
                p95=display_ms(row.get("p95_latency_ms")),
                score=float(row.get("screening_score") or 0.0),
            )
        )
    return "\n".join(lines)


def candidate_matrix(repo: Path, model_info: dict[str, Any], package_data: dict[str, Any]) -> str:
    vosk_repo = "https://github.com/alphacep/vosk-api"
    sherpa_repo = "https://github.com/k2-fsa/sherpa-onnx"
    whisper_repo = "https://github.com/ggerganov/whisper.cpp"
    return "\n".join(
        [
            "| 候选 | 本轮 | Windows | Android | 中文 | 许可证/模型注意 | 集成取舍 |",
            "|---|---:|---|---|---|---|---|",
            f"| 当前 Sherpa 全短语 KWS | 是 | Python wheel + 本地 ONNX | 官方项目支持 | 当前中文 Zipformer/KWS 模型 | 引擎 Apache-2.0（[仓库]({sherpa_repo})）；本地模型目录未见独立 LICENSE，需单独确认权利 | 与当前正式代码最接近，低延迟、拒识边界清楚；同义词需扩关键词并重建词表 |",
            f"| Vosk 受限词表 | 是 | 官方 Python/Windows wheel | 官方 API 支持 | `vosk-model-small-cn-0.22` | 引擎 Apache-2.0（[仓库]({vosk_repo})）；模型条款以本地模型 README/来源为准 | 依赖少、词表可控；长中文短语和噪声鲁棒性需实测 |",
            f"| Vosk 开放识别 | 是 | 同上 | 同上 | 同上 | 同上 | 覆盖开放文本，但严格 parser 仍决定是否触发，误识别风险更高 |",
            f"| sherpa-onnx 通用 ASR/Zipformer | 否（无第二套现成中文 ASR 模型） | 官方项目支持 | 官方项目支持 | 官方列出中文模型 | 引擎 Apache-2.0；具体权重需核对 | 可能提升同义词/开放句覆盖，但需新模型、VAD/端点和 parser，实时成本未测 |",
            f"| whisper.cpp tiny/base | 否（本机无可用二进制+中文模型） | 官方 README 列出 Windows | 官方 README 列出 Android | 多语种可用，中文质量/延迟需本机测 | 项目仓库含 MIT 许可信息；模型许可另核对（[仓库]({whisper_repo})） | 通用 ASR 方案，但非专用 KWS；端点延迟和模型体积不适合在本轮凭文档夺冠 |",
        ]
    )


def write_report(
    root: Path,
    repo: Path,
    manifest: dict[str, Any],
    ranking: list[dict[str, Any]],
    extreme: list[dict[str, Any]],
    resource_records: list[dict[str, Any]],
    raw_rows: list[dict[str, Any]],
) -> None:
    by_key = {(row["candidate"], row["preprocessing"]): row for row in ranking}
    table_rows = []
    for row in ranking:
        item = dict(row)
        item["label"] = f"{row['candidate']}"
        table_rows.append(item)
    top = ranking[0] if ranking else None
    passed = [row for row in ranking if row.get("screen_pass")]
    current_winner = top["candidate"] + "/" + top["preprocessing"] if top else "无可用候选"
    overall_conclusion = (
        f"相对第一名为 `{current_winner}`。"
        + ("它通过本轮保守筛选门槛，但仍不能替代真人验收。" if passed else "本轮没有达到保守的产品筛选门槛，结论为“当前无产品可接受胜者”。")
    )

    # Resource view: choose the smallest evaluated model, then state its quality.
    resource_by_candidate: dict[str, dict[str, Any]] = {}
    for record in resource_records:
        resource_by_candidate.setdefault(record["candidate"], record)
    smallest = sorted(resource_by_candidate.values(), key=lambda r: int(r["model"]["bytes"]))[0] if resource_by_candidate else None

    # Extreme noise uses SNR 5/0 dB rows.  Keep a high-recall/low-latency
    # candidate ahead of a zero-false-trigger recognizer that rejects every
    # command; zero false triggers alone are not useful for game control.
    extreme_sorted = sorted(
        extreme,
        key=lambda row: (
            -(float(row.get("emergency_recall") or 0.0)),
            -(float(row.get("positive_intent_accuracy") or 0.0)),
            float(row.get("false_trigger_rate") if row.get("false_trigger_rate") is not None else 1.0),
            float(row.get("p95_latency_ms") or 9999.0),
        ),
    )
    extreme_top = extreme_sorted[0] if extreme_sorted else None

    failure_counter: dict[tuple[str, str], int] = defaultdict(int)
    for row in raw_rows:
        if row["kind"] == "positive" and row["predicted_intent"] != row["expected_intent"]:
            failure_counter[(row["candidate"], row["condition"])] += 1
        if row["kind"] != "positive" and row["false_trigger"]:
            failure_counter[(row["candidate"], row["condition"])] += 1
    worst_failures = sorted(failure_counter.items(), key=lambda pair: -pair[1])[:12]

    resource_lines = [
        "| 方案 | 模型目录字节 | 加载 ms | RSS 增量（近似） |",
        "|---|---:|---:|---:|",
    ]
    for record in resource_records:
        resource_lines.append(
            f"| {record['candidate']} | {record['model']['bytes']:,} | {record['load_ms']:.1f} | {record['rss_delta_bytes'] if record['rss_delta_bytes'] is not None else 'n/a'} |"
        )

    lines = [
        "# 中文语音极端条件离线对比 v1 报告",
        "",
        f"生成时间：{manifest['generated_at']}；语料样本：{manifest['counts']['speech_clips']} 个合成语音 WAV，评测 case：{manifest['counts']['cases']}；候选：{manifest['counts']['successful_candidates']} 个成功运行。",
        "",
        "## 结论",
        "",
        overall_conclusion,
        "",
        f"- 综合第一名：`{current_winner}`（安全优先排序：误触率 → 紧急停止召回 → P95 处理耗时 → 正例意图准确率）。",
        f"- 低资源第一名：`{smallest['candidate'] if smallest else '无'}`（本轮实测模型目录最小；它的准确率、误触和紧停数据仍以表格为准，不因体积小自动胜出）。",
        f"- 强噪声相对第一名：`{extreme_top['candidate']}/{extreme_top['preprocessing'] if extreme_top else 'n/a'}`（SNR 5/0 dB 子集；若紧停召回未达 95% 保守线，则不视为强噪声产品胜者）。" if extreme_top else "- 强噪声相对第一名：无可用结果。",
        "",
        "筛选门槛是本次实验的保守比较线，不是已完成的产品验收：误触率 ≤5%、紧急停止召回 ≥95%、P95 处理耗时 ≤500 ms。真人小声、手机麦克风、真实房间噪声和持续在线端点仍未验收。",
        "",
        "## 综合排名",
        "",
        render_table(table_rows),
        "",
        "评分仅用于排序，计算为 `0.40×(1-误触率) + 0.30×紧停召回 + 0.20×正例意图准确率 + 0.10×(1-min(P95/1000,1))`。误触和紧停权重高于普通整句字面准确率。",
        "",
        "## 极端噪声子集（SNR 5/0 dB）",
        "",
        render_table(extreme_sorted),
        "",
        "## 资源指标",
        "",
        "模型目录大小是当前本地权重目录的字节数；RSS 增量包含解释器/依赖的共享开销，只作为同进程近似，不是手机峰值内存。加载时间不计入逐条 P95，但单独报告。",
        "",
        *resource_lines,
        "",
        "## 方案筛选与落地性",
        "",
        candidate_matrix(repo, manifest["models"]["vosk"], manifest["packages"]),
        "",
        "本轮真实跑过同一套音频的只有 Sherpa 当前 KWS、Vosk 受限词表、Vosk 开放识别；通用 sherpa-onnx ASR 和 whisper.cpp 没有可核验的本机模型/二进制，因此只列为资料候选，不参加“最好”结论。",
        "",
        "## 语料与退化",
        "",
        f"- 正例覆盖：唤醒词+开始校准、紧急停止、上下左右、开始/停止输出、加速/攻击/闪避/跳跃/换弹/技能/地图/截图/设置中心；另含 `向上/向左/向右/自动校准` 同义短语。",
        "- 负例覆盖：唤醒词单独出现、紧停/校准近似句、同音/近音短语、缺少唤醒词、疑问句和普通非命令句；另有白噪声、粉红噪声、风扇型噪声纯负例。",
        "- 清洁响度：-6、-18、-30 dBFS；噪声：20、10、5、0 dB SNR。噪声 case 只对标记为 stress 的小子集运行，避免无意义的组合膨胀。",
        "- 轻预处理：静音裁剪 + 80 Hz 一阶高通 + -18 dBFS 归一化；没有重型降噪。",
        "",
        "## 主要失效点",
        "",
    ]
    if worst_failures:
        for (candidate, condition), count in worst_failures:
            lines.append(f"- `{candidate}` 在 `{condition}` 出现 {count} 个意图错误或负例误触发；逐条证据见 `RESULT.csv`。")
    else:
        lines.append("- 没有记录到失败 case；请以 RESULT.json/CSV 的逐条结果复核。")
    lines += [
        "",
        "当前 Sherpa KWS 的固有边界是全短语关键词：正式 `voice_action_map.json` 没有把所有 `voice_mappings.json` 同义词都做成关键词，所以 `体感向上/向左/向右/体感自动校准` 是有意加入的兼容压力项；这不等同于模型听不懂，而是词表/映射覆盖不足。",
        "",
        "## 最小下一步",
        "",
        "1. 不把本报告的相对第一名直接接入正式软件；先录制同一短语的真人正常音量、小声和手机麦克风样本，保留输出禁用。",
        "2. 若真人数据确认误触可控，再只扩展当前 KWS 的必要同义词并重复这套回放；同时确认模型权重的独立许可文件。",
        "3. 若真实房间噪声下仍需要开放句覆盖，再单独引入一个轻量中文流式 ASR/VAD 试验，不与 KWS 叠加到正式链路，除非延迟和误触证据足够。",
        "",
        "## 证据边界",
        "",
        "这是合成 TTS + 确定性噪声的离线压力测试。它不是真人声学测试，不是手机本地麦克风实测，不是 Android 性能实测，也没有连接游戏输出。",
        "",
        "详细复现入口、当前快照、包版本、模型哈希和逐条预测在同目录的 `manifest.json`、`RESULT.json`、`RESULT.csv`。",
    ]
    (root / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    handoff = [
        "# MotionControl 中文语音离线对比交接",
        "",
        f"产物：`{root}`",
        f"正式仓库快照：`{manifest['git']['head']}`；本试验未修改生产源码，`test_results/voice_robustness_v1` 之外的原有脏改动状态：`{'clean' if manifest['git']['clean_outside_this_artifact'] else 'dirty（运行前已有/保留）'}`。",
        "",
        f"综合相对第一名：`{current_winner}`；低资源（仅模型体积）：`{smallest['candidate'] if smallest else '无'}`；强噪声相对第一名：`{extreme_top['candidate'] if extreme_top else '无'}`（未达紧停保守线时不视为产品胜者）。",
        "",
        ("产品筛选结论：本轮无产品可接受胜者。" if not passed else "产品筛选结论：相对第一名通过本轮保守筛选线，但仍需真人验收。"),
        "",
        "实测候选只有当前 Sherpa 全短语 KWS、Vosk 受限词表、Vosk 开放识别；Whisper/通用 sherpa-onnx ASR 未因缺少本地可核验模型而参加排名。TTS+合成噪声只是压力代理，不能冒充手机麦克风/真人验收。",
        "",
        "建议下一步：在输出禁用下录制真人正常/小声/真实房间噪声，回放同一 manifest，再决定是否只扩 KWS 同义词或另开轻量流式 ASR 试验。",
        "",
        "完整数据：`RESULT.json`、`RESULT.csv`、`REPORT.md`；复现：`run_experiment.ps1`。",
    ]
    (root / "HANDOFF.md").write_text("\n".join(handoff) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument(
        "--preprocessing",
        choices=("raw", "light_preprocess", "both"),
        default="both",
        help="Run raw audio, the light production-like preprocessing, or both.",
    )
    parser.add_argument(
        "--trial-lib",
        type=Path,
        help="Optional isolated site-packages directory; inserted before repository helper libraries.",
    )
    args = parser.parse_args()
    repo = args.repo.resolve()
    root = args.root.resolve()
    preprocessing_modes = (
        ("raw", "light_preprocess") if args.preprocessing == "both" else (args.preprocessing,)
    )

    pylibs = repo / ".pylibs"
    if pylibs.is_dir():
        sys.path.insert(0, str(pylibs))
    sys.path.insert(0, str(repo))
    if args.trial_lib:
        trial_lib = args.trial_lib.resolve()
        if not trial_lib.is_dir():
            raise FileNotFoundError(trial_lib)
        sys.path.insert(0, str(trial_lib))
    import numpy as np  # noqa: F401  # imported after path setup

    tts_manifest_path = root / "tts_manifest.json"
    if not tts_manifest_path.is_file():
        raise FileNotFoundError(f"run generate_tts.ps1 first: {tts_manifest_path}")
    tts_manifest = json_load(tts_manifest_path)

    noise_profile_dir = root / "noise_profiles"
    for noise_type in NOISE_TYPES:
        write_wav(noise_profile_dir / f"{noise_type}.wav", make_noise(noise_type, SAMPLE_RATE * 2, stable_seed("profile", noise_type)))

    action_map_path = repo / "config" / "generated_voice" / "voice_action_map.json"
    legacy_map_path = repo / "config" / "voice_mappings.json"
    action_map = json_load(action_map_path)
    legacy_map = json_load(legacy_map_path)
    parser_for_commands = CommandParser(action_map, legacy_map)

    vosk_config_text = (repo / "config" / "vosk_model_path.txt").read_text(encoding="utf-8-sig").strip()
    vosk_model_dir = Path(vosk_config_text)
    if not vosk_model_dir.is_absolute():
        vosk_model_dir = repo / vosk_model_dir
    sherpa_config_text = (repo / "config" / "sherpa_kws_model_path.txt").read_text(encoding="utf-8-sig").strip()
    sherpa_model_dir = Path(sherpa_config_text)
    if not sherpa_model_dir.is_absolute():
        sherpa_model_dir = repo / sherpa_model_dir
    if not vosk_model_dir.is_dir():
        raise FileNotFoundError(vosk_model_dir)
    if not sherpa_model_dir.is_dir():
        raise FileNotFoundError(sherpa_model_dir)

    cases, conditions = build_cases(root, tts_manifest)
    positive_texts = [str(item.get("text")) for item in tts_manifest.get("items", []) if item.get("kind") == "positive"]
    # The Chinese Vosk model stores character tokens rather than whole command
    # phrases.  Passing an unspaced phrase makes Kaldi drop the entry as an
    # unknown vocabulary item (and turns the constrained baseline into a silent
    # failure).  Keep the same phrase set but express CJK entries as token-
    # separated characters; CommandParser.compact() removes the spaces again.
    def grammar_entry(text: str) -> str:
        value = str(text or "").strip()
        if any("\u4e00" <= char <= "\u9fff" for char in value):
            return " ".join(char for char in value if not char.isspace())
        return value

    grammar = sorted(set(grammar_entry(text) for text in list(action_map.keys()) + positive_texts))

    models = {
        "vosk": model_tree_info(vosk_model_dir),
        "sherpa": model_tree_info(sherpa_model_dir),
    }
    packages = package_info(["vosk", "sherpa-onnx", "sherpa-onnx-core", "numpy", "cffi"])
    git = git_snapshot(repo)
    manifest: dict[str, Any] = {
        "schema": "voice_robustness_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": str(repo),
        "git": git,
        "input_protocol": "offline WAV -> recognizer/parser only; output disabled by construction",
        "sample_rate": SAMPLE_RATE,
        "chunk_ms": 200,
        "tts": tts_manifest,
        "models": models,
        "packages": packages,
        "candidate_defs": [
            {"id": "sherpa_phrase_kws_current", "evaluated": True, "source": "production sherpa_phrase_kws.py + generated keyword map"},
            {"id": "vosk_constrained", "evaluated": True, "source": "Vosk KaldiRecognizer grammar"},
            {"id": "vosk_open", "evaluated": True, "source": "Vosk KaldiRecognizer open vocabulary"},
            {"id": "sherpa_general_asr", "evaluated": False, "reason": "no separate local Chinese ASR model selected/downloaded"},
            {"id": "whisper_cpp", "evaluated": False, "reason": "no local binary and Chinese model available for this run"},
        ],
        "conditions": {
            "clean_levels_dbfs": list(CLEAN_LEVELS_DBFS),
            "noise_types": list(NOISE_TYPES),
            "snr_db": list(SNRS_DB),
            "preprocessing": list(preprocessing_modes),
            "noise_scope": "items marked stress in tts_manifest",
            "noise_seed": "sha256(item_id|voice_id|noise_type|snr_db) first 32 bits",
        },
        "mapping_sources": {
            "current_full_phrase": str(action_map_path),
            "legacy_synonyms": str(legacy_map_path),
        },
        "counts": {
            "speech_clips": len(tts_manifest.get("items", [])),
            "cases": len(cases),
            "conditions": len(conditions),
        },
    }

    # Ensure a failed candidate cannot silently become a ranking result.
    candidates: list[tuple[CandidateBase, dict[str, Any]]] = []
    candidate_errors: list[dict[str, Any]] = []
    constructors = [
        ("sherpa_phrase_kws_current", lambda: SherpaCurrentCandidate(parser_for_commands, repo, sherpa_model_dir)),
        ("vosk_constrained", lambda: VoskCandidate(parser_for_commands, vosk_model_dir, True, grammar)),
        ("vosk_open", lambda: VoskCandidate(parser_for_commands, vosk_model_dir, False, grammar)),
    ]
    all_rows: list[dict[str, Any]] = []
    resource_records: list[dict[str, Any]] = []
    run_timings: list[dict[str, Any]] = []
    for candidate_id, constructor in constructors:
        candidate = None
        try:
            candidate = constructor()
            model_key = "sherpa" if candidate_id.startswith("sherpa") else "vosk"
            resource = candidate.resource_record(models[model_key])
            resource_records.append(resource)
            for preprocessing in preprocessing_modes:
                rows, timing = run_candidate(candidate, candidate_id, cases, preprocessing)
                all_rows.extend(rows)
                run_timings.append({"candidate": candidate_id, "preprocessing": preprocessing, **timing})
            del candidate
            gc.collect()
        except Exception as exc:
            candidate_errors.append({"candidate": candidate_id, "error": f"{type(exc).__name__}: {exc}"})
            if candidate is not None:
                try:
                    candidate.close()
                except Exception:
                    pass
            gc.collect()

    overall = aggregate(all_rows, ("candidate", "preprocessing"))
    ranking = ranking_view(overall)
    extreme_rows = [
        row
        for row in all_rows
        if row.get("condition_group") == "noise" and row.get("snr_db") in (5, 0)
    ]
    extreme = ranking_view(aggregate(extreme_rows, ("candidate", "preprocessing")))
    by_condition = aggregate(all_rows, ("candidate", "preprocessing", "condition"))

    manifest["counts"].update(
        {
            "successful_candidates": len({row["candidate"] for row in all_rows}),
            "prediction_rows": len(all_rows),
            "candidate_errors": len(candidate_errors),
        }
    )
    manifest["candidate_errors"] = candidate_errors
    manifest["resources"] = resource_records
    manifest["run_timings"] = run_timings
    manifest["ranking_policy"] = {
        "safety_first_order": ["false_trigger_rate", "emergency_recall", "p95_latency_ms", "positive_intent_accuracy"],
        "screening_line": {"false_trigger_rate_max": 0.05, "emergency_recall_min": 0.95, "p95_latency_ms_max": 500},
        "weighted_score": "0.40*(1-false_trigger_rate)+0.30*emergency_recall+0.20*positive_intent_accuracy+0.10*latency_score",
    }
    manifest["counts"]["clean_positive_cases"] = sum(1 for row in all_rows if row["condition_group"] == "clean" and row["kind"] == "positive")
    manifest["counts"]["noise_cases"] = sum(1 for row in all_rows if row["condition_group"] in ("noise", "noise_only"))
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "schema": "voice_robustness_result_v1",
        "manifest": "manifest.json",
        "overall": ranking,
        "extreme_noise_snr_5_0": extreme,
        "by_condition": by_condition,
        "resources": resource_records,
        "candidate_errors": candidate_errors,
        "predictions": all_rows,
    }
    (root / "RESULT.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_fields = [
        "case_id", "item_id", "voice_id", "condition", "condition_group", "noise_type", "snr_db", "kind", "text",
        "candidate", "preprocessing", "latency_ms", "cpu_ms", "expected_intent", "predicted_intent", "predicted_text",
        "triggered", "intent_correct", "sentence_exact", "false_trigger", "is_emergency", "emergency_hit", "error",
    ]
    with (root / "RESULT.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    write_report(root, repo, manifest, ranking, extreme, resource_records, all_rows)
    print(json.dumps({
        "artifact_root": str(root),
        "successful_candidates": manifest["counts"]["successful_candidates"],
        "cases": manifest["counts"]["cases"],
        "rows": len(all_rows),
        "overall": [{"candidate": row["candidate"], "preprocessing": row["preprocessing"], "false_trigger_rate": row["false_trigger_rate"], "emergency_recall": row["emergency_recall"], "p95_latency_ms": row["p95_latency_ms"]} for row in ranking],
        "candidate_errors": candidate_errors,
    }, ensure_ascii=False, indent=2))
    return 0 if all_rows else 2


if __name__ == "__main__":
    raise SystemExit(main())
