from __future__ import annotations

import json
from pathlib import Path

from motioncontrol.steaminput_builder import (
    BuildResult,
    SeedGame,
    build_one,
    profile_from_vdf,
    rank_candidates,
    save_library,
)


SAMPLE = r'''
"controller_mappings"
{
  "group" { "id" "0" "mode" "four_buttons" "bindings"
    {
      "button_A" "key_press SPACE, Jump"
      "button_B" "mouse_button LEFT, Fire"
      "button_X" "xinput_button X"
      "button_Y" "xinput_button Y"
    }
  }
  "group_source_bindings" { "0" "button_diamond active" }
  "switch_bindings" { "bindings"
    {
      "left_bumper" "mouse_wheel SCROLL_DOWN, Previous Weapon"
      "right_bumper" "xinput_button shoulder_right"
    }
  }
}
'''


def test_profile_from_vdf_maps_six_zone_controls_without_guessing():
    seed = SeedGame(123, "Example Game")
    candidate = {"official": True, "file_id": 99, "title": "Official"}
    profile = profile_from_vdf(seed, SAMPLE, candidate)
    assert profile["quality"]["usable"] is True
    assert profile["quality"]["zone_coverage"] == 6
    zones = profile["bindings"]["zones"]
    assert zones["rightHandLower"]["action"] == {"type": "keyboard", "target": "SPACE", "behavior": "hold"}
    assert zones["rightHandUpper"]["action"]["target"] == "LEFT"
    assert zones["leftFoot"]["action"] == {"type": "mouse_wheel", "target": "SCROLL_DOWN", "behavior": "tap"}
    assert zones["rightFoot"]["action"]["target"] == "RB"


def test_profile_rejects_runtime_unsupported_keyboard_key():
    bad = SAMPLE.replace("key_press SPACE", "key_press CAPSLOCK")
    profile = profile_from_vdf(SeedGame(123, "Example"), bad, {"file_id": 1})
    assert profile["quality"]["zone_coverage"] == 5
    assert "button_a" in profile["quality"]["rejected"]


def test_candidate_ranking_prefers_official_then_real_usage():
    ranked = rank_candidates([
        {"file_id": 1, "official": False, "lifetime_playtime_seconds": 100000, "subscriptions": 5000, "votes": {"up": 100}},
        {"file_id": 2, "official": True, "lifetime_playtime_seconds": 1, "subscriptions": 1},
        {"file_id": 3, "official": False, "lifetime_playtime_seconds": 500000, "subscriptions": 100},
    ])
    assert ranked[0]["file_id"] == 2
    assert ranked[1]["file_id"] == 3


class FakeClient:
    def app_info(self, appid):
        return {"official_configs": {"controller_xboxone": 42}}

    def search_configs(self, appid, game_name, limit=25):
        return []

    def file_details(self, file_id):
        return {"file_id": file_id, "file_url": "https://example.invalid/config.vdf", "controller_type": "controller_xboxone"}

    def download_vdf(self, url):
        return SAMPLE


def test_build_one_uses_official_candidate_and_returns_usable_profile():
    result = build_one(FakeClient(), SeedGame(123, "Example Game"))
    assert result.usable is True
    assert result.profile["quality"]["zone_coverage"] == 6
    assert result.profile["source"]["official"] is True


def test_save_library_reports_target_but_never_enforces_300_gate(tmp_path: Path):
    gp = tmp_path / "game_profiles" / "profiles"
    gp.mkdir(parents=True)
    (gp / "generic-xbox.json").write_text(json.dumps({
        "schema": "motioncontrol.game_profile.v1", "id": "generic-xbox", "name": "Generic", "bindings": {}
    }), encoding="utf-8")
    (tmp_path / "game_profiles" / "catalog.json").write_text(json.dumps({
        "schema": "motioncontrol.game_catalog.v1", "count": 1,
        "games": [{"id": "generic-xbox", "name": "Generic", "profile": "profiles/generic-xbox.json"}]
    }), encoding="utf-8")

    profile = profile_from_vdf(SeedGame(123, "Example Game"), SAMPLE, {"file_id": 1})
    report = save_library(tmp_path, [BuildResult(SeedGame(123, "Example Game"), True, profile)], target_count=300)
    assert report["batch_usable"] == 1
    assert report["library_generated_count"] == 1
    assert report["target_met"] is False
    assert report["build_gate"] is False
    assert (tmp_path / "game_profiles" / "catalog.json").exists()


def test_save_library_is_incremental_across_batches(tmp_path: Path):
    gp = tmp_path / "game_profiles" / "profiles"
    gp.mkdir(parents=True)
    generic = {"schema": "motioncontrol.game_profile.v1", "id": "generic-xbox", "name": "Generic", "bindings": {}}
    (gp / "generic-xbox.json").write_text(json.dumps(generic), encoding="utf-8")
    (tmp_path / "game_profiles" / "catalog.json").write_text(json.dumps({
        "schema": "motioncontrol.game_catalog.v1", "count": 1,
        "games": [{"id": "generic-xbox", "name": "Generic", "profile": "profiles/generic-xbox.json"}]
    }), encoding="utf-8")

    p1 = profile_from_vdf(SeedGame(101, "Game One"), SAMPLE, {"file_id": 1})
    r1 = save_library(tmp_path, [BuildResult(SeedGame(101, "Game One"), True, p1)])
    assert r1["library_generated_count"] == 1

    p2 = profile_from_vdf(SeedGame(202, "Game Two"), SAMPLE, {"file_id": 2})
    r2 = save_library(tmp_path, [BuildResult(SeedGame(202, "Game Two"), True, p2)])
    assert r2["library_generated_count"] == 2
    catalog = json.loads((tmp_path / "game_profiles" / "catalog.json").read_text(encoding="utf-8"))
    assert {g["appid"] for g in catalog["games"] if g.get("appid")} == {101, 202}


def test_existing_profile_appids_only_counts_catalog_backed_files(tmp_path):
    from motioncontrol.steaminput_builder import existing_profile_appids
    library = tmp_path / "game_profiles"
    profiles = library / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "ok.json").write_text("{}", encoding="utf-8")
    (library / "catalog.json").write_text(json.dumps({
        "schema": "motioncontrol.game_catalog.v1",
        "count": 3,
        "games": [
            {"id": "ok", "appid": 123, "profile": "profiles/ok.json"},
            {"id": "ghost", "appid": 456, "profile": "profiles/missing.json"},
            {"id": "builtin", "appid": None, "profile": "profiles/ok.json"},
        ],
    }), encoding="utf-8")
    assert existing_profile_appids(tmp_path) == {123}


def test_search_configs_filters_by_appid_without_title_text():
    from motioncontrol.steaminput_builder import SteamInputDBClient

    class CaptureClient(SteamInputDBClient):
        def __init__(self):
            pass

        def _json(self, method, path, body=None):
            self.captured = (method, path, body)
            return {"items": []}

    client = CaptureClient()
    client.search_configs(271590, "Grand Theft Auto V Legacy", limit=7)
    method, path, body = client.captured
    assert method == "POST"
    assert path == "/v1/search/configs"
    assert body["filter"]["app_id"] == "271590"
    assert body["query_text"] == ""
    assert body["limit"] == 7
