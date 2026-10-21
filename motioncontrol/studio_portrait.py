"""复用体感摄像头的录制人像。无人查看时不运行，不另开采集设备。"""

from __future__ import annotations

import base64
import math
from pathlib import Path
import sys
import threading
import time


MAX_FRAME_AGE = 0.9
FRAME_INTERVAL = 1.0 / 8.0
DEFAULT_CONFIG = {"source": "computer", "background": "original", "face": "original", "mirror": True,
                  "avatar_preset": ""}


def face_geometry(pose, width, height):
    """复用本帧眼鼻耳定位面具；肩膀不参与放大，眼线决定侧倾。"""
    if not isinstance(pose, dict):
        return None

    def point(name):
        item = pose.get(name) or {}
        try:
            x, y, score = float(item["x"]), float(item["y"]), float(item.get("score", 0))
        except (KeyError, ValueError, TypeError):
            return None
        if not all(math.isfinite(v) for v in (x, y, score)) or score < 0.5:
            return None
        if not 0 <= x <= 1 or not 0 <= y <= 1:
            return None
        return x * width, y * height

    nose = point("nose")
    left, right = point("left_eye"), point("right_eye")
    if nose is None or left is None or right is None:
        return None
    eye_left, eye_right = sorted((left, right))
    eye_width = math.dist(eye_left, eye_right)
    if eye_width < 3:
        return None
    ears = point("left_ear"), point("right_ear")
    face_width = max(eye_width * 2.3, math.dist(*ears) * 1.05 if all(ears) else 0)
    angle = math.atan2(eye_right[1] - eye_left[1], eye_right[0] - eye_left[0])
    down = (-math.sin(angle), math.cos(angle))
    center = ((left[0] + right[0]) / 2 + down[0] * face_width * .22,
              (left[1] + right[1]) / 2 + down[1] * face_width * .22)
    # 一组点横向错配时宁可不显示人物，不能把遮盖误贴到胸口。
    if math.dist(nose, center) > face_width * .7:
        return None
    nose_depth = (nose[0] - (left[0] + right[0]) / 2) * down[0] + (nose[1] - (left[1] + right[1]) / 2) * down[1]
    if not eye_width * .08 <= nose_depth <= eye_width * 1.5:
        return None
    return {"center": center, "width": face_width * 1.05, "height": face_width * 1.45,
            "angle": math.degrees(angle), "eyes": (eye_left, eye_right), "nose": nose}


def face_rectangle(pose, width, height):
    geometry = face_geometry(pose, width, height)
    return _face_bounds(geometry, width, height) if geometry else None


def _face_bounds(geometry, width, height):
    cx, cy = geometry["center"]
    angle = math.radians(geometry["angle"])
    half_w = abs(math.cos(angle)) * geometry["width"] / 2 + abs(math.sin(angle)) * geometry["height"] / 2
    half_h = abs(math.sin(angle)) * geometry["width"] / 2 + abs(math.cos(angle)) * geometry["height"] / 2
    return max(0, int(cx - half_w)), max(0, int(cy - half_h)), min(width, math.ceil(cx + half_w)), min(height, math.ceil(cy + half_h))


def _cover_face(image, geometry, avatar, preset=""):
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    bounds = _face_bounds(geometry, width, height)
    transform = None
    if avatar is not None:
        # 内置面具的图像只有脸，锚点以320像素画布标定；自选图片仍按完整图片显示。
        anchors = {"cat": ((116, 166), (204, 166), (160, 195)),
                   "fox": ((114, 151), (206, 151), (160, 220)),
                   "robot": ((118, 153), (202, 153), (160, 185))}.get(preset)
        if anchors:
            source = np.float32(anchors) * np.float32((avatar.shape[1] / 320, avatar.shape[0] / 320))
            transform = cv2.getAffineTransform(source, np.float32((*geometry["eyes"], geometry["nose"])))
        else:
            scale = min(geometry["width"] / avatar.shape[1], geometry["height"] / avatar.shape[0])
            pivot = (avatar.shape[1] / 2, avatar.shape[0] / 2)
            transform = cv2.getRotationMatrix2D(pivot, -geometry["angle"], scale)
            transform[:, 2] += np.array(geometry["center"]) - np.array(pivot)
        corners = np.array(((0, 0, 1), (avatar.shape[1], 0, 1), (0, avatar.shape[0], 1),
                            (avatar.shape[1], avatar.shape[0], 1)), dtype=float) @ transform.T
        bounds = (max(0, min(bounds[0], math.floor(corners[:, 0].min()))),
                  max(0, min(bounds[1], math.floor(corners[:, 1].min()))),
                  min(width, max(bounds[2], math.ceil(corners[:, 0].max()))),
                  min(height, max(bounds[3], math.ceil(corners[:, 1].max()))))
    x0, y0, x1, y1 = bounds
    if x1 <= x0 or y1 <= y0:
        return None
    center = geometry["center"]
    local = (center[0] - x0, center[1] - y0)
    cover = np.zeros((y1 - y0, x1 - x0), np.uint8)
    cv2.ellipse(cover, tuple(round(value) for value in local),
                (max(1, round(geometry["width"] / 2)), max(1, round(geometry["height"] / 2))),
                geometry["angle"], 0, 360, 255, -1)
    roi = image[y0:y1, x0:x1]
    roi[cover > 0] = (48, 48, 48)
    if avatar is not None:
        tile = avatar if avatar.shape[2] == 4 else cv2.cvtColor(avatar, cv2.COLOR_BGR2BGRA)
        transform[:, 2] -= np.array((x0, y0))
        placed = cv2.warpAffine(tile, transform, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
        alpha = placed[:, :, 3:4].astype(np.float32) / 255.0
        roi[:] = (placed[:, :, :3] * alpha + roi * (1 - alpha)).astype(np.uint8)
        cover = np.maximum(cover, placed[:, :, 3])
    return (x0, y0, x1, y1), cover


def decode_avatar(data_url):
    import cv2
    import numpy as np

    if not isinstance(data_url, str) or not data_url.startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")):
        raise ValueError("头像需要 PNG、JPEG 或 WebP 图片")
    try:
        data = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("头像图片数据无效") from exc
    if not data or len(data) > 4 * 1024 * 1024:
        raise ValueError("头像图片请控制在 4 MB 以内")
    avatar = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if avatar is None or avatar.ndim != 3 or avatar.shape[2] not in (3, 4):
        raise ValueError("无法读取头像图片")
    if max(avatar.shape[:2]) > 4096:
        raise ValueError("头像图片尺寸过大")
    return avatar


def builtin_avatar(preset):
    """内置形象以程序自带透明脸型为准，不再使用旧档案中的方形缓存。"""
    import cv2

    if preset not in {"cat", "fox", "robot"}:
        raise ValueError("内置面具无效")
    path = Path(__file__).resolve().parent.parent / "web" / "assets" / f"studio-{preset}.png"
    avatar = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if avatar is None or avatar.ndim != 3 or avatar.shape[2] != 4:
        raise ValueError("内置面具资源缺失，请更新程序后重试")
    return avatar


def render_portrait(frame, pose, mask, config, avatar=None):
    """输入同一帧的原图、点与分割概率，输出带透明通道的画面。"""
    import cv2
    import numpy as np

    if frame is None or frame.ndim != 3 or frame.shape[2] != 3:
        return None, "等待摄像头画面"
    height, width = frame.shape[:2]
    image = frame.copy()
    face_cover = None
    if config["face"] != "original":
        geometry = face_geometry(pose, width, height)
        if geometry is None:
            return None, "人脸跟踪丢失，已隐藏画面"
        face_cover = _cover_face(image, geometry, avatar if config["face"] == "avatar" else None,
                                config.get("avatar_preset", ""))
        if face_cover is None:
            return None, "人脸不在画面中，已隐藏人物"

    if config["background"] == "original":
        alpha = np.full((height, width), 255, dtype=np.uint8)
    else:
        if mask is None:
            return None, "等待人体分割"
        probabilities = np.asarray(mask, dtype=np.float32).squeeze()
        if probabilities.ndim != 2 or not probabilities.size:
            return None, "人体分割数据无效"
        probabilities = cv2.resize(probabilities, (width, height), interpolation=cv2.INTER_LINEAR)
        probabilities = np.nan_to_num(probabilities, nan=0.0, posinf=0.0, neginf=0.0)
        alpha = (np.clip((probabilities - 0.25) / 0.5, 0, 1) * 255).astype(np.uint8)
        if face_cover is not None:
            (x0, y0, x1, y1), cover = face_cover
            alpha[y0:y1, x0:x1] = np.maximum(alpha[y0:y1, x0:x1], cover)
        if config["background"] == "green":
            weight = alpha[:, :, None].astype(np.float32) / 255.0
            image = (image * weight + np.array([0, 255, 0]) * (1 - weight)).astype(np.uint8)
            alpha.fill(255)
        else:
            # 透明像素不保留原背景颜色，避免其他合成工具忽略透明通道时泄露。
            image[alpha == 0] = 0
    output = np.dstack((image, alpha))
    # 镜像属于人物输出，而非识别坐标；录像、合成预览和直播复用同一结果。
    if config.get("mirror", True):
        output = cv2.flip(output, 1)
    return output, None


class StudioPortrait:
    """低频轻任务，摄像头与控制线程保持原来的所有权。"""

    def __init__(self, camera, kernel=None, phone_provider=None, model_path=None):
        self.camera = camera
        self.kernel = kernel
        self.phone_provider = phone_provider
        self.model_path = Path(model_path) if model_path else None
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._config = dict(DEFAULT_CONFIG)
        self._avatar = None
        self._avatar_data_url = ""
        self._enabled = False
        self._revision = 0
        self._frame = None
        self._png = None
        self._frame_at = 0.0
        self._reason = "预览未开启"
        self._phone_detector = None
        self._phone_mp = None
        self._phone_mask_enabled = False
        self._phone_timestamp_ms = 0

    def update(self, config):
        if not isinstance(config, dict):
            raise ValueError("人像设置需要对象")
        allowed = {"source": {"computer", "phone"}, "background": {"original", "green", "transparent"},
                   "face": {"original", "mask", "avatar"}, "avatar_preset": {"", "cat", "fox", "robot"}}
        with self._lock:
            candidate = dict(self._config)
            for name, choices in allowed.items():
                if name in config:
                    if config[name] not in choices:
                        raise ValueError(f"人像设置 {name} 的值无效")
                    candidate[name] = config[name]
            avatar = self._avatar
            if "mirror" in config:
                if type(config["mirror"]) is not bool:
                    raise ValueError("人物镜像必须为开启或关闭")
                candidate["mirror"] = config["mirror"]
            data_url = config.get("avatar_data_url", self._avatar_data_url)
            avatar_changed = (data_url != self._avatar_data_url
                              or candidate["avatar_preset"] != self._config["avatar_preset"])
            if avatar_changed:
                avatar = (builtin_avatar(candidate["avatar_preset"]) if candidate["avatar_preset"] else
                          decode_avatar(data_url) if data_url else None)
            if candidate != self._config or avatar_changed:
                self._config, self._avatar = candidate, avatar
                if avatar_changed:
                    self._avatar_data_url = data_url
                self._revision += 1
                self._clear_locked("等待新设置的画面")
                self._wake.set()
        return self.status()

    def set_enabled(self, enabled):
        with self._lock:
            enabled = bool(enabled)
            if enabled != self._enabled:
                self._enabled = enabled
                self._revision += 1
                self._clear_locked("等待摄像头画面" if enabled else "预览未开启")
            if enabled and self._thread is None and not self._stop.is_set():
                self._thread = threading.Thread(target=self._worker, name="motion-studio-portrait", daemon=True)
                self._thread.start()
            if not enabled:
                self.camera.release_studio()
            self._wake.set()
        return self.status()

    def _clear_locked(self, reason):
        self._frame = self._png = None
        self._frame_at = 0.0
        self._reason = reason

    def status(self):
        with self._lock:
            age = max(0, time.monotonic() - self._frame_at) if self._frame_at else None
            ready = self._enabled and self._png is not None and age is not None and age <= MAX_FRAME_AGE
            return {"enabled": self._enabled, "ready": ready, "revision": self._revision, "config": dict(self._config),
                    "avatar_loaded": self._avatar is not None,
                    "age_ms": round(age * 1000) if age is not None else None,
                    "reason": None if ready else ("摄像头画面已过期" if age is not None and age > MAX_FRAME_AGE else self._reason)}

    def frame_png(self):
        return self.frame_snapshot_png()[0]

    def frame_snapshot_png(self):
        """画面和配置版本在同一把锁下读取，便于客户端拒绝旧响应。"""
        with self._lock:
            if not self._enabled or time.monotonic() - self._frame_at > MAX_FRAME_AGE:
                return None, self._revision
            return self._png, self._revision

    def frame_bgra(self):
        with self._lock:
            if not self._enabled or time.monotonic() - self._frame_at > MAX_FRAME_AGE or self._frame is None:
                return None
            return self._frame.copy()

    def _detect_phone(self, sample, require_mask):
        import cv2

        if self._phone_detector is None or require_mask and not self._phone_mask_enabled:
            self._close_phone_detector()
            model_path = self.model_path or getattr(self.camera, "model_path", None)
            if not model_path or not Path(model_path).is_file():
                raise RuntimeError("现有的人体模型未就绪")
            sys.modules.setdefault("tensorflow", None)
            import mediapipe as mp
            from mediapipe.tasks import python
            from mediapipe.tasks.python import vision

            self._phone_detector = vision.PoseLandmarker.create_from_options(vision.PoseLandmarkerOptions(
                base_options=python.BaseOptions(model_asset_path=str(model_path)),
                running_mode=vision.RunningMode.VIDEO, num_poses=1,
                min_pose_detection_confidence=0.35, min_pose_presence_confidence=0.35,
                min_tracking_confidence=0.35, output_segmentation_masks=bool(require_mask)))
            self._phone_mp, self._phone_mask_enabled = mp, bool(require_mask)
        from .control_kernel import MP_NAMES

        rgb = cv2.cvtColor(sample["frame"], cv2.COLOR_BGR2RGB)
        image = self._phone_mp.Image(image_format=self._phone_mp.ImageFormat.SRGB, data=rgb)
        self._phone_timestamp_ms = max(int(sample["arrived_at"] * 1000), self._phone_timestamp_ms + 1)
        result = self._phone_detector.detect_for_video(image, self._phone_timestamp_ms)
        landmarks = result.pose_landmarks[0] if result.pose_landmarks else []
        pose = {MP_NAMES[index]: {"x": point.x, "y": point.y, "score": getattr(point, "visibility", 0)}
                for index, point in enumerate(landmarks)}
        masks = getattr(result, "segmentation_masks", None)
        return {**sample, "pose": pose, "mask": masks[0].numpy_view().copy() if masks else None}

    def _close_phone_detector(self):
        if self._phone_detector is not None:
            self._phone_detector.close()
            self._phone_detector = None
            self._phone_mask_enabled = False

    def _worker(self):
        import cv2

        last_key = None
        try:
            while not self._stop.is_set():
                with self._lock:
                    enabled, revision = self._enabled, self._revision
                    config, avatar = dict(self._config), self._avatar
                if not enabled:
                    self._close_phone_detector()
                    self._wake.wait(0.25)
                    self._wake.clear()
                    continue
                try:
                    need_mask = config["background"] != "original"
                    need_pose = config["face"] != "original"
                    if config["source"] == "computer":
                        self._close_phone_detector()
                        with self._lock:
                            if not self._enabled or revision != self._revision:
                                continue
                            sample = self.camera.studio_snapshot(require_mask=need_mask, require_pose=need_pose)
                    else:
                        self.camera.release_studio()
                        sample = self.phone_provider() if self.phone_provider else None
                    now = time.monotonic()
                    if not sample or not isinstance(sample.get("arrived_at"), (int, float)):
                        raise RuntimeError("等待摄像头画面")
                    if not math.isfinite(sample["arrived_at"]) or not 0 <= now - sample["arrived_at"] <= MAX_FRAME_AGE:
                        raise RuntimeError("摄像头画面已过期")
                    key = (revision, config["source"], sample.get("sequence"), sample["arrived_at"])
                    if key != last_key:
                        if config["source"] == "phone" and (need_mask or need_pose):
                            sample = self._detect_phone(sample, need_mask)
                        frame, reason = render_portrait(sample["frame"], sample.get("pose"), sample.get("mask"), config, avatar)
                        if frame is None:
                            raise RuntimeError(reason)
                        ok, png = cv2.imencode(".png", frame, [cv2.IMWRITE_PNG_COMPRESSION, 1])
                        if not ok:
                            raise RuntimeError("人像画面编码失败")
                        with self._lock:
                            if self._enabled and revision == self._revision:
                                self._frame, self._png = frame, png.tobytes()
                                self._frame_at = sample["arrived_at"]
                                self._reason = None
                        last_key = key
                except Exception as exc:
                    with self._lock:
                        if revision == self._revision:
                            self._clear_locked(str(exc))
                    last_key = None
                self._wake.wait(FRAME_INTERVAL)
                self._wake.clear()
        finally:
            self._close_phone_detector()

    def close(self):
        self._stop.set()
        self.set_enabled(False)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
