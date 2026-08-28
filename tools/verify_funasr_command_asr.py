from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    sys.path.insert(0, str(root))
    from funasr_command_backend import (
        FunAsrWorkerClient,
        find_fsmn_vad_model,
        find_funasr_python,
        find_seaco_model,
    )

    python_path = find_funasr_python(root)
    model_path = find_seaco_model(root)
    vad_path = find_fsmn_vad_model(root)
    if not python_path or not model_path or not vad_path:
        raise RuntimeError({
            "python": str(python_path) if python_path else None,
            "model": str(model_path) if model_path else None,
            "vad": str(vad_path) if vad_path else None,
        })
    client = FunAsrWorkerClient(root, python_path, model_path, vad_path, startup_timeout=180.0)
    try:
        if not client.wait_ready(180.0):
            raise RuntimeError(client.last_error or "FunASR worker 未就绪")
        # One second of silence is sufficient to validate the full process,
        # model load, JSON protocol and generate() call without pretending to
        # measure speech accuracy.
        answer = client.transcribe(
            b"\x00\x00" * 16000,
            sample_rate=16000,
            hotwords=["体感", "截图", "加速", "开始输出", "停止输出"],
        )
        print(json.dumps({
            "ok": True,
            "mode": "sherpa-wake+funasr-seaco",
            "python": str(python_path),
            "model": str(model_path),
            "vad": str(vad_path),
            "silence_result": answer,
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
