from __future__ import annotations

import json
from pathlib import Path

from tools.mark_profile_verified import set_verified


def test_mark_and_clear_profile_verification(tmp_path: Path):
    gp = tmp_path / "game_profiles"
    gp.mkdir()
    path = gp / "catalog.json"
    path.write_text(json.dumps({
        "schema": "motioncontrol.game_catalog.v1",
        "count": 1,
        "games": [{"id": "steam-123-example", "appid": 123, "name": "Example", "profile": "profiles/x.json", "verified": False}],
    }), encoding="utf-8")

    marked = set_verified(tmp_path, "123", verified=True, note="tested combat and menus", verifier="tester")
    assert marked["verified"] is True
    assert marked["verification"]["by"] == "tester"
    assert marked["verification"]["note"] == "tested combat and menus"

    cleared = set_verified(tmp_path, "steam-123-example", verified=False)
    assert cleared["verified"] is False
    assert "verification" not in cleared
