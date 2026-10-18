"""HFHPE 彩色头姿推理：复用人体定位，只在选择实验转头时加载和运行。

裁剪/归一化及角度解码遵循上游 demo_hfhpe_onnx.py。整头位置由已有
Pose33 的耳、眼和肩部确定，不再运行一遍整头检测器。模型角度独立于骨骼
几何角度；看不到头时交付无效结果，控制器立即停止。
"""
from __future__ import annotations

import math
from pathlib import Path
import time

from .user_paths import user_data_root

MODEL_FILES = {
    "vitt128": "hfhpe_vitt_rollgate_1x3x128x128.onnx",
    "vitt64": "hfhpe_vitt_rollgate_1x3x64x64.onnx",
    "hgnet64": "hfhpe_hgnetv2_rollgate_1x3x64x64.onnx",
}
MODEL_SIZES = {"vitt128": 128, "vitt64": 64, "hgnet64": 64}


def head_box(pose, width, height):
    """从当前同一人的骨骼取整头粗框；不复用过期框、不产生头部角度。"""
    def point(name):
        p = (pose or {}).get(name, {})
        try:
            x, y, score = float(p["x"]), float(p["y"]), float(p.get("score", 1))
        except (KeyError, TypeError, ValueError):
            return None
        return (x * width, y * height) if all(map(math.isfinite, (x, y, score))) and score >= .45 and 0 <= x <= 1 and 0 <= y <= 1 else None

    ears = [p for name in ("left_ear", "right_ear") if (p := point(name))]
    eyes = [p for name in ("left_eye", "right_eye") if (p := point(name))]
    face = [p for name in ("nose", "left_ear", "right_ear", "left_eye", "right_eye", "mouth_left", "mouth_right") if (p := point(name))]
    if len(face) < 2:
        return None
    shoulders = [p for name in ("left_shoulder", "right_shoulder") if (p := point(name))]
    span = lambda pts: math.dist(*pts) if len(pts) == 2 else 0.
    fw = max(p[0] for p in face) - min(p[0] for p in face)
    fh = max(p[1] for p in face) - min(p[1] for p in face)
    # 耳眼不是头部外轮廓；留出颅顶、头发和后脑，避免只把五官送进整头模型。
    side = max(span(ears) * 1.9, span(eyes) * 4.2, span(shoulders) * .78, fw * 1.75, fh * 2.15)
    if side < 16 or side > min(width, height) * .95:
        return None
    anchor = ears if len(ears) == 2 else face
    cx = sum(p[0] for p in anchor) / len(anchor)
    cy = sum(p[1] for p in anchor) / len(anchor) - .09 * side
    return (cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2)


def prepare_crop(frame, box, size):
    """上游整头裁剪约定：每侧加5%，长边方形，移入画内，必要时补黑。"""
    import cv2
    import numpy as np

    height, width = frame.shape[:2]
    x1, y1, x2, y2 = box
    side = max(x2 - x1, y2 - y1) * 1.1
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    sx = min(max(cx - side / 2, 0), max(width - side, 0))
    sy = min(max(cy - side / 2, 0), max(height - side, 0))
    crop = frame[round(sy):round(min(sy + side, height)), round(sx):round(min(sx + side, width))]
    h, w = crop.shape[:2]
    if not h or not w:
        raise ValueError("头部裁剪为空")
    if h != w:
        padded = np.zeros((max(h, w), max(h, w), 3), dtype=crop.dtype)
        oy, ox = (max(h, w) - h) // 2, (max(h, w) - w) // 2
        padded[oy:oy + h, ox:ox + w] = crop
        crop = padded
    interpolation = cv2.INTER_AREA if size < crop.shape[0] else cv2.INTER_LANCZOS4
    rgb = cv2.cvtColor(cv2.resize(crop, (size, size), interpolation=interpolation), cv2.COLOR_BGR2RGB)
    return np.ascontiguousarray((rgb.astype(np.float32) / 127.5 - 1).transpose(2, 0, 1)[None])


def decode_pose(cos_sin, kappa):
    import numpy as np

    cs = np.asarray(cos_sin).reshape(-1)
    concentration = np.asarray(kappa).reshape(-1)
    if cs.size != 6 or concentration.size != 3 or not np.all(np.isfinite(cs)) or not np.all(np.isfinite(concentration)):
        raise ValueError("头姿模型返回无效数值")
    if any(math.hypot(float(cs[i]), float(cs[i + 1])) < .01 for i in (0, 2, 4)) or np.any(concentration < 0):
        raise ValueError("头姿模型返回无效方向")
    # kappa 是角度分布集中程度，不能假充0到1的识别概率。
    yaw, pitch, roll = [math.degrees(math.atan2(float(cs[i + 1]), float(cs[i]))) for i in (0, 2, 4)]
    return {"source": "hfhpe", "valid": True, "yaw_deg": yaw, "pitch_deg": pitch,
            "roll_deg": roll, "kappa": [float(value) for value in concentration]}


class HfhpeEstimator:
    def __init__(self, model_path=None, *, model="vitt128", session_factory=None):
        self.model = model
        self.model_path = Path(model_path) if model_path else None
        self._factory = session_factory
        self.session = None
        self._retry_at = 0.
        self._state = {"state": "inactive", "message": "", "model": model}

    def _load(self):
        import onnxruntime as ort

        name = MODEL_FILES[self.model]
        paths = [self.model_path] if self.model_path else [
            Path(__file__).resolve().parents[1] / "models" / "hfhpe" / name,
            user_data_root() / "models" / "hfhpe" / name,
        ]
        path = next((p for p in paths if p.is_file()), None)
        if path is None:
            raise FileNotFoundError(f"缺少实验模型，请运行 python tools/download_hfhpe_models.py --model {self.model} 下载后重试")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        factory = self._factory or ort.InferenceSession
        session = factory(str(path), sess_options=options, providers=["CPUExecutionProvider"])
        inputs, outputs = session.get_inputs(), {p.name for p in session.get_outputs()}
        size = MODEL_SIZES[self.model]
        if len(inputs) != 1 or inputs[0].name != "images" or inputs[0].shape != [1, 3, size, size] or not {"cos_sin", "kappa"} <= outputs:
            raise ValueError("实验头姿模型格式不匹配，请重新下载官方模型")
        metadata = session.get_modelmeta().custom_metadata_map
        description = metadata.get("input.images", "")
        if description and "x/127.5 - 1" not in description:
            raise ValueError("实验头姿模型颜色归一化不匹配")
        self.session, self.model_path = session, path
        self.size = inputs[0].shape[2]

    def process(self, frame, pose, *, enabled):
        if not enabled:
            self.close()
            return None
        now = time.monotonic()
        if self.session is None:
            if now < self._retry_at:
                return {"source": "hfhpe", "model": self.model, "valid": False, "error": self._state["message"]}
            self._state = {"state": "loading", "message": "正在加载实验头姿模型", "model": self.model}
            try:
                self._load()
            except Exception as exc:
                message = "缺少头姿推理运行库，请安装 requirements-runtime.txt 中的依赖" if isinstance(exc, ImportError) else str(exc)
                self._state = {"state": "error", "message": message, "model": self.model}
                self._retry_at = now + 5
                return {"source": "hfhpe", "model": self.model, "valid": False, "error": message}
        box = head_box(pose, frame.shape[1], frame.shape[0])
        if box is None:
            message = "未看清头部，请让整头和双肩入镜"
            self._state = {"state": "unavailable", "message": message, "model": self.model}
            return {"source": "hfhpe", "model": self.model, "valid": False, "error": message}
        started = time.perf_counter()
        try:
            tensor = prepare_crop(frame, box, self.size)
            result = decode_pose(*self.session.run(["cos_sin", "kappa"], {"images": tensor}))
        except Exception as exc:
            self._state = {"state": "error", "message": "头姿推理失败：" + str(exc), "model": self.model}
            return {"source": "hfhpe", "model": self.model, "valid": False, "error": self._state["message"]}
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        self._state = {"state": "ready", "message": "", "model": self.model,
                       "inference_ms": elapsed, "head_box": list(box), "kappa": result["kappa"]}
        # 和角度保留在同一帧，录制无需另外查询相机状态或重复推理。
        result.update(model=self.model, inference_ms=elapsed, head_box=list(box))
        return result

    def status(self):
        return dict(self._state)

    def close(self):
        if self._state["state"] != "inactive":
            self.session = None
            self._retry_at = 0.
            self._state = {"state": "inactive", "message": "", "model": self.model}
