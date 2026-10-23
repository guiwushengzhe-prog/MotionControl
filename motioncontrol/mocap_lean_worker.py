"""个人研究用的外部 MocapNET 适配；不包含或分发上游源码、模型。"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from .depth_comparison_worker import read_packet, write_packet


class MocapPoseFeatures:
    def __init__(self, spec):
        for folder in spec.get("packages", []):
            sys.path.insert(0, str(Path(folder).resolve()))
        import numpy as np
        import onnxruntime as ort
        from motioncontrol_shared.pose_points import MP_NAMES

        self.np, self.names = np, MP_NAMES
        source, checkpoint = Path(spec["source"]), Path(spec["checkpoint"])
        if hashlib.sha256(checkpoint.read_bytes()).hexdigest() != spec["sha256"]:
            raise ValueError("MocapNET 模型校验不匹配")
        raw = Path(spec["readout"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != spec["readout_sha256"]:
            raise ValueError("个人倾角读出校验不匹配")
        head = json.loads(raw)
        if head.get("type") != "mocapnet_personal_readout" or head.get("model_sha256") != spec["sha256"]:
            raise ValueError("个人倾角读出与当前网络不匹配")
        self.keys = head["keys"]
        self.coef = np.asarray(head["weights"], dtype=np.float64)
        if (len(self.keys) != 42 or len(set(self.keys)) != 42 or self.coef.shape != (42,)
                or not np.isfinite(self.coef).all()):
            raise ValueError("个人倾角读出参数无效")
        sys.path.insert(0, str(source))
        # 上游 tools.py 与本项目包同名；只在隔离进程内绑定，不污染体感进程。
        module_spec = importlib.util.spec_from_file_location("tools", source / "tools.py")
        module = importlib.util.module_from_spec(module_spec)
        sys.modules["tools"] = module
        module_spec.loader.exec_module(module)
        # 上游神经网络模块导入此符号，但本路径不运行绘制或迭代骨骼拟合。
        def unsupported_bvh(*args, **kwargs):
            raise RuntimeError("个人倾角路径不加载骨骼绘制和迭代拟合组件")
        sys.modules["bvhConverter"] = SimpleNamespace(BVH=unsupported_bvh)
        from MocapNETONNX import MocapNETONNXSubProblem
        from holisticPartNames import getHolisticBodyNameList, processPoseLandmarks, guessLandmarks
        self.labels = getHolisticBodyNameList()
        self.process, self.guess = processPoseLandmarks, guessLandmarks
        factory = ort.SessionOptions
        def options():
            value = factory()
            value.intra_op_num_threads, value.inter_op_num_threads = 2, 1
            value.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            value.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            value.add_session_config_entry("session.intra_op.allow_spinning", "0")
            value.add_session_config_entry("session.inter_op.allow_spinning", "0")
            return value
        ort.SessionOptions = options
        try:
            self.model = MocapNETONNXSubProblem(None, str(checkpoint.parent / "upperbody_configuration.json"),
                                               str(checkpoint), "upperbody")
        finally:
            ort.SessionOptions = factory
        if self.model.model.get_providers() != ["CPUExecutionProvider"]:
            raise ValueError("个人研究网络必须仅在处理器运行")

    def infer(self, pose, width, height):
        np = self.np
        try:
            for name in ("left_shoulder", "right_shoulder", "left_hip", "right_hip"):
                p = pose[name]
                if (p.get("score", 0) < .8 or not np.isfinite([p["x"], p["y"]]).all()
                        or not 0 <= p["x"] <= 1 or not 0 <= p["y"] <= 1):
                    return None
            points = [pose[name] for name in self.names]
            if not np.isfinite([[p["x"], p["y"], p.get("score", 0)] for p in points]).all():
                return None
            landmarks = SimpleNamespace(landmark=[SimpleNamespace(x=p["x"], y=p["y"], z=0., visibility=p.get("score", 0)) for p in points])
        except (KeyError, TypeError, ValueError):
            return None
        inputs = {}
        self.process(inputs, self.labels, landmarks, width / height, 1920 / 1080)
        self.guess(inputs)
        self.model.output, self.model.incompleteInput = {}, 1
        output = self.model.predict(inputs)
        if not output or self.model.incompleteInput:
            return None
        values = np.asarray([output[k] / (100 if k.endswith("position") else 180) for k in self.keys], dtype=np.float64)
        if not np.isfinite(values).all():
            return None
        return {"features": values.tolist(), "linear_coef": self.coef.tolist(),
                "geometric_deg": float(values @ self.coef), "sampled_regions": 0,
                "personal_research": True, "source": "mocapnet_personal"}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--runtime", required=True)
    p.add_argument("--model", default="mocapnet_personal")
    p.add_argument("--size", type=int, default=392)
    p.add_argument("--auto-scale", action="store_true")
    args = p.parse_args()
    output, incoming = sys.stdout.buffer, sys.stdin.buffer
    try:
        spec = json.loads(Path(args.runtime).read_text(encoding="utf-8-sig"))["models"][args.model]
        started = time.perf_counter()
        with contextlib.redirect_stdout(sys.stderr):
            adapter = MocapPoseFeatures(spec)
        write_packet(output, {"kind": "ready", "device": "cpu", "device_name": "处理器 · MocapNET个人研究",
            "load_ms": round((time.perf_counter()-started)*1000, 1), "feature_only": True, "feature_dim": 42})
        while True:
            meta, payload = read_packet(incoming)
            width, height = meta["width"], meta["height"]
            if payload or type(width) is not int or type(height) is not int or not 0 < width <= 4096 or not 0 < height <= 4096:
                raise ValueError("轻量倾角仅接收骨骼点，不接收图像或深度")
            started = time.perf_counter()
            with contextlib.redirect_stdout(sys.stderr):
                torso = adapter.infer(meta.get("pose") or {}, width, height)
            write_packet(output, {"kind": "frame", "sequence": meta["sequence"], "sample_at": meta["sample_at"],
                "inference_ms": round((time.perf_counter()-started)*1000, 3), "torso": torso,
                "input_shape": None, "input_points": len(meta.get("pose") or {}), "comparison": {},
                "preview_generated": False, "gpu_allocated_mb": 0, "gpu_reserved_mb": 0})
    except EOFError:
        pass
    except Exception as exc:
        import traceback
        traceback.print_exc(file=sys.stderr)
        write_packet(output, {"kind": "error", "error": str(exc)})


if __name__ == "__main__":
    main()
