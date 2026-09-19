"""Schema identifiers and pure schema migrations.

These strings are a wire contract: the desktop writes them, the cloud reads
them back, and an unknown value must be rejected rather than guessed at.  They
live here, apart from any storage code, so both sides compare against the same
constants.
"""

from __future__ import annotations

import copy

SCHEMA = "motioncontrol.game_profile.v1"
CATALOG_SCHEMA = "motioncontrol.game_catalog.v1"
# 玩家自己添加的游戏。内置目录里只有两百个，搜不到的游戏以前只能占用别人的
# 坑位——界面上一直显示着错的游戏名，而且第二个未收录的游戏就没地方放了。
CUSTOM_GAMES_SCHEMA = "motioncontrol.custom_games.v1"
SELECTION_SCHEMA = "motioncontrol.profile_selection.v2"
SELECTION_SCHEMA_V1 = "motioncontrol.profile_selection.v1"

# The other two documents on the cloud sync whitelist.  Neither file carries a
# schema field on disk today -- the desktop has always written a bare object --
# so the canonical form adds one and a document without it is read as v1.  Both
# desktop loaders take the keys they know and ignore the rest, so a file that
# has been through the cloud still loads on an older build.
MOTION_MAPPINGS_SCHEMA = "motioncontrol.motion_mappings.v1"
VOICE_MAPPINGS_SCHEMA = "motioncontrol.voice_mappings.v1"


def is_selection_v1(data) -> bool:
    return isinstance(data, dict) and data.get("schema") == SELECTION_SCHEMA_V1


def migrate_selection_v1_to_v2(data: dict) -> dict:
    """Return the v2 form of a v1 selection document.

    Pure: the caller is responsible for backing up and persisting.  v1 held a
    single flat ``overrides`` map, which only ever applied to whichever game was
    selected at the time, so it becomes that one game's entry in v2.
    """
    if not isinstance(data.get("overrides", {}), dict):
        raise ValueError("游戏映射数据无效，原文件已保留")
    selected = str(data.get("selected_id", "generic-xbox"))
    return {
        "schema": SELECTION_SCHEMA,
        "selected_id": selected,
        "overrides_by_profile": {selected: copy.deepcopy(data.get("overrides", {}))},
    }
