"""从固定上游发布下载并核验 HFHPE 模型；不运行下载内容。"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tarfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_COMMIT = "6c0cab8cc8e5586f4126ae9a7d30971e08016291"
RELEASE_URL = "https://github.com/PINTO0309/High-Angle_Robust_Fast_Head_Pose_Estimation/releases/download/weights"
MODEL_SPECS = {
    "vitt128": {
        "archive": "vitt_distill_128_yp_unified_v12.tar.gz",
        "archive_size": 157895143,
        "archive_sha256": "57656370e441c09945de079b5378d1902ac836784d305379ee1975325d53a7b6",
        "member": "vitt_distill_128_yp_unified_v12/hfhpe_vitt_rollgate_1x3x128x128.onnx",
        "size": 25764719,
        "sha256": "ef2496a942653bb790ae60f9a942deb7125fca6085f7c163b020e511fefafc54",
    },
    "vitt64": {
        "archive": "vitt_distill_064_yp_unified_v12.tar.gz",
        "archive_size": 157848769,
        "archive_sha256": "b0085ce8788665a5417667fb461fc740f7adc16b441a2e39c4dec3bf60e1e306",
        "member": "vitt_distill_064_yp_unified_v12/hfhpe_vitt_rollgate_1x3x64x64.onnx",
        "size": 25641823,
        "sha256": "240cce2e0f48a474ba56a4eae9d8a035ffd1453b34f6a963b0e04fbdeea71ca7",
    },
    "hgnet64": {
        "archive": "hgnetv2_distill_064_yp_unified_v12.tar.gz",
        "archive_size": 60647553,
        "archive_sha256": "7fde9db952f8d54d6214c590699e47c3b92fc7f787f9203db0afa259161f5c38",
        "member": "hgnetv2_distill_064_yp_unified_v12/hfhpe_hgnetv2_rollgate_1x3x64x64.onnx",
        "size": 10573002,
        "sha256": "1bb95ca9abbd33f23b6b9f29826066aed64ceaa1ddcb89bfa38030f6c85b5dca",
    },
}
DETECTOR_NAME = "deimv2_hgnetv2_n_wholebody49_boxes_only.onnx"
DETECTOR_SHA256 = "155c3c6f37dcf398288735f96a18738b67eb8f7d02ec54f024c75bed0a169e31"
DETECTOR_SIZE = 15979709


def verified(path: Path, sha256: str, size: int) -> bool:
    if not path.is_file() or path.stat().st_size != size:
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == sha256


def write_verified(source, destination: Path, sha256: str, size: int) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    try:
        with partial.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        if not verified(partial, sha256, size):
            raise ValueError(f"文件大小或校验值不一致，保留原文件：{destination.name}")
        partial.replace(destination)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return destination


def download_verified(url: str, destination: Path, sha256: str, size: int) -> Path:
    if verified(destination, sha256, size):
        return destination
    request = urllib.request.Request(url, headers={"User-Agent": "MotionControl-HFHPE-Download"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return write_verified(response, destination, sha256, size)


def extract_model(archive: Path, destination: Path, spec: dict) -> Path:
    with tarfile.open(archive, "r:gz") as bundle:
        member = bundle.getmember(spec["member"])
        if not member.isfile() or member.size != spec["size"]:
            raise ValueError("压缩包中指定的模型不是预期普通文件")
        # 只读取这一个成员，不展开包内路径，也不解压训练权重或其他文件。
        with bundle.extractfile(member) as stream:
            return write_verified(stream, destination, spec["sha256"], spec["size"])


def install_models(output: Path, variants=("vitt128",), *, with_detector=False) -> list[Path]:
    output = Path(output).resolve()
    paths = []
    for variant in dict.fromkeys(variants):
        spec = MODEL_SPECS[variant]
        model_path = output / Path(spec["member"]).name
        if not verified(model_path, spec["sha256"], spec["size"]):
            archive = download_verified(
                f"{RELEASE_URL}/{spec['archive']}", output / ".downloads" / spec["archive"],
                spec["archive_sha256"], spec["archive_size"],
            )
            extract_model(archive, model_path, spec)
        paths.append(model_path)
        print(f"模型已就绪：{model_path}")
    if with_detector:
        # 此固定哈希的轻量上游文件已无掩膜分支：829 个节点均通向唯一框输出。
        # 不再剪枝，不要求安装 onnx；实验模式本身复用现有身体识别来裁剪头部。
        path = download_verified(f"{RELEASE_URL}/{DETECTOR_NAME}", output / "deimv2_head.onnx",
                                 DETECTOR_SHA256, DETECTOR_SIZE)
        paths.append(path)
        print(f"可选研究检测器已就绪：{path}")
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description="下载并核验 HFHPE 头部姿态模型")
    parser.add_argument("--output", type=Path, default=ROOT / "models" / "hfhpe", help="模型存放目录")
    parser.add_argument("--model", nargs="+", choices=MODEL_SPECS, default=["vitt128"],
                        help="选择一种或多种实验模型，默认下载 128 像素模型")
    parser.add_argument("--with-detector", action="store_true", help="额外下载研究用整头检测器，体感实验模式不需要")
    args = parser.parse_args()
    install_models(args.output, args.model, with_detector=args.with_detector)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
