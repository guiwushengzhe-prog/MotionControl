from __future__ import annotations

import json
from pathlib import Path

from profile_audit import audit_library
from steaminput_builder import BuildResult, SeedGame, profile_from_vdf, save_library


SAMPLE = r'''
"controller_mappings"
{
  "group" { "id" "0" "mode" "four_buttons" "bindings"
    {
      "button_A" "xinput_button A"
      "button_B" "xinput_button B"
      "button_X" "xinput_button X"
      "button_Y" "xinput_button Y"
    }
  }
  "group_source_bindings" { "0" "button_diamond active" }
  "switch_bindings" { "bindings"
    {
      "left_bumper" "xinput_button shoulder_left"
      "right_bumper" "xinput_button shoulder_right"
    }
  }
}
'''


def _init_catalog(root: Path):
    profiles = root / "game_profiles" / "profiles"
    profiles.mkdir(parents=True)
    generic = {"schema": "motioncontrol.game_profile.v1", "id": "generic-xbox", "name": "Generic", "bindings": {}}
    (profiles / "generic-xbox.json").write_text(json.dumps(generic), encoding="utf-8")
    (root / "game_profiles" / "catalog.json").write_text(json.dumps({
        "schema": "motioncontrol.game_catalog.v1",
        "count": 1,
        "games": [{"id": "generic-xbox", "name": "Generic", "profile": "profiles/generic-xbox.json", "verified": True, "source": "builtin"}],
    }), encoding="utf-8")


def test_refresh_preserves_human_verified_flag_and_metadata(tmp_path: Path):
    _init_catalog(tmp_path)
    seed = SeedGame(123, "Example", priority=True)
    profile = profile_from_vdf(seed, SAMPLE, {"file_id": 1})
    save_library(tmp_path, [BuildResult(seed, True, profile)])

    catalog_path = tmp_path / "game_profiles" / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    item = next(x for x in catalog["games"] if x.get("appid") == 123)
    item["verified"] = True
    item["verification"] = {"by": "human", "note": "tested in game"}
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    refreshed = profile_from_vdf(seed, SAMPLE, {"file_id": 2})
    save_library(tmp_path, [BuildResult(seed, True, refreshed)])
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    item = next(x for x in catalog["games"] if x.get("appid") == 123)
    assert item["verified"] is True
    assert item["verification"]["note"] == "tested in game"
    assert item["priority"] is True


def test_audit_reports_priority_gaps_without_build_gate(tmp_path: Path):
    _init_catalog(tmp_path)
    seed = SeedGame(123, "Example")
    profile = profile_from_vdf(seed, SAMPLE, {"file_id": 1})
    save_library(tmp_path, [BuildResult(seed, True, profile)])

    report = audit_library(tmp_path, priority_games=[
        {"appid": 123, "name": "Example"},
        {"appid": 456, "name": "Missing"},
    ])
    assert report["build_gate"] is False
    assert report["summary"]["priority_total"] == 2
    assert report["summary"]["priority_present"] == 1
    assert report["summary"]["priority_verified"] == 0
    assert any(x["code"] == "priority_missing" for x in report["issues"])
