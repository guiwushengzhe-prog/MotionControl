from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path


SEACO_MODEL_ID = "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
SEACO_DIRNAME = "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
VAD_MODEL_ID = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
VAD_DIRNAME = "speech_fsmn_vad_zh-cn-16k-common-pytorch"


def _read_path_config(root: Path, filename: str) -> Path | None:
    path = root / "config" / filename
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8-sig").strip().strip('"')
    except OSError:
        return None
    if not text:
        return None
    value = Path(text)
    return value if value.is_absolute() else (root / value)


def find_funasr_python(root: Path) -> Path | None:
    candidates: list[Path] = []
    env = os.environ.get("MOTIONCONTROL_FUNASR_PYTHON", "").strip().strip('"')
    if env:
        candidates.append(Path(env))
    configured = _read_path_config(root, "funasr_python_path.txt")
    if configured:
        candidates.append(configured)
    candidates.extend([
        root / ".venv-funasr" / "Scripts" / "python.exe",
        root / ".venv-funasr" / "bin" / "python",
    ])
    for path in candidates:
        try:
            if path.is_file():
                return path.resolve()
        except OSError:
            pass
    return None


def find_seaco_model(root: Path) -> Path | None:
    candidates: list[Path] = []
    env = os.environ.get("MOTIONCONTROL_FUNASR_SEACO_MODEL", "").strip().strip('"')
    if env:
        candidates.append(Path(env))
    configured = _read_path_config(root, "funasr_seaco_model_path.txt")
    if configured:
        candidates.append(configured)
    candidates.extend([
        root / "models" / "funasr" / SEACO_DIRNAME,
        root / "models" / SEACO_DIRNAME,
    ])
    for path in candidates:
        try:
            if path.is_dir() and (path / "config.yaml").is_file() and (path / "model.pt").is_file():
                return path.resolve()
        except OSError:
            pass
    return None


def find_fsmn_vad_model(root: Path) -> Path | None:
    candidates: list[Path] = []
    env = os.environ.get("MOTIONCONTROL_FUNASR_VAD_MODEL", "").strip().strip('"')
    if env:
        candidates.append(Path(env))
    configured = _read_path_config(root, "funasr_vad_model_path.txt")
    if configured:
        candidates.append(configured)
    candidates.extend([
        root / "models" / "funasr" / VAD_DIRNAME,
        root / "models" / VAD_DIRNAME,
    ])
    for path in candidates:
        try:
            if path.is_dir() and (path / "config.yaml").is_file() and (path / "model.pt").is_file():
                return path.resolve()
        except OSError:
            pass
    return None


def build_hotwords(wake_word: str, mappings: list[dict], emergency_phrases: list[str]) -> list[str]:
    """Build the SeACo hotword list from the real MotionControl vocabulary."""
    result: list[str] = []
    seen: set[str] = set()
    for phrase in [wake_word, *emergency_phrases]:
        text = str(phrase or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    for mapping in mappings:
        for phrase in [mapping.get("phrase", ""), *mapping.get("synonyms", [])]:
            text = str(phrase or "").strip()
            if text and text not in seen:
                result.append(text)
                seen.add(text)
    return result


class FunAsrWorkerClient:
    """Persistent JSON-lines client for an isolated FunASR worker process.

    FunASR/PyTorch intentionally lives in ``.venv-funasr`` instead of the
    MediaPipe runtime.  This prevents ASR dependencies from changing NumPy,
    OpenCV or MediaPipe versions in the already-working visual pipeline.
    """

    def __init__(
        self,
        root: Path,
        python_path: Path,
        model_path: Path,
        vad_model_path: Path,
        *,
        startup_timeout: float = 120.0,
        request_timeout: float = 8.0,
    ) -> None:
        self.root = Path(root)
        self.python_path = Path(python_path)
        self.model_path = Path(model_path)
        self.vad_model_path = Path(vad_model_path)
        self.startup_timeout = float(startup_timeout)
        self.request_timeout = float(request_timeout)
        self.worker_path = self.root / "tools" / "funasr_command_worker.py"
        self.process: subprocess.Popen[str] | None = None
        self.ready = False
        self.ready_metadata: dict = {}
        self.last_error: str | None = None
        self.last_latency_ms: float | None = None
        self.last_text = ""
        self._ready_event = threading.Event()
        self._response_queue: queue.Queue[dict] = queue.Queue()
        self._reader_thread: threading.Thread | None = None
        self._request_lock = threading.Lock()
        self._stderr_handle = None
        self._closed = False

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("FunASR worker 已关闭")
        if self.process is not None and self.process.poll() is None:
            return
        if not self.worker_path.is_file():
            raise RuntimeError(f"FunASR worker 脚本不存在：{self.worker_path}")
        log_dir = self.root / "output"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._stderr_handle = (log_dir / "funasr-command-worker.log").open("a", encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        cmd = [
            str(self.python_path), "-X", "utf8", str(self.worker_path),
            "--model", str(self.model_path),
            "--vad-model", str(self.vad_model_path),
        ]
        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=str(self.root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr_handle,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
        except Exception:
            if self._stderr_handle is not None:
                self._stderr_handle.close()
                self._stderr_handle = None
            raise
        self.ready = False
        self.last_error = None
        self._ready_event.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, name="funasr-worker-reader", daemon=True)
        self._reader_thread.start()

    def start_async(self) -> None:
        try:
            self.start()
        except Exception as exc:
            self.last_error = str(exc)
            self._ready_event.set()

    def _reader_loop(self) -> None:
        proc = self.process
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                text = line.strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    # Library noise on stdout is ignored; full library logs are
                    # normally redirected to stderr by the worker itself.
                    continue
                kind = str(message.get("type", ""))
                if kind == "ready":
                    self.ready = True
                    self.ready_metadata = message
                    self.last_error = None
                    self._ready_event.set()
                elif kind == "fatal":
                    self.ready = False
                    self.last_error = str(message.get("error") or "FunASR worker 启动失败")
                    self._ready_event.set()
                elif message.get("id"):
                    self._response_queue.put(message)
        finally:
            if not self._closed and not self.last_error:
                code = proc.poll()
                self.last_error = f"FunASR worker 已退出（code={code}）"
            self.ready = False
            self._ready_event.set()

    def wait_ready(self, timeout: float | None = None) -> bool:
        self.start_async()
        self._ready_event.wait(self.startup_timeout if timeout is None else timeout)
        return self.ready

    def transcribe(self, pcm16: bytes, *, sample_rate: int, hotwords: list[str]) -> dict:
        if not pcm16:
            return {"text": "", "latency_ms": 0.0}
        with self._request_lock:
            if not self.wait_ready():
                raise RuntimeError(self.last_error or "FunASR 命令模型尚未就绪")
            proc = self.process
            if proc is None or proc.poll() is not None or proc.stdin is None:
                raise RuntimeError(self.last_error or "FunASR worker 未运行")
            request_id = uuid.uuid4().hex
            request = {
                "id": request_id,
                "type": "transcribe",
                "sample_rate": int(sample_rate),
                "pcm16_base64": base64.b64encode(pcm16).decode("ascii"),
                "hotwords": list(hotwords),
            }
            try:
                proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except Exception as exc:
                self.last_error = f"FunASR worker 写入失败：{exc}"
                raise RuntimeError(self.last_error) from exc

            deadline = time.monotonic() + self.request_timeout
            deferred: list[dict] = []
            try:
                while time.monotonic() < deadline:
                    remaining = max(0.01, deadline - time.monotonic())
                    try:
                        message = self._response_queue.get(timeout=min(0.25, remaining))
                    except queue.Empty:
                        if proc.poll() is not None:
                            raise RuntimeError(self.last_error or "FunASR worker 意外退出")
                        continue
                    if message.get("id") != request_id:
                        deferred.append(message)
                        continue
                    if message.get("ok") is not True:
                        error = str(message.get("error") or "FunASR 识别失败")
                        self.last_error = error
                        raise RuntimeError(error)
                    self.last_text = str(message.get("text") or "").strip()
                    latency = float(message.get("latency_ms") or 0.0)
                    self.last_latency_ms = latency
                    self.last_error = None
                    return {"text": self.last_text, "latency_ms": latency}
                raise RuntimeError("FunASR 命令识别超时")
            finally:
                for item in deferred:
                    self._response_queue.put(item)

    def status(self) -> dict:
        running = self.process is not None and self.process.poll() is None
        return {
            "ready": bool(self.ready and running),
            "running": running,
            "python_path": str(self.python_path),
            "model_path": str(self.model_path),
            "vad_model_path": str(self.vad_model_path),
            "last_error": self.last_error,
            "last_latency_ms": self.last_latency_ms,
            "last_text": self.last_text,
        }

    def close(self) -> None:
        self._closed = True
        proc = self.process
        self.process = None
        if proc is not None:
            try:
                if proc.stdin is not None:
                    proc.stdin.write(json.dumps({"type": "close"}) + "\n")
                    proc.stdin.flush()
            except Exception:
                pass
            try:
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        if self._stderr_handle is not None:
            try:
                self._stderr_handle.close()
            except Exception:
                pass
            self._stderr_handle = None
        self.ready = False
