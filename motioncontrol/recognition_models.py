"""身体模型目录、按实际帧预算选择模型，以及按需手势识别。"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path


class RecognitionModels:
    NAMES = {"full": "MediaPipe Pose Full", "heavy": "MediaPipe Pose Heavy",
             "gesture": "MediaPipe Gesture Recognizer"}
    FILES = {"full": ("pose_landmarker_full_compatible_075.task", "pose_landmarker_full.task"),
             "heavy": ("pose_landmarker_heavy.task",), "gesture": ("gesture_recognizer.task",)}

    def __init__(self, root: Path | None, bundled: Path | None = None):
        self.paths: dict[str, Path] = {}
        self._digests: dict[Path, tuple[int, int, str]] = {}
        roots = [p for p in (root, bundled) if p and p.is_dir()]
        # 开发模型库还存有手机模型；发布包优先从自己的 models 目录取。
        if root and (root.parent / "runtime").is_dir():
            roots.append(root.parent / "runtime")
        for key, names in self.FILES.items():
            for base in roots:
                for name in names:
                    direct = [base / "mediapipe" / name, base / name]
                    found = next((p for p in direct if p.is_file()), None)
                    if found is None:
                        found = next(base.rglob(name), None)
                    if found:
                        self.paths[key] = found.resolve()
                        break
                if key in self.paths:
                    break

    def metadata(self, key: str) -> dict:
        path = self.paths.get(key)
        stat = path.stat() if path and path.is_file() else None
        digest = ""
        if stat:
            cached = self._digests.get(path)
            if not cached or cached[:2] != (stat.st_size, stat.st_mtime_ns):
                with path.open("rb") as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
                self._digests[path] = (stat.st_size, stat.st_mtime_ns, digest)
            else:
                digest = cached[2]
        return {"id": "mp-" + key, "name": self.NAMES[key], "available": bool(stat),
                "size_bytes": stat.st_size if stat else 0, "sha256": digest,
                "url": "/api/model/mp-" + key, "points": 21 if key == "gesture" else 33}

    def manifest(self) -> list[dict]:
        return [self.metadata(key) for key in self.NAMES]


class AutoPoseChoice:
    """只测有人且手部加载完成的帧；先预热，留出少量帧预算余量。"""
    def __init__(self):
        self.samples: list[float] = []
        self.warmup = 0
        self.measured_ms: float | None = None
        self.reason = "识别到人体后按实际帧率选择"

    def observe(self, duration_ms: float, capture_fps: float | None, *, visible: bool,
                hands_ready: bool = True) -> str | None:
        if not visible or not hands_ready or not capture_fps or capture_fps < 2:
            return None
        self.warmup += 1
        if self.warmup <= 3:
            return None
        self.samples.append(duration_ms)
        if len(self.samples) < 24:
            return None
        values = sorted(self.samples[-24:])
        self.samples.clear()
        self.measured_ms = values[math.ceil(len(values) * .9) - 1]
        choice = "heavy" if self.measured_ms <= 1000 / capture_fps * .90 else "full"
        self.reason = ("高精度模型能跟上当前摄像头" if choice == "heavy" else
                       "完整模型更适合当前摄像头帧率")
        return choice


class LazyGestureRecognizer:
    """与推理线程同寿命。无需手势时不导入、不创建、不推理手部模型。"""
    def __init__(self, path: Path | None):
        self.path, self.detector = path, None
        self.state, self.error = "idle", None

    def close(self):
        if self.detector:
            self.detector.close()
            self.detector = None
        self.state, self.error = "idle", None

    def configure(self, enabled: bool):
        if not enabled:
            self.close()
        elif self.state == "idle":
            try:
                if not self.path or not self.path.is_file():
                    raise ValueError("手势模型未安装")
                from mediapipe.tasks import python
                from mediapipe.tasks.python import vision
                self.state = "loading"
                self.detector = vision.GestureRecognizer.create_from_options(
                    vision.GestureRecognizerOptions(
                        base_options=python.BaseOptions(model_asset_path=str(self.path)),
                        running_mode=vision.RunningMode.IMAGE, num_hands=1))
                self.state = "ready"
            except Exception as exc:
                self.state, self.error = "error", str(exc)

    def recognize(self, rgb, pose_map: dict | None, sides: list[str], mp) -> dict:
        self.configure(bool(sides))
        if not self.detector or not pose_map:
            return {}
        import cv2
        height, width = rgb.shape[:2]
        hands = {}
        for side in sides:
            wrist, elbow = pose_map.get(side + "_wrist"), pose_map.get(side + "_elbow")
            if not wrist or not elbow or min(wrist["score"], elbow["score"]) < .35:
                continue
            dx, dy = (wrist["x"]-elbow["x"])*width, (wrist["y"]-elbow["y"])*height
            forearm = math.hypot(dx, dy)
            if forearm < 8:
                continue
            size = int(min(max(forearm*1.5, 48), width, height))
            x = int(min(max(wrist["x"]*width+dx*.35-size/2, 0), width-size))
            y = int(min(max(wrist["y"]*height+dy*.35-size/2, 0), height-size))
            crop = cv2.resize(rgb[y:y+size, x:x+size], (256, 256))
            result = self.detector.recognize(mp.Image(image_format=mp.ImageFormat.SRGB, data=crop))
            if result.hand_landmarks:
                hands[side] = [{"x": (x+p.x*size)/width, "y": (y+p.y*size)/height,
                                "z": p.z*size/width, "score": 1.0}
                               for p in result.hand_landmarks[0]]
        return hands
