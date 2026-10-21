"""只用合成图片检查透明背景、遮脸失效与旧帧拒绝。"""

import time

import numpy as np
import pytest

from motioncontrol.studio_portrait import MAX_FRAME_AGE, StudioPortrait, face_geometry, render_portrait


def face_pose():
    return {name: {"x": x, "y": y, "score": 0.99} for name, x, y in (
        ("nose", 0.5, 0.35), ("left_eye", 0.45, 0.3), ("right_eye", 0.55, 0.3),
        ("left_ear", 0.4, 0.35), ("right_ear", 0.6, 0.35),
        ("left_shoulder", 0.3, 0.65), ("right_shoulder", 0.7, 0.65))}


class Camera:
    def release_studio(self):
        pass


def test_person_mirror_is_applied_once_after_mask_and_does_not_modify_source():
    frame = np.zeros((20, 40, 3), dtype=np.uint8)
    frame[:, :20] = [12, 30, 200]
    frame[:, 20:] = [220, 80, 10]
    mask = np.zeros((20, 40), dtype=np.float32)
    mask[:, :20] = 1
    original = frame.copy()
    config = {"background": "transparent", "face": "original", "mirror": True}
    mirrored, _ = render_portrait(frame, None, mask, config)
    assert mirrored[10, 30].tolist() == [12, 30, 200, 255]
    assert mirrored[10, 5].tolist() == [0, 0, 0, 0]
    unmirrored, _ = render_portrait(frame, None, mask, {**config, "mirror": False})
    assert unmirrored[10, 5].tolist() == [12, 30, 200, 255]
    assert np.array_equal(frame, original)


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


def test_face_mask_uses_current_face_not_shoulder_size_and_follows_tilt():
    pose = face_pose()
    first = face_geometry(pose, 400, 300)
    pose["left_shoulder"]["x"], pose["right_shoulder"]["x"] = .05, .95
    assert face_geometry(pose, 400, 300) == first
    # 从同一组眼鼻点做平移及旋转，不能固定在胸口或保留上一位置。
    angle = np.deg2rad(25)
    origin = np.array((.5 * 400, .3 * 300))
    rotation = np.array(((np.cos(angle), -np.sin(angle)), (np.sin(angle), np.cos(angle))))
    moved = {}
    for name, point in pose.items():
        x, y = rotation @ (np.array((point["x"] * 400, point["y"] * 300)) - origin) + origin + (40, 20)
        moved[name] = {"x": x / 400, "y": y / 300, "score": .99}
    current = face_geometry(moved, 400, 300)
    assert current["angle"] == pytest.approx(25)
    assert np.linalg.norm(np.array(current["center"]) - np.array(first["center"])) > 35


@pytest.mark.parametrize("angle", [-30, 0, 30])
@pytest.mark.parametrize("mirror", [False, True])
def test_privacy_covers_hair_and_ear_edges_after_head_tilt_and_mirror(angle, mirror):
    width, height = 440, 400
    origin = np.array((200, 90))
    radians = np.deg2rad(angle)
    rotation = np.array(((np.cos(radians), -np.sin(radians)), (np.sin(radians), np.cos(radians))))

    def moved(point):
        return rotation @ (np.array(point) - origin) + origin + (20, 60)

    pose = {}
    for name, point in face_pose().items():
        x, y = moved((point["x"] * 400, point["y"] * 300))
        pose[name] = {"x": x / width, "y": y / height, "score": .99}
    frame = np.full((height, width, 3), 180, np.uint8)
    result, reason = render_portrait(frame, pose, None,
                                    {"background": "original", "face": "mask", "mirror": mirror})
    assert reason is None
    # 发顶、两侧头发和耳缘都要覆盖，不能只盖住眼鼻嘴。
    for point in ((200, 10), (170, 25), (230, 25), (150, 65), (250, 65), (156, 105), (244, 105)):
        x, y = (round(value) for value in moved(point))
        if mirror:
            x = width - 1 - x
        assert result[y, x, :3].tolist() == [48, 48, 48], (angle, mirror, point)
    for point in ((120, 195), (280, 195), (200, 230)):
        x, y = (round(value) for value in moved(point))
        if mirror:
            x = width - 1 - x
        assert result[y, x, :3].tolist() == [180, 180, 180], "不扩大到肩膀和胸口"


def test_builtin_mask_aligns_eye_and_nose_markers_without_cropping_the_picture():
    import cv2

    frame = np.full((300, 400, 3), 180, np.uint8)
    avatar = np.zeros((320, 320, 4), np.uint8)
    cv2.circle(avatar, (116, 166), 8, (0, 0, 255, 255), -1)
    cv2.circle(avatar, (204, 166), 8, (0, 255, 0, 255), -1)
    cv2.circle(avatar, (160, 195), 8, (255, 0, 0, 255), -1)
    pose = face_pose()
    result, reason = render_portrait(frame, pose, None,
                                    {"background": "original", "face": "avatar", "avatar_preset": "cat", "mirror": False}, avatar)
    assert reason is None
    for name, expected in (("left_eye", [0, 0, 255]), ("right_eye", [0, 255, 0]), ("nose", [255, 0, 0])):
        point = pose[name]
        assert result[round(point["y"] * 300), round(point["x"] * 400), :3].tolist() == expected
    assert result[230, 200, :3].tolist() == [180, 180, 180], "面具不覆盖胸口"


def test_single_visible_ear_can_expand_head_cover_without_expanding_avatar():
    pose = face_pose()
    del pose["left_ear"]
    pose["right_ear"]["x"] = .7
    frame = np.full((300, 400, 3), 180, np.uint8)
    result, reason = render_portrait(frame, pose, None,
                                    {"background": "original", "face": "mask", "mirror": False})
    assert reason is None
    assert result[105, 284, :3].tolist() == [48, 48, 48]
    assert result[230, 200, :3].tolist() == [180, 180, 180]


@pytest.mark.parametrize("ear", [(0.5, .59), (0.5, .7), (.9, .35)])
def test_implausible_single_ear_hides_portrait_instead_of_unbounded_cover(ear):
    pose = face_pose()
    del pose["left_ear"]
    pose["right_ear"].update(x=ear[0], y=ear[1])
    frame = np.full((300, 400, 3), 180, np.uint8)
    result, reason = render_portrait(frame, pose, None, {"background": "original", "face": "mask"})
    assert result is None
    assert reason


def test_mask_silhouette_stays_visible_with_transparent_body_and_fail_stop():
    frame = np.full((300, 400, 3), 180, np.uint8)
    pose = face_pose()
    result, _ = render_portrait(frame, pose, np.zeros((300, 400), np.float32),
                                {"background": "transparent", "face": "avatar", "mirror": False},
                                np.zeros((30, 30, 4), np.uint8))
    assert result[105, 200, 3] == 255
    for x, y in ((200, 15), (156, 105), (244, 105)):
        assert result[y, x].tolist() == [48, 48, 48, 255], "透明分割和头像不能漏出发顶与耳朵"
    assert result[230, 200, 3] == 0
    pose["nose"]["score"] = .2
    assert render_portrait(frame, pose, None, {"background": "original", "face": "avatar"})[0] is None


@pytest.mark.parametrize("preset", ["cat", "fox", "robot"])
def test_old_builtin_square_image_uses_current_face_asset_without_changing_config(preset):
    import base64
    import cv2

    old_square = np.full((20, 20, 4), (10, 60, 100, 255), np.uint8)
    _, png = cv2.imencode(".png", old_square)
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    config = {"face": "avatar", "avatar_preset": preset, "avatar_data_url": data_url}
    portrait = StudioPortrait(Camera())
    portrait.update(config)
    assert portrait._avatar.shape == (320, 320, 4)
    assert portrait._avatar[0, 0, 3] == portrait._avatar[319, 160, 3] == 0
    assert portrait._avatar_data_url == config["avatar_data_url"] == data_url
    portrait.close()


def test_custom_avatar_remains_the_uploaded_picture_after_switching_from_builtin():
    import base64
    import cv2

    image = np.full((30, 40, 4), (10, 60, 100, 255), np.uint8)
    _, png = cv2.imencode(".png", image)
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    portrait = StudioPortrait(Camera())
    portrait.update({"face": "avatar", "avatar_preset": "cat", "avatar_data_url": data_url})
    portrait.update({"avatar_preset": ""})
    assert np.array_equal(portrait._avatar, image)
    assert portrait._avatar_data_url == data_url
    portrait.close()
