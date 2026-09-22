"""What may be synced to the cloud, as a whitelist.

A blacklist would default to allowing, and the next config file someone adds --
a camera backend cache, a DLL path, a model location -- would travel to another
machine before anyone noticed. So the bundle builder takes its file list *only*
from here. There is deliberately no code path anywhere that walks the user data
directory and syncs what it finds.

The three categories:

**User configuration** travels. Per-game mappings, motions and voice bindings
describe intent, not hardware, so they mean the same thing on any machine.

**Device configuration** never travels. Camera backend probe results, absolute
model paths, the ViGEm DLL location: every one of these is a fact about one
installation, and on another machine they are simply wrong.

**Spatial calibration** never travels, and this is the one that is easy to get
wrong because it looks like user configuration. ``scene_layout.json`` is a
homography onto one frame captured from one camera in one physical position,
and ``scene_reference.jpg`` is that frame. scene_layout.py already refuses to
load the layout when the reference photo is missing. Copied to another machine
the file parses, the app starts, and the six zones sit somewhere silently
wrong -- which is worse than an error.

``head_profile.json`` sits between the categories and is therefore excluded for
now. Its tuning (sensitivity, deadzone, chosen control mode) is a preference
and would travel fine; its calibration centre, camera jitter estimate and
personal PnP geometry are tied to one person in front of one camera. Splitting
the file is the prerequisite for syncing half of it, and that is not done yet.
"""

from __future__ import annotations

USER_CONFIGURATION = "user_configuration"
DEVICE_CONFIGURATION = "device_configuration"
SPATIAL_CALIBRATION = "spatial_calibration"

# The only keys a cloud bundle may ever contain.  Keys are the same ones
# motioncontrol_shared.user_layout uses, so a file is named once.
CLOUD_SYNC_ALLOWLIST = {
    "profile_selection",
    "motion_mappings",
    "voice_mappings",
}

# Every user-data key, classified.  Anything absent from this map is a
# programming error rather than an implicit "do not sync": the test suite
# asserts the two stay in step, so a new file has to be sorted deliberately.
CLASSIFICATION = {
    "profile_selection": USER_CONFIGURATION,
    "motion_mappings": USER_CONFIGURATION,
    "voice_mappings": USER_CONFIGURATION,
    # Registered, deliberately not synced -- see the module docstring.
    "head_profile": DEVICE_CONFIGURATION,
    "camera_backend": DEVICE_CONFIGURATION,
    "paired_devices": DEVICE_CONFIGURATION,
    "require_paired_devices": DEVICE_CONFIGURATION,
    # Someone self-hosting points their own install at their own server;
    # carrying that address to another machine would silently redirect it.
    "cloud_endpoint": DEVICE_CONFIGURATION,
    # 用户自己录的姿势。模板已经做成位置、距离、体型无关的了（见
    # motioncontrol_shared.pose_template），所以它确实能跨机器跨人使用——
    # 和 scene_layout 那种绑死一个机位的东西不是一回事。
    #
    # 暂时不进同步白名单：进白名单就要有对应的规范化入口，而云端还没有校验姿势
    # 模板的逻辑。以后补是纯加法。
    "custom_poses": USER_CONFIGURATION,
    # 和 head_profile 同一种情况：上下视角开不开是纯偏好，换台机器一样成立；
    # 但手控鼠标的握拳阈值是照着你的手和你的摄像头读出来的两个数定的（张开一次、
    # 握紧一次，取中间），换个人换个机位就不对了。拆开文件是同步一半的前提，
    # 还没做，所以整份不走。
    "general_settings": DEVICE_CONFIGURATION,
    # 自己添加的游戏只是一个名字加一个基础档 id，不含任何本机信息，换台机器
    # 一样成立——它和 profile_selection 是同一类东西，理应一起走。
    #
    # 暂时不进白名单：云端还不认识这些 id，收到一个引用了 custom-xxx 的配置
    # 会当成引用了一个不存在的游戏。要一起做，不是加一行能完的事。
    "custom_games": USER_CONFIGURATION,
    # 这台电脑在手机自动发现里的标识。它的全部意义就是"和别的机器不一样"，
    # 同步过去等于让两台电脑自称同一台，手机会把第二台当成第一台的另一个网卡，
    # 于是永远只能看见其中一台。属于绝对不能跨机器走的那一类。
    "discovery_instance": DEVICE_CONFIGURATION,
    # 键盘宏就是一串键名和几个毫秒数，不含任何本机信息，换台机器一样成立——
    # 和 custom_games 是同一类东西。
    #
    # 暂时不进同步白名单，理由也和它一样：绑定里是按编号引用宏的，云端收到一份
    # 引用了 m_xxx 的配置时手上没有那条宏，只能当成引用了一个不存在的东西。
    # 要么宏跟着配置一起走，要么都别走；只让宏单独走是更坏的一种——两边编号
    # 撞上就会张冠李戴。以后一起做，是纯加法。
    "key_macros": USER_CONFIGURATION,
    # 唤醒词和急停口令。是"你的"没错，但**绝不能进可分享的那一类**：分享一份语音
    # 配置是在分享"说什么话按什么键"，不是在把自己的唤醒词装到别人机器上。
    # 所以它单独一个文件、全局一份，而且永远不在 CLOUD_SYNC_ALLOWLIST 里。
    "personal_voice": USER_CONFIGURATION,
    "scene_layout": SPATIAL_CALIBRATION,
    "scene_reference": SPATIAL_CALIBRATION,
}


def may_sync(key: str) -> bool:
    return key in CLOUD_SYNC_ALLOWLIST


def syncable_keys() -> tuple[str, ...]:
    """The bundle builder's only source of truth for what to include."""
    return tuple(sorted(CLOUD_SYNC_ALLOWLIST))


def classify(key: str) -> str:
    try:
        return CLASSIFICATION[key]
    except KeyError:
        raise KeyError(
            f"{key!r} is not classified; add it to CLASSIFICATION in "
            "motioncontrol_shared/sync_allowlist.py as user configuration, "
            "device configuration or spatial calibration"
        ) from None
