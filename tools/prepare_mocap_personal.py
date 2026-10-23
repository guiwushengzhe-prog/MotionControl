"""登记用户个人研究用的已有外部 MocapNET 文件；不下载或分发上游内容。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--model-dir", type=Path, required=True)
    p.add_argument("--readout", type=Path, required=True)
    p.add_argument("--destination", type=Path, required=True)
    p.add_argument("--registry", type=Path, required=True)
    p.add_argument("--packages", type=Path, action="append", default=[])
    args = p.parse_args()
    source = args.source / "src/python/mnet4"
    model = args.model_dir / "model.onnx"
    library_license = args.source / "license.txt"
    if not source.is_dir() or not model.is_file() or not library_license.is_file():
        p.error("需要完整的外部官方源码、上半身网络和原许可证")
    head = json.loads(args.readout.read_text(encoding="utf-8"))
    if head.get("model") != "mocapnet" or len(head.get("keys", [])) != 42 or len(head.get("weights", [])) != 42:
        p.error("需要已训练的42维MocapNET个人读出")
    registry = json.loads(args.registry.read_text(encoding="utf-8-sig"))
    if not Path(registry["python"]).is_file():
        p.error("独立推理解释器不存在")
    # 仅创建一个新的个人模型目录，避免覆盖已有版本或其他环境。
    args.destination.mkdir(parents=True, exist_ok=False)
    installed_source = args.destination / "source"
    installed_source.mkdir()
    for path in source.glob("*.py"):
        shutil.copy2(path, installed_source / path.name)
    installed_model = args.destination / "upperbody"
    shutil.copytree(args.model_dir, installed_model, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(library_license, args.destination / "license.txt")
    readout = {"type": "mocapnet_personal_readout", "version": 1, "model_sha256": digest(model),
               "keys": head["keys"], "weights": head["weights"],
               "purpose": "用户个人非商用研究，非通用精度承诺", "training": head.get("train_recordings", []),
               "test": head.get("test_recording"), "readout_origin_sha256": digest(args.readout)}
    readout_path = args.destination / "personal-readout.json"
    readout_path.write_text(json.dumps(readout, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    spec = {"source": str(installed_source.resolve()), "checkpoint": str((installed_model / "model.onnx").resolve()),
            "sha256": digest(model), "readout": str(readout_path.resolve()), "readout_sha256": digest(readout_path),
            "packages": [str(path.resolve()) for path in args.packages], "personal_research": True,
            "license": str((args.destination / "license.txt").resolve())}
    registry.setdefault("models", {})["mocapnet_personal"] = spec
    temporary = args.registry.with_suffix(args.registry.suffix + ".partial")
    temporary.write_text(json.dumps(registry, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    temporary.replace(args.registry)
    print(json.dumps({"model": "mocapnet_personal", "destination": str(args.destination),
                      "registry": str(args.registry), "enabled": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
