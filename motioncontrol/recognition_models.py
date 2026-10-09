"""身体模型目录、按实际帧预算选择模型，以及按需手势识别。"""
from __future__ import annotations

import hashlib
import math
import time
from concurrent.futures import ThreadPoolExecutor
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
        self.cpu_threads = None
        self.requested_cpu_threads = None

    def close(self):
        if self.detector:
            self.detector.close()
            self.detector = None
        self.state, self.error = "idle", None
        self.cpu_threads = None

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
                from motioncontrol.cpu_inference import create_cpu_task
                self.detector, self.cpu_threads, _ = create_cpu_task(
                    vision.GestureRecognizer, vision.GestureRecognizerOptions(
                        base_options=python.BaseOptions(model_asset_path=str(self.path)),
                        running_mode=vision.RunningMode.IMAGE, num_hands=1), hands=True, threads=self.requested_cpu_threads)
                self.state = "ready"
            except Exception as exc:
                self.state, self.error = "error", str(exc)

    def change_threads(self, count):
        if self.cpu_threads == count:
            return True
        if self.detector is None:
            self.requested_cpu_threads = count
            return True  # 没有手势需求时仍不加载模型。
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision
        from motioncontrol.cpu_inference import create_cpu_task
        try:
            detector, threads, _ = create_cpu_task(vision.GestureRecognizer,
                vision.GestureRecognizerOptions(base_options=python.BaseOptions(model_asset_path=str(self.path)),
                    running_mode=vision.RunningMode.IMAGE, num_hands=1), hands=True, threads=count)
        except Exception:
            return False
        if threads != count:
            detector.close()
            return False
        previous = self.detector
        self.detector, self.cpu_threads, self.requested_cpu_threads = detector, threads, count
        previous.close()
        return True

    def recognize(self, rgb, pose_map: dict | None, sides: list[str], mp) -> dict:
        self.configure(bool(sides))
        if not self.detector or not pose_map:
            return {}
        import cv2
        height, width = rgb.shape[:2]
        hands = {}
        for side in sides:
            bounds = hand_crop(pose_map, side, width, height)
            if bounds is None:
                continue
            x, y, size = bounds
            crop = cv2.resize(rgb[y:y+size, x:x+size], (256, 256))
            result = self.detector.recognize(mp.Image(image_format=mp.ImageFormat.SRGB, data=crop))
            if result.hand_landmarks:
                hands[side] = [{"x": (x+p.x*size)/width, "y": (y+p.y*size)/height,
                                "z": p.z*size/width, "score": 1.0}
                               for p in result.hand_landmarks[0]]
        return hands


def hand_crop(pose_map, side, width, height):
    """原有手部裁切规则，串行和并行路径共用。"""
    if not pose_map:
        return None
    wrist, elbow = pose_map.get(side + "_wrist"), pose_map.get(side + "_elbow")
    if not wrist or not elbow or min(wrist["score"], elbow["score"]) < .35:
        return None
    dx, dy = (wrist["x"]-elbow["x"])*width, (wrist["y"]-elbow["y"])*height
    forearm = math.hypot(dx, dy)
    if forearm < 8:
        return None
    size = int(min(max(forearm*1.5, 48), width, height))
    x = int(min(max(wrist["x"]*width+dx*.35-size/2, 0), width-size))
    y = int(min(max(wrist["y"]*height+dy*.35-size/2, 0), height-size))
    return x, y, size


def hand_crops_match(previous, current, sides, width, height, hands):
    """用本帧身体点复核提前裁切；快速移动、丢人、错手时重新识别本帧。"""
    for side in sides:
        before = hand_crop(previous, side, width, height)
        after = hand_crop(current, side, width, height)
        if before is None or after is None:
            return False
        x, y, size = before
        nx, ny, nsize = after
        if (abs(nx+nsize/2-x-size/2) > size*.15
                or abs(ny+nsize/2-y-size/2) > size*.15
                or not .8 <= nsize/size <= 1.2):
            return False
        points = hands.get(side)
        if points:
            wrist = current[side + "_wrist"]
            distance = math.hypot((points[0]["x"]-wrist["x"])*width,
                                  (points[0]["y"]-wrist["y"])*height)
            if distance > max(24, nsize/3):
                return False
    return True


class ScheduledGestureRecognizer:
    """一个按需线程，与身体识别重叠；不排队、不缓存跨帧手势。"""
    def __init__(self, path):
        self.gesture = LazyGestureRecognizer(path)
        self.executor = None

    def begin(self, rgb, pose_map, sides, mp):
        if self.executor is None:
            if not sides:
                return None
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="motion-camera-hands")
        return self.executor.submit(self._recognize, rgb, pose_map, sides, mp)

    def _recognize(self, rgb, pose_map, sides, mp):
        started = time.perf_counter()
        hands = self.gesture.recognize(rgb, pose_map, sides, mp)
        return hands, (time.perf_counter()-started)*1000

    def change_threads(self, count):
        if self.executor is None:
            return self.gesture.change_threads(count)
        return self.executor.submit(self.gesture.change_threads, count).result()

    def close(self):
        if self.executor is not None:
            try:
                # 同一个线程创建、调用及关闭模型；等待在途调用后才允许新相机启动。
                self.executor.submit(self.gesture.close).result()
            finally:
                self.executor.shutdown(wait=True)
                self.executor = None
