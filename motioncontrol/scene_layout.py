from __future__ import annotations

import copy
import json
import math
import time
from pathlib import Path
from typing import Any

from motioncontrol.user_paths import user_path


SCENE_VERSION = 2
# Persisted identifier kept for v2 files already on disk. Runtime now exposes
# six canonical regions; changing this string would invalidate old profiles.
SCENE_LAYOUT_PROFILE = "seven-zone-body-recommended-v1"
CANONICAL_ZONE_IDS = ("leftHand", "rightHand", "leftFoot", "rightFoot", "headJump", "lookGate")
LEGACY_ZONE_ALIASES = {
    "leftHandUpper": "leftHand",
    "leftHandLower": "leftHand",
    "rightHandUpper": "rightHand",
    "rightHandLower": "rightHand",
}
DEFAULT_MATCH_THRESHOLDS = {
    "min_matches": 30,
    "min_inlier_ratio": 0.45,
    "max_reprojection_error_px": 4.0,
    "min_scale": 0.80,
    "max_scale": 1.25,
    "max_rotation_deg": 15.0,
    "max_body_anchor_error_norm": 0.12,
}


def _score(point: dict | None) -> float:
    if not isinstance(point, dict):
        return 0.0
    try:
        return float(point.get("score", point.get("visibility", point.get("presence", 0.0))) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _pose_subset(pose: dict[str, dict] | None) -> dict[str, dict]:
    if not isinstance(pose, dict):
        return {}
    names = (
        "nose", "left_eye", "right_eye", "left_ear", "right_ear", "mouth_left", "mouth_right",
        "left_shoulder", "right_shoulder", "left_wrist", "right_wrist",
        "left_hip", "right_hip", "left_ankle", "right_ankle",
        "left_heel", "right_heel", "left_foot_index", "right_foot_index",
    )
    result: dict[str, dict] = {}
    for name in names:
        point = pose.get(name)
        if isinstance(point, dict):
            result[name] = {
                "x": _finite(point.get("x")), "y": _finite(point.get("y")),
                "score": _score(point),
            }
    return result


def _point_in_frame(point: dict | None) -> bool:
    if not isinstance(point, dict):
        return False
    try:
        x, y = float(point.get("x")), float(point.get("y"))
    except (TypeError, ValueError):
        return False
    return math.isfinite(x) and math.isfinite(y) and -0.03 <= x <= 1.03 and -0.03 <= y <= 1.03


def _best_point(pose: dict[str, dict] | None, names: tuple[str, ...], min_score: float = 0.03) -> dict | None:
    candidates = []
    for name in names:
        point = (pose or {}).get(name)
        if _point_in_frame(point):
            candidates.append((_score(point), point))
    if not candidates:
        return None
    score, point = max(candidates, key=lambda item: item[0])
    if score < min_score:
        return None
    return dict(point)


def _resolve_placement_pose(pose: dict[str, dict]) -> tuple[dict[str, dict], list[str]]:
    """Resolve robust anchors for first-time six-zone placement.

    Ear/ankle visibility often fluctuates even while the landmarks are visibly on
    screen. First capture only needs a head anchor and both shoulders. Missing
    hips/feet are estimated from a generic shoulder-to-body proportion so the
    user does not have to fit their whole body in the first setup frame.
    """
    pose = pose if isinstance(pose, dict) else {}
    resolved = {name: dict(point) for name, point in pose.items() if isinstance(point, dict)}
    fallback: list[str] = []
    ls, rs = pose["left_shoulder"], pose["right_shoulder"]
    shoulder_width = max(0.06, math.hypot(ls["x"] - rs["x"], ls["y"] - rs["y"]))
    shoulder_mid_x = (ls["x"] + rs["x"]) / 2.0
    left_dir = -1.0 if ls["x"] <= rs["x"] else 1.0
    right_dir = -left_dir

    # A visible hip pair is preferred, but it is not a setup prerequisite. Use
    # the shoulder width as a scale ruler for a conservative lower-body estimate
    # when the camera framing contains only the upper body.
    lh, rh = pose.get("left_hip"), pose.get("right_hip")
    if (
        isinstance(lh, dict) and isinstance(rh, dict)
        and _score(lh) >= 0.03 and _score(rh) >= 0.03
        and _point_in_frame(lh) and _point_in_frame(rh)
    ):
        hip_mid = {"x": (lh["x"] + rh["x"]) / 2.0, "y": (lh["y"] + rh["y"]) / 2.0}
        torso = max(0.08, math.hypot(shoulder_mid_x - hip_mid["x"], ((ls["y"] + rs["y"]) / 2.0) - hip_mid["y"]))
    else:
        hip_mid = {
            "x": shoulder_mid_x,
            "y": _clamp(((ls["y"] + rs["y"]) / 2.0) + shoulder_width * 1.55, 0.0, 1.0),
        }
        torso = max(0.08, shoulder_width * 1.55)
        hip_half = shoulder_width * 0.30
        lh = {"x": _clamp(hip_mid["x"] + left_dir * hip_half, 0.0, 1.0), "y": hip_mid["y"], "score": 0.01}
        rh = {"x": _clamp(hip_mid["x"] + right_dir * hip_half, 0.0, 1.0), "y": hip_mid["y"], "score": 0.01}
        fallback.append("hips")
    resolved["left_hip"], resolved["right_hip"] = lh, rh
    torso = max(0.08, float(torso))

    # Keep a canonical nose anchor even when MediaPipe supplied only eyes/ears;
    # this lets the head-jump target and ear fallbacks share one stable center.
    nose = _best_point(pose, ("nose", "left_eye", "right_eye", "left_ear", "right_ear"), 0.03)
    if nose is None:
        nose = {
            "x": _clamp(shoulder_mid_x, 0.0, 1.0),
            "y": _clamp(((ls["y"] + rs["y"]) / 2.0) - shoulder_width * 0.72, 0.0, 1.0),
            "score": 0.01,
        }
        fallback.append("nose")
    resolved["nose"] = nose

    for side, direction in (("left", left_dir), ("right", right_dir)):
        ear = _best_point(pose, (f"{side}_ear",), 0.06)
        if ear is None:
            eye = _best_point(pose, (f"{side}_eye",), 0.05)
            if eye is not None:
                ear = {**eye, "x": _clamp(eye["x"] + direction * shoulder_width * 0.16, 0.0, 1.0), "score": max(_score(eye), 0.05)}
            else:
                ear = {"x": _clamp(nose["x"] + direction * shoulder_width * 0.24, 0.0, 1.0), "y": nose["y"], "score": 0.01}
            fallback.append(f"{side}_ear")
        resolved[f"{side}_ear"] = ear

        foot = _best_point(pose, (f"{side}_ankle", f"{side}_heel", f"{side}_foot_index"), 0.03)
        if foot is None:
            # If the lower-limb landmark scores collapse for one frame, estimate
            # the standing foot from the corresponding hip and body scale. This
            # is only an initial recommendation; the user can still drag the zone.
            hip = resolved[f"{side}_hip"]
            foot = {
                "x": _clamp(hip["x"] + direction * shoulder_width * 0.04, 0.0, 1.0),
                "y": _clamp(hip["y"] + torso * 0.95, 0.0, 1.0),
                "score": 0.01,
            }
            fallback.append(f"{side}_foot")
        resolved[f"{side}_ankle"] = foot

    return resolved, fallback


def _pose_ready(pose: dict[str, dict] | None, *, require_placement: bool = False) -> tuple[bool, str]:
    """Return whether there is enough upper-body geometry to author six circles.

    Both shoulders and one head anchor are mandatory. Hips, wrists, ears and
    feet are deliberately *not* hard gates: they fluctuate more in MediaPipe,
    and missing lower-body points are estimated for the editable initial layout.
    """
    del require_placement
    core = ("left_shoulder", "right_shoulder")
    missing_core = [
        name for name in core
        if not pose or _score(pose.get(name)) < 0.24 or not _point_in_frame(pose.get(name))
    ]
    if missing_core:
        labels = {
            "left_shoulder": "左肩",
            "right_shoulder": "右肩",
        }
        missing_text = "、".join(labels.get(name, name) for name in missing_core)
        return False, f"还缺少稳定骨架：{missing_text}；请让双肩进入画面（不要求全身入镜）"
    face = _best_point(pose, ("nose", "left_eye", "right_eye", "left_ear", "right_ear"), 0.03)
    if face is None:
        return False, "还缺少头部定位点；请让脸部或耳朵进入画面（不要求髋部和脚入镜）"
    ls, rs = pose["left_shoulder"], pose["right_shoulder"]
    shoulder_width = math.hypot(ls["x"] - rs["x"], ls["y"] - rs["y"])
    if shoulder_width < 0.055:
        return False, "人物在画面中过小，请稍微靠近摄像头；只需保证头和双肩清楚可见"
    return True, "ok"


def _circle_from_rect(rect: dict | None, fallback: tuple[float, float, float]) -> dict:
    if not isinstance(rect, dict):
        cx, cy, radius = fallback
    else:
        x1, x2 = _finite(rect.get("x1")), _finite(rect.get("x2"))
        y1, y2 = _finite(rect.get("y1")), _finite(rect.get("y2"))
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        radius = max(0.025, min(0.16, ((x2 - x1) + (y2 - y1)) / 4.0))
    return {"shape": "circle", "cx": _clamp(cx, 0.0, 1.0), "cy": _clamp(cy, 0.0, 1.0), "r": _clamp(radius, 0.025, 0.20)}


def _enclose_circles(circles: list[dict]) -> dict | None:
    """Build one editable circle containing a pair of legacy hand circles."""
    valid = []
    for item in circles:
        if not isinstance(item, dict):
            continue
        try:
            cx, cy, radius = float(item["cx"]), float(item["cy"]), float(item["r"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (cx, cy, radius)) or radius <= 0.0:
            continue
        valid.append((cx, cy, radius))
    if not valid:
        return None
    if len(valid) == 1:
        cx, cy, radius = valid[0]
    else:
        x1 = min(cx - radius for cx, _cy, radius in valid)
        x2 = max(cx + radius for cx, _cy, radius in valid)
        y1 = min(cy - radius for _cx, cy, radius in valid)
        y2 = max(cy + radius for _cx, cy, radius in valid)
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        radius = max(math.hypot(cx - px, cy - py) + pr for px, py, pr in valid)
    radius = _clamp(radius, 0.025, 0.30)
    return {"shape": "circle", "cx": _clamp(cx, radius, 1.0 - radius), "cy": _clamp(cy, radius, 1.0 - radius), "r": radius}


def _canonicalize_zones(zones: dict | None) -> dict:
    """Add merged hand/head-jump zones while retaining legacy coordinates."""
    source = zones if isinstance(zones, dict) else {}
    result = {name: copy.deepcopy(value) for name, value in source.items() if isinstance(value, dict)}
    for canonical, aliases in (
        ("leftHand", ("leftHandUpper", "leftHandLower")),
        ("rightHand", ("rightHandUpper", "rightHandLower")),
    ):
        if not isinstance(result.get(canonical), dict):
            merged = _enclose_circles([source.get(alias) for alias in aliases])
            if merged:
                result[canonical] = merged
    if "headJump" not in result:
        merged = _enclose_circles([source.get("leftHandUpper"), source.get("rightHandUpper")])
        if merged:
            result["headJump"] = {
                "shape": "circle",
                "cx": merged["cx"],
                "cy": _clamp(merged["cy"] - merged["r"] * 1.65, merged["r"], 1.0 - merged["r"]),
                "r": _clamp(merged["r"] * 0.90, 0.04, 0.12),
            }
    return result


class SceneLayoutManager:
    """Persistent reference scene + explicit/manual session adaptation.

    Reference zones are never overwritten by rematching. A rematch produces a
    session transform from the original reference image to the current camera
    image, preventing day-to-day cumulative drift.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.config_dir = self.root / "config"
        # Never in the program folder, and never synced anywhere: this is a
        # homography onto one captured frame from one camera in one
        # position, plus the reference photo it maps onto.  Carried to
        # another machine it puts the six zones silently in the wrong place.
        self.layout_path = user_path("scene_layout")
        self.reference_path = user_path("scene_reference")
        self.reference: dict | None = None
        self.session: dict | None = None
        self.last_result: dict = {"ok": False, "state": "not_configured", "message": "尚未记录参考场景"}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.layout_path.read_text(encoding="utf-8"))
            if int(data.get("version", 0)) != SCENE_VERSION:
                return
            if not self.reference_path.is_file():
                return
            data = copy.deepcopy(data)
            data["zones"] = _canonicalize_zones(data.get("zones"))
            self.reference = data
            # On every program start, use the original reference coordinates.
            # Nothing adapts until the user explicitly requests a rematch.
            self.session = copy.deepcopy(data)
            self.session["adapted"] = False
            self.last_result = {
                "ok": True, "state": "reference_loaded",
                "message": "已载入参考场景；本次未自动重新匹配",
            }
        except Exception:
            self.reference = None
            self.session = None

    def _save(self) -> None:
        if not self.reference:
            return
        self.config_dir.mkdir(parents=True, exist_ok=True)
        temp = self.layout_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(self.reference, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.layout_path)

    @staticmethod
    def _initial_layout(pose: dict[str, dict], dynamic_rects: dict[str, dict] | None) -> tuple[dict, dict]:
        """Generate the merged hand/foot/head-jump circles from first pose.

        The legacy dynamic rectangles are intentionally ignored.  The body is
        used once as an ergonomic ruler; after capture the returned circles are
        camera-space coordinates and never follow the player.

        New runtime zones use one broad hand circle per side.  The four old
        hand circles remain as coordinate aliases in the saved document so
        older profile editors can still read their original positions.
          leftHand      -> X  -> left wrist, broad side area
          rightHand     -> B  -> right wrist, broad side area
          leftFoot       -> LB -> left foot kick target
          rightFoot      -> RB -> right foot kick target
          headJump      -> A  -> nose entering the space above the head
          lookGate       -> no button; left wrist near the left side of chin
        """
        del dynamic_rects  # compatibility parameter; never seed fixed zones from moving rectangles

        pose, _fallback = _resolve_placement_pose(pose)
        le, re = pose["left_ear"], pose["right_ear"]
        la, ra = pose["left_ankle"], pose["right_ankle"]
        ls, rs = pose["left_shoulder"], pose["right_shoulder"]
        lh, rh = pose["left_hip"], pose["right_hip"]

        shoulder = {"x": (ls["x"] + rs["x"]) / 2.0, "y": (ls["y"] + rs["y"]) / 2.0}
        hip = {"x": (lh["x"] + rh["x"]) / 2.0, "y": (lh["y"] + rh["y"]) / 2.0}
        ear_mid = {"x": (le["x"] + re["x"]) / 2.0, "y": (le["y"] + re["y"]) / 2.0}
        shoulder_width = max(0.06, math.hypot(ls["x"] - rs["x"], ls["y"] - rs["y"]))
        head_width = max(0.035, math.hypot(le["x"] - re["x"], le["y"] - re["y"]), shoulder_width * 0.32)
        torso = max(0.08, math.hypot(shoulder["x"] - hip["x"], shoulder["y"] - hip["y"]))
        head_to_shoulder = max(0.055, abs(shoulder["y"] - ear_mid["y"]), torso * 0.34)

        # Do not assume anatomical left is image-left.  Move each named side
        # away from the head/legs using the actual first-frame ordering.
        left_dir = -1.0 if le["x"] <= re["x"] else 1.0
        right_dir = -left_dir
        left_foot_dir = -1.0 if la["x"] <= ra["x"] else 1.0
        right_foot_dir = -left_foot_dir

        # Right wrist is useful only for the initial hand-controlled vertical
        # center. It must never block six-zone creation. If it is absent or
        # confidence dips on the capture frame, place a conservative neutral
        # hand center from the shoulder/torso geometry; the user can still drag
        # or recalibrate it later.
        rw = _best_point(pose, ("right_wrist",), 0.03)
        if rw is None:
            rw = {
                "x": _clamp(rs["x"] + right_dir * shoulder_width * 0.18, 0.0, 1.0),
                "y": _clamp(rs["y"] + torso * 0.72, 0.0, 1.0),
                "score": 0.01,
            }

        hand_r = _clamp(shoulder_width * 0.24, 0.040, 0.080)
        foot_r = _clamp(shoulder_width * 0.30, 0.050, 0.095)
        gate_r = _clamp(shoulder_width * 0.22, 0.040, 0.072)
        jump_r = _clamp(shoulder_width * 0.22, 0.045, 0.075)

        # 1-2: one broad circle per hand containing the old ear/head targets.
        ear_offset = max(head_width * 0.85, shoulder_width * 0.30)
        left_ear_zone = (le["x"] + left_dir * ear_offset, le["y"] + torso * 0.01)
        right_ear_zone = (re["x"] + right_dir * ear_offset, re["y"] + torso * 0.01)

        # Preserve the former two head-side targets as aliases, then use their
        # enclosing circle as the single side-hand trigger region.
        head_top_y = ear_mid["y"] - 0.52 * head_to_shoulder
        head_zone_y = head_top_y - 0.34 * head_to_shoulder
        head_side_offset = max(head_width * 0.72, shoulder_width * 0.32)
        left_head_zone = (ear_mid["x"] + left_dir * head_side_offset, head_zone_y)
        right_head_zone = (ear_mid["x"] + right_dir * head_side_offset, head_zone_y)

        # 5-6: outward/upward kick targets.  They are deliberately not placed
        # directly on the resting feet, otherwise standing still would trigger.
        leg_span = max(0.04, abs(la["x"] - ra["x"]))
        kick_offset = max(shoulder_width * 0.48, leg_span * 0.55)
        kick_lift = max(torso * 0.36, 0.040)
        left_foot_zone = (la["x"] + left_foot_dir * kick_offset, la["y"] - kick_lift)
        right_foot_zone = (ra["x"] + right_foot_dir * kick_offset, ra["y"] - kick_lift)

        # The nose must rise a modest, bounded distance into this target; the
        # standing nose remains below it, while a small jump can enter it.
        head_jump_zone = (pose["nose"]["x"], pose["nose"]["y"] - 0.30 * torso)

        # 7: a compact left-of-chin gate. MediaPipe Pose has no chin landmark,
        # so estimate chin from the ear line toward the shoulder line.
        chin_x = ear_mid["x"]
        chin_y = ear_mid["y"] + 0.38 * head_to_shoulder
        gate = (chin_x + left_dir * max(head_width * 0.52, shoulder_width * 0.16), chin_y)

        def circle(center: tuple[float, float], radius: float) -> dict:
            return {
                "shape": "circle",
                "cx": _clamp(center[0], radius, 1.0 - radius),
                "cy": _clamp(center[1], radius, 1.0 - radius),
                "r": radius,
            }

        left_head_circle = circle(left_head_zone, hand_r)
        left_ear_circle = circle(left_ear_zone, hand_r)
        right_head_circle = circle(right_head_zone, hand_r)
        right_ear_circle = circle(right_ear_zone, hand_r)
        zones = {
            "leftHand": _enclose_circles([left_head_circle, left_ear_circle]),
            "rightHand": _enclose_circles([right_head_circle, right_ear_circle]),
            "leftFoot": circle(left_foot_zone, foot_r),
            "rightFoot": circle(right_foot_zone, foot_r),
            "headJump": circle(head_jump_zone, jump_r),
            "lookGate": circle(gate, gate_r),
            # Legacy coordinates are retained as read-only aliases. Runtime
            # evaluation uses only leftHand/rightHand, so there is one visual
            # and one trigger region per hand.
            "leftHandUpper": left_head_circle,
            "leftHandLower": left_ear_circle,
            "rightHandUpper": right_head_circle,
            "rightHandLower": right_ear_circle,
        }
        vertical = {
            "enabled": False,
            "gate_zone_id": "lookGate",
            "point": "right_wrist",
            "source": "hand",
            "verticalLookSource": "hand",
            "exclusive_axes": False,
            "body_motion_guard": False,
            "center_x": _clamp(rw["x"], 0.0, 1.0),
            "center_y": _clamp(rw["y"], 0.0, 1.0),
            "range_y": _clamp(torso * 0.75, 0.10, 0.28),
            "deadzone": 0.10,
        }
        return zones, vertical

    def capture_reference(self, frame, pose: dict[str, dict] | None, dynamic_rects: dict[str, dict] | None = None,
                          *, vertical_preferences: dict | None = None) -> dict:
        ready, reason = _pose_ready(pose, require_placement=False)
        if not ready:
            raise ValueError(reason)
        if frame is None or getattr(frame, "size", 0) <= 0:
            raise ValueError("当前没有可保存的摄像头画面")
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("场景截图需要 OpenCV（opencv-python）") from exc
        height, width = frame.shape[:2]
        placement_pose, fallback = _resolve_placement_pose(pose)
        if _best_point(pose or {}, ("right_wrist",), 0.03) is None:
            fallback.append("right_wrist")
        zones, vertical = self._initial_layout(placement_pose, dynamic_rects)
        # 重新定位只更新位置，不重新开启上下绿区或身体保护。
        preferences = vertical_preferences if vertical_preferences is not None else (self.reference or {}).get("vertical_look", {})
        for key in ("enabled", "source", "verticalLookSource", "exclusive_axes", "body_motion_guard"):
            if key in preferences:
                vertical[key] = preferences[key]
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(self.reference_path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 94]):
            raise RuntimeError("参考截图保存失败")
        data = {
            "version": SCENE_VERSION,
            "layout_profile": SCENE_LAYOUT_PROFILE,
            "created_at_unix": round(time.time(), 3),
            "reference_image": self.reference_path.name,
            "width": int(width), "height": int(height),
            "pose": _pose_subset(pose),
            "placement_fallback": list(fallback),
            "zones": zones,
            "vertical_look": vertical,
            "match_thresholds": copy.deepcopy(DEFAULT_MATCH_THRESHOLDS),
        }
        self.reference = data
        self.session = copy.deepcopy(data)
        self.session["adapted"] = False
        self._save()
        message = "参考场景已记录，已按人体自动生成 6 个固定触发区"
        if fallback:
            message += "；部分腕/耳/脚关键点本帧不稳定，已自动估算初始位置，请按需要拖动微调"
        self.last_result = {
            "ok": True, "state": "reference_captured", "message": message,
            "width": width, "height": height, "placement_fallback": list(fallback),
        }
        return self.status()

    def update_reference_layout(self, body: dict) -> dict:
        if not self.reference:
            raise ValueError("请先记录参考场景")
        zones = body.get("zones")
        if not isinstance(zones, dict):
            raise ValueError("zones 必须是对象")
        current = copy.deepcopy(self.reference.get("zones") or {})
        allowed = set(CANONICAL_ZONE_IDS) | set(LEGACY_ZONE_ALIASES)
        for name, raw in zones.items():
            if name not in allowed or not isinstance(raw, dict):
                continue
            normalized = {
                "shape": "circle",
                "cx": _clamp(raw.get("cx", current.get(name, {}).get("cx", 0.5)), 0.0, 1.0),
                "cy": _clamp(raw.get("cy", current.get(name, {}).get("cy", 0.5)), 0.0, 1.0),
                "r": _clamp(raw.get("r", current.get(name, {}).get("r", 0.07)), 0.025, 0.20),
            }
            target = LEGACY_ZONE_ALIASES.get(name, name)
            current[target] = normalized
            # Keep a legacy alias in the saved document when an old client
            # edits it, but make the canonical merged circle authoritative.
            if name in LEGACY_ZONE_ALIASES:
                current[name] = copy.deepcopy(normalized)
        current = _canonicalize_zones(current)
        self.reference["zones"] = current
        vertical = body.get("vertical_look")
        if isinstance(vertical, dict):
            old = copy.deepcopy(self.reference.get("vertical_look") or {})
            old.update({
                "enabled": bool(vertical.get("enabled", old.get("enabled", True))),
                "source": "head" if str(vertical.get("source", vertical.get("verticalLookSource", old.get("source", "hand")))).lower() in {"head", "头部"} else "hand",
                "verticalLookSource": "head" if str(vertical.get("verticalLookSource", vertical.get("source", old.get("source", "hand")))).lower() in {"head", "头部"} else "hand",
                "exclusive_axes": bool(vertical.get("exclusive_axes", old.get("exclusive_axes", False))),
                "body_motion_guard": bool(vertical.get("body_motion_guard", old.get("body_motion_guard", False))),
                "center_x": _clamp(vertical.get("center_x", old.get("center_x", 0.5)), 0.0, 1.0),
                "center_y": _clamp(vertical.get("center_y", old.get("center_y", 0.5)), 0.0, 1.0),
                "range_y": _clamp(vertical.get("range_y", old.get("range_y", 0.18)), 0.06, 0.40),
                "deadzone": _clamp(vertical.get("deadzone", old.get("deadzone", 0.10)), 0.0, 0.35),
            })
            old["gate_zone_id"] = "lookGate"
            old["point"] = "right_wrist"
            self.reference["vertical_look"] = old
        self._save()
        # Editing always resets the current session to the newly-authored
        # reference coordinates. The user may explicitly rematch afterwards.
        self.session = copy.deepcopy(self.reference)
        self.session["adapted"] = False
        self.last_result = {"ok": True, "state": "layout_saved", "message": "固定区域布局已保存"}
        return self.status()

    @staticmethod
    def _body_mask(shape, pose: dict[str, dict] | None):
        import numpy as np
        mask = np.full(shape[:2], 255, dtype=np.uint8)
        points = [p for p in (pose or {}).values() if isinstance(p, dict) and _score(p) >= 0.35]
        if not points:
            return mask
        h, w = shape[:2]
        xs = [_clamp(p["x"], 0.0, 1.0) for p in points]
        ys = [_clamp(p["y"], 0.0, 1.0) for p in points]
        x1, x2 = int(max(0, (min(xs) - 0.08) * w)), int(min(w, (max(xs) + 0.08) * w))
        y1, y2 = int(max(0, (min(ys) - 0.08) * h)), int(min(h, (max(ys) + 0.08) * h))
        mask[y1:y2, x1:x2] = 0
        return mask

    @staticmethod
    def _transform_points(matrix, points):
        import cv2
        import numpy as np
        arr = np.asarray(points, dtype=np.float32).reshape(-1, 1, 2)
        return cv2.perspectiveTransform(arr, matrix).reshape(-1, 2)

    def rematch(self, frame, current_pose: dict[str, dict] | None) -> dict:
        if not self.reference or not self.reference_path.is_file():
            raise ValueError("尚未记录参考场景，请先说“截图”或点击记录参考场景")
        ready, reason = _pose_ready(current_pose)
        if not ready:
            raise ValueError(reason)
        if frame is None or getattr(frame, "size", 0) <= 0:
            raise ValueError("当前没有摄像头画面")
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("场景重新匹配需要 OpenCV 与 NumPy") from exc
        reference_frame = cv2.imread(str(self.reference_path), cv2.IMREAD_COLOR)
        if reference_frame is None:
            raise RuntimeError("参考截图无法读取")
        ref_pose = self.reference.get("pose") or {}
        ref_gray = cv2.cvtColor(reference_frame, cv2.COLOR_BGR2GRAY)
        cur_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        orb = cv2.ORB_create(nfeatures=2200, scaleFactor=1.2, nlevels=8, fastThreshold=12)
        kp1, des1 = orb.detectAndCompute(ref_gray, self._body_mask(reference_frame.shape, ref_pose))
        kp2, des2 = orb.detectAndCompute(cur_gray, self._body_mask(frame.shape, current_pose))
        if des1 is None or des2 is None or len(kp1) < 30 or len(kp2) < 30:
            raise ValueError("背景特征太少，无法可靠重新匹配；请让墙面/家具等参考背景进入画面")
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        pairs = matcher.knnMatch(des1, des2, k=2)
        good = [a for a, b in pairs if a.distance < 0.75 * b.distance]
        thresholds = {**DEFAULT_MATCH_THRESHOLDS, **(self.reference.get("match_thresholds") or {})}
        if len(good) < int(thresholds["min_matches"]):
            raise ValueError(f"背景匹配点不足：{len(good)} / {int(thresholds['min_matches'])}")
        src = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        matrix, mask = cv2.findHomography(src, dst, cv2.RANSAC, float(thresholds["max_reprojection_error_px"]))
        if matrix is None or mask is None:
            raise ValueError("无法计算可靠的场景变换")
        inliers = mask.ravel().astype(bool)
        inlier_count = int(inliers.sum())
        inlier_ratio = inlier_count / max(1, len(good))
        projected = cv2.perspectiveTransform(src[inliers].reshape(-1, 1, 2), matrix).reshape(-1, 2)
        target = dst[inliers].reshape(-1, 2)
        errors = np.linalg.norm(projected - target, axis=1)
        reprojection = float(np.median(errors)) if len(errors) else float("inf")
        a, b, c, d = float(matrix[0, 0]), float(matrix[0, 1]), float(matrix[1, 0]), float(matrix[1, 1])
        scale = (math.hypot(a, c) + math.hypot(b, d)) / 2.0
        rotation = math.degrees(math.atan2(c, a))
        ref_h, ref_w = reference_frame.shape[:2]
        cur_h, cur_w = frame.shape[:2]
        anchor_errors = []
        for name in ("nose", "left_shoulder", "right_shoulder", "left_hip", "right_hip"):
            rp, cp = ref_pose.get(name), current_pose.get(name)
            if _score(rp) < 0.42 or _score(cp) < 0.42:
                continue
            transformed = self._transform_points(matrix, [[rp["x"] * ref_w, rp["y"] * ref_h]])[0]
            anchor_errors.append(math.hypot(transformed[0] - cp["x"] * cur_w, transformed[1] - cp["y"] * cur_h))
        diag = math.hypot(cur_w, cur_h)
        body_error_norm = (float(np.median(anchor_errors)) / diag) if anchor_errors else 1.0
        reasons = []
        if inlier_ratio < float(thresholds["min_inlier_ratio"]): reasons.append("内点比例过低")
        if reprojection > float(thresholds["max_reprojection_error_px"]): reasons.append("重投影误差过大")
        if not (float(thresholds["min_scale"]) <= scale <= float(thresholds["max_scale"])): reasons.append("画面尺度变化过大")
        if abs(rotation) > float(thresholds["max_rotation_deg"]): reasons.append("摄像头旋转变化过大")
        if body_error_norm > float(thresholds["max_body_anchor_error_norm"]): reasons.append("玩家站位与参考位置差异过大")
        metrics = {
            "matches": len(good), "inliers": inlier_count, "inlier_ratio": round(inlier_ratio, 3),
            "reprojection_error_px": round(reprojection, 2), "scale": round(scale, 4),
            "rotation_deg": round(rotation, 2), "body_anchor_error_norm": round(body_error_norm, 4),
        }
        if reasons:
            self.last_result = {"ok": False, "state": "match_rejected", "message": "；".join(reasons), **metrics}
            return self.status()
        session = copy.deepcopy(self.reference)
        transformed_zones = {}
        for name, zone in (self.reference.get("zones") or {}).items():
            cx, cy, radius = float(zone["cx"]), float(zone["cy"]), float(zone["r"])
            pts = self._transform_points(matrix, [
                [cx * ref_w, cy * ref_h], [(cx + radius) * ref_w, cy * ref_h], [cx * ref_w, (cy + radius) * ref_h],
            ])
            center = pts[0]
            rx = math.hypot(*(pts[1] - center)) / cur_w
            ry = math.hypot(*(pts[2] - center)) / cur_h
            transformed_zones[name] = {
                "shape": "circle", "cx": _clamp(center[0] / cur_w, 0.0, 1.0),
                "cy": _clamp(center[1] / cur_h, 0.0, 1.0), "r": _clamp((rx + ry) / 2.0, 0.02, 0.24),
            }
        session["zones"] = transformed_zones
        vertical = copy.deepcopy(self.reference.get("vertical_look") or {})
        center = self._transform_points(matrix, [[float(vertical.get("center_x", 0.5)) * ref_w, float(vertical.get("center_y", 0.5)) * ref_h]])[0]
        vertical["center_x"], vertical["center_y"] = _clamp(center[0] / cur_w, 0.0, 1.0), _clamp(center[1] / cur_h, 0.0, 1.0)
        vertical["range_y"] = _clamp(float(vertical.get("range_y", 0.18)) * scale, 0.05, 0.45)
        session["vertical_look"] = vertical
        session["adapted"] = True
        session["match_metrics"] = metrics
        session["matched_at_unix"] = round(time.time(), 3)
        self.session = session
        confidence = _clamp((inlier_ratio - 0.35) / 0.45, 0.0, 1.0) * _clamp(1.0 - reprojection / 10.0, 0.0, 1.0) * _clamp(1.0 - body_error_norm / 0.18, 0.0, 1.0)
        self.last_result = {
            "ok": True, "state": "matched", "message": "场景重新匹配成功，本次区域已锁定",
            "confidence": round(confidence, 3), **metrics,
        }
        return self.status()

    def status(self) -> dict:
        active = self.session or self.reference or {}
        return {
            "configured": bool(self.reference),
            "reference_image_available": bool(self.reference and self.reference_path.is_file()),
            "reference_image_url": "/api/scene/reference.jpg" if self.reference and self.reference_path.is_file() else None,
            "zones": copy.deepcopy(active.get("zones") or {}),
            "vertical_look": copy.deepcopy(active.get("vertical_look") or {}),
            "adapted": bool(active.get("adapted", False)),
            "last_result": copy.deepcopy(self.last_result),
            "reference_width": self.reference.get("width") if self.reference else None,
            "reference_height": self.reference.get("height") if self.reference else None,
        }
