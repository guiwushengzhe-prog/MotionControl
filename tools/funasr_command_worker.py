from __future__ import annotations

import argparse
import base64
import contextlib
import json
import sys
import time
from pathlib import Path


PROTOCOL_OUT = sys.stdout


def emit(message: dict) -> None:
    PROTOCOL_OUT.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    PROTOCOL_OUT.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MotionControl isolated FunASR command worker")
    parser.add_argument("--model", required=True)
    parser.add_argument("--vad-model", required=True)
    return parser.parse_args()


def extract_text(result) -> str:
    if isinstance(result, list):
        if not result:
            return ""
        first = result[0]
    else:
        first = result
    if isinstance(first, dict):
        return str(first.get("text") or first.get("sentence") or "").strip()
    return str(first or "").strip()


def main() -> int:
    args = parse_args()
    model_path = Path(args.model)
    vad_path = Path(args.vad_model)
    try:
        # FunASR can be verbose.  The parent process reserves stdout for the
        # JSON-lines protocol, so all library output is redirected to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            import numpy as np
            import torch
            from funasr import AutoModel

            try:
                torch.set_num_threads(max(2, min(6, (torch.get_num_threads() or 4))))
            except Exception:
                pass
            model = AutoModel(
                model=str(model_path),
                vad_model=str(vad_path),
                device="cpu",
                ncpu=4,
                disable_update=True,
            )
        emit({
            "type": "ready",
            "model": str(model_path),
            "vad_model": str(vad_path),
            "device": "cpu",
        })
    except Exception as exc:
        emit({"type": "fatal", "error": f"FunASR 模型加载失败：{exc}"})
        return 2

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if request.get("type") == "close":
            return 0
        if request.get("type") != "transcribe":
            continue
        request_id = str(request.get("id") or "")
        try:
            sample_rate = int(request.get("sample_rate") or 16000)
            if sample_rate != 16000:
                raise ValueError("命令识别当前只接受 16kHz PCM")
            encoded = str(request.get("pcm16_base64") or "")
            pcm = base64.b64decode(encoded, validate=True)
            if not pcm or len(pcm) % 2:
                raise ValueError("PCM16 数据为空或长度无效")
            if len(pcm) > 512 * 1024:
                raise ValueError("命令音频过长")
            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            hotwords = [str(x).strip() for x in request.get("hotwords", []) if str(x).strip()]
            hotword_text = " ".join(dict.fromkeys(hotwords))
            started = time.perf_counter()
            with contextlib.redirect_stdout(sys.stderr):
                result = model.generate(
                    input=samples,
                    hotword=hotword_text,
                    batch_size_s=30,
                )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            emit({
                "id": request_id,
                "ok": True,
                "text": extract_text(result),
                "latency_ms": round(elapsed_ms, 1),
            })
        except Exception as exc:
            emit({"id": request_id, "ok": False, "error": str(exc)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
