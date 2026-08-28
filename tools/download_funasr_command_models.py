from __future__ import annotations

import argparse
from pathlib import Path

from modelscope import snapshot_download

SEACO_ID = "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
VAD_ID = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
SEACO_DIR = "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
VAD_DIR = "speech_fsmn_vad_zh-cn-16k-common-pytorch"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    model_root = root / "models" / "funasr"
    model_root.mkdir(parents=True, exist_ok=True)
    seaco = model_root / SEACO_DIR
    vad = model_root / VAD_DIR
    print(f"[download] SeACoParaformer -> {seaco}")
    snapshot_download(SEACO_ID, local_dir=str(seaco))
    print(f"[download] FSMN-VAD -> {vad}")
    snapshot_download(VAD_ID, local_dir=str(vad))
    for path, name in [(seaco, "SeACoParaformer"), (vad, "FSMN-VAD")]:
        if not (path / "config.yaml").is_file() or not (path / "model.pt").is_file():
            raise RuntimeError(f"{name} 下载后缺少 config.yaml/model.pt：{path}")
    print("[OK] FunASR models downloaded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
