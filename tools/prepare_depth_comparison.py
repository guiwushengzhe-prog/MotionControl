"""准备三个官方小模型；资源留在指定目录，显式激活后才写本机资源索引。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motioncontrol.user_paths import user_data_root
from motioncontrol.config_transaction import atomic_bytes

SOURCES = {
    "dav2_metric_s": {"repo": "https://github.com/DepthAnything/Depth-Anything-V2.git", "folder": "dav2",
        "commit": "a561b849ebae10a6f5ef49e26c83cbbcd36c71bf",
        "sha256": "b782898d8a3e8be1f639de33837ed85e9b4b73e40f8f5e5cd99067588d722545",
        "weight": "https://huggingface.co/depth-anything/Depth-Anything-V2-Metric-Hypersim-Small/resolve/main/depth_anything_v2_metric_hypersim_vits.pth"},
    "vda_metric_s": {"repo": "https://github.com/DepthAnything/Video-Depth-Anything.git", "folder": "vda",
        "commit": "4f5ae23172ba60fd7bc11ef671cca678842c7072",
        "sha256": "3c28432b4e1f0d7bb31cad5151b6313b49457db5aa58d82e85bfb0f8b1311b33",
        "weight": "https://huggingface.co/depth-anything/Metric-Video-Depth-Anything-Small/resolve/main/metric_video_depth_anything_vits.pth"},
    "zipdepth": {"repo": "https://github.com/fabiotosi92/ZipDepth.git", "folder": "zipdepth",
        "commit": "91f3fd21e131641f51e8d35736d1958350180e3a", "local_weight": "checkpoints/zipdepth_base.pth",
        "sha256": "a55910bb0b99c8c5e641cb9206e810b269690ad94e8a2ef08c827c4679391a65"},
}


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def prepare(root, python, activate=False, hf_mirror="https://huggingface.co"):
    if not Path(python).is_file():
        raise ValueError("指定的独立Python运行环境不存在")
    mirror = urlsplit(hf_mirror)
    if mirror.scheme != "https" or not mirror.netloc or mirror.path not in ("", "/") or mirror.query or mirror.fragment:
        raise ValueError("权重下载地址需为有效的HTTPS站点根地址")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    registry = {"python": str(Path(python).resolve()), "models": {}}
    for ident, spec in SOURCES.items():
        source = root / spec["folder"]
        if not source.exists():
            subprocess.run(["git", "clone", "--depth", "1", "--no-checkout", spec["repo"], str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "fetch", "--depth", "1", "origin", spec["commit"]], check=True)
            subprocess.run(["git", "-C", str(source), "checkout", "--detach", spec["commit"]], check=True)
        actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        if actual != spec["commit"]:
            raise RuntimeError(f"{ident}源码不是已核对版本：{actual}，请使用独立目录准备指定提交")
        if subprocess.run(["git", "-C", str(source), "diff", "--quiet", "HEAD", "--"]).returncode:
            raise RuntimeError(f"{ident}源码存在本地修改，未激活")
        if spec.get("local_weight"):
            checkpoint = source / spec["local_weight"]
        else:
            checkpoint = root / (ident + ".pth")
            if not checkpoint.exists():
                partial = checkpoint.with_suffix(".pth.partial")
                print(f"下载{ident}官方权重…", flush=True)
                download_url = spec["weight"].replace("https://huggingface.co", hf_mirror.rstrip("/"), 1)
                request = urllib.request.Request(download_url, headers={"User-Agent": "MotionControl-depth-comparison"})
                with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as target:
                    while block := response.read(1024 * 1024):
                        target.write(block)
                if file_hash(partial) != spec["sha256"]:
                    raise RuntimeError("权重校验值不符，保留临时文件且未激活")
                partial.replace(checkpoint)
        digest = file_hash(checkpoint)
        if digest != spec["sha256"]:
            raise RuntimeError(f"{ident}权重与已核对版本不符，未激活")
        registry["models"][ident] = {"source": str(source), "checkpoint": str(checkpoint),
            "repository": spec["repo"], "commit": actual, "sha256": digest}
    (root / "motioncontrol-depth-manifest.json").write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    if activate:
        destination = user_data_root() / "depth_comparison_runtime.json"
        atomic_bytes(destination, json.dumps(registry, ensure_ascii=False, indent=2).encode("utf-8"))
    print(json.dumps({"prepared": list(registry["models"]), "activated": activate}, ensure_ascii=False), flush=True)
    return registry


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--python", required=True, help="独立深度运行环境的Python路径")
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--hf-mirror", default="https://huggingface.co", help="可选权重镜像站点；仍校验固定权重摘要")
    args = parser.parse_args()
    prepare(args.root, args.python, args.activate, args.hf_mirror)
