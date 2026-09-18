"""Where each piece of user data lives, as a pure function of a base directory.

Resolving %LOCALAPPDATA% is deliberately *not* here.  The cloud service imports
this package on Linux, and a helper that reached for a Windows environment
variable would import fine and still be wrong: it would make the shared rules
depend on where one particular desktop happens to keep files.  This module
describes the *shape* of the layout; ``user_paths.py`` on the desktop side
resolves the base directory and hands it in.  tests/test_shared_boundary.py
enforces the split.

The layout is flat because two of these files -- head_profile.json and
camera_backend.json -- already live directly under %LOCALAPPDATA%\\MotionControl
and work.  Reorganising them into subdirectories would buy tidiness at the cost
of migrating real user calibration, so the rest joins them instead.
"""

from __future__ import annotations

from pathlib import Path

# key -> filename under the user data directory.
USER_DATA_FILES = {
    "profile_selection": "game_profile_selection.json",
    "motion_mappings": "motion_mappings.json",
    "voice_mappings": "voice_mappings.json",
    "scene_layout": "scene_layout.json",
    "scene_reference": "scene_reference.jpg",
    "head_profile": "head_profile.json",
    "camera_backend": "camera_backend.json",
    "paired_devices": "paired_devices.json",
    "require_paired_devices": "require_paired_devices",
    # Which cloud instance this installation talks to. A plain text file
    # holding one URL; it is per-installation, so it never travels.
    "cloud_endpoint": "cloud_endpoint.txt",
    "custom_poses": "custom_poses.json",
    # 「通用设置」那一页里不跟游戏走的东西：手控鼠标、上下视角开关。
    # 它们原来一个都不存盘——手控鼠标压根没写过盘，上下视角只写在
    # scene_layout.json 里，而没定位过区域的人根本没有那个文件。结果是每次
    # 启动都悄悄回到默认值，玩家只会觉得"我明明开过"。
    "general_settings": "general_settings.json",
}

# Where 2.0 and earlier wrote them, relative to the program directory.  Only
# the entries that were ever written there appear: head_profile.json and
# camera_backend.json already lived in the user directory.
LEGACY_PROGRAM_FILES = {
    "profile_selection": "config/game_profile_selection.json",
    "motion_mappings": "config/motion_mappings.json",
    "voice_mappings": "config/voice_mappings.json",
    "scene_layout": "config/scene_layout.json",
    "scene_reference": "config/scene_reference.jpg",
    "require_paired_devices": "config/require_paired_devices",
}

# Read-only inputs that belong to the installation, not to the user.  Listed so
# the classification test can prove every file under config/ has been
# considered -- a new one shows up as unclassified rather than silently
# defaulting into either category.
PROGRAM_CONFIG_FILES = {
    "model_root.txt",
    "vosk_model_path.txt",
    "sherpa_kws_model_path.txt",
    "vigemclient_dll.txt",
    "funasr_python_path.txt",
    "funasr_seaco_model_path.txt",
    "funasr_vad_model_path.txt",
    "voice_commands_v094.json",
    "scene_layout.example.json",
    # 发布用的网页包签名私钥放在哪。跟上面那几个模型路径同一类：指向本机一个
    # 文件，只有发布的人有，装了软件的玩家没有这个文件也一切正常。
    "phone_web_signing_key.txt",
}


def user_data_path(base_dir: Path | str, key: str) -> Path:
    """Absolute path of one user-data file under *base_dir*."""
    try:
        name = USER_DATA_FILES[key]
    except KeyError:
        raise KeyError(f"unknown user data file: {key!r}") from None
    return Path(base_dir) / name


def legacy_program_path(program_root: Path | str, key: str) -> Path | None:
    """Where 2.0 and earlier kept *key*, or None if it never lived there."""
    relative = LEGACY_PROGRAM_FILES.get(key)
    return None if relative is None else Path(program_root) / relative


def migration_plan(base_dir: Path | str, program_root: Path | str) -> list[tuple[str, Path, Path]]:
    """Every (key, source, destination) pair a migration would consider.

    Pure: it does not look at the filesystem and does not decide whether a copy
    is needed.  The caller checks what actually exists, which keeps the
    decision auditable and this function testable without a disk.
    """
    plan = []
    for key in LEGACY_PROGRAM_FILES:
        source = legacy_program_path(program_root, key)
        if source is not None:
            plan.append((key, source, user_data_path(base_dir, key)))
    return plan
