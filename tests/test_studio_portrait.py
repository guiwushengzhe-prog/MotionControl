"""只用合成图片检查透明背景、遮脸失效与旧帧拒绝。"""

import time

import numpy as np

from motioncontrol.studio_portrait import MAX_FRAME_AGE, StudioPortrait, render_portrait


def face_pose():
    return {name: {"x": x, "y": y, "score": 0.99} for name, x, y in (
        ("nose", 0.5, 0.35), ("left_eye", 0.45, 0.3), ("right_eye", 0.55, 0.3),
        ("left_ear", 0.4, 0.35), ("right_ear", 0.6, 0.35),
        ("left_shoulder", 0.3, 0.65), ("right_shoulder", 0.7, 0.65))}


class Camera:
    def release_studio(self):
        pass


def test_cutout_alpha_and_green_background_use_same_segmentation():
    frame = np.full((64, 64, 3), 180, dtype=np.uint8)
    mask = np.zeros((64, 64), dtype=np.float32)
    mask[16:48, 16:48] = 1
    result, reason = render_portrait(frame, None, mask, {"background": "transparent", "face": "original"})
    assert reason is None
    assert result[0, 0].tolist() == [0, 0, 0, 0]
    assert result[32, 32].tolist() == [180, 180, 180, 255]
    green, _ = render_portrait(frame, None, mask, {"background": "green", "face": "original"})
    assert green[0, 0].tolist() == [0, 255, 0, 255]


def test_privacy_hides_frame_when_tracking_lost_and_transparent_avatar_cannot_reveal_face():
    frame = np.full((100, 100, 3), 180, dtype=np.uint8)
    for face in ("mask", "avatar"):
        assert render_portrait(frame, {}, None, {"background": "original", "face": face})[0] is None
    avatar = np.zeros((4, 4, 4), dtype=np.uint8)
    result, reason = render_portrait(frame, face_pose(), None, {"background": "original", "face": "avatar"}, avatar)
    assert reason is None
    assert result[35, 50, :3].tolist() == [48, 48, 48]


def test_config_change_and_expired_frame_never_return_previous_picture():
    portrait = StudioPortrait(Camera())
    portrait._enabled = True
    portrait._png = b"previous picture"
    portrait._frame = np.ones((4, 4, 4), dtype=np.uint8)
    portrait._frame_at = time.monotonic()
    assert portrait.frame_png() == b"previous picture"
    portrait.update({"face": "mask"})
    assert portrait.frame_png() is None
    portrait._png = b"expired picture"
    portrait._frame_at = time.monotonic() - MAX_FRAME_AGE - 0.1
    assert portrait.frame_png() is None
    assert portrait.frame_bgra() is None
    assert portrait.status()["ready"] is False
    portrait.close()


def test_png_snapshot_and_public_revision_change_together_for_privacy_config():
    portrait = StudioPortrait(Camera())
    portrait._enabled = True
    portrait._png = b"original frame"
    portrait._frame_at = time.monotonic()
    png, revision = portrait.frame_snapshot_png()
    assert png == b"original frame"
    assert revision == portrait.status()["revision"]
    portrait.update({"face": "mask"})
    png, next_revision = portrait.frame_snapshot_png()
    assert png is None
    assert next_revision > revision
    assert next_revision == portrait.status()["revision"]
    assert portrait.frame_png() is None
    portrait.close()
