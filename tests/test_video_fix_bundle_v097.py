from head_control import IntentAxis
from game_profiles import flatten_bindings
from steaminput_builder import SeedGame, profile_from_vdf


def test_returning_is_latched_across_center_until_motion_settles():
    axis = IntentAxis("yaw")
    cfg = dict(angle_threshold=.055, start_velocity=.12, stop_velocity=.045,
               release_threshold=.03)
    samples = [(0.00, 0.00), (.08, .05), (.15, .10), (.12, .15),
               (.06, .20), (.00, .25), (-.06, .30), (-.10, .35)]
    outputs = []
    states = []
    for signal, at in samples:
        result = axis.step(signal, at, **cfg)
        outputs.append(result["active"])
        states.append(result["state"])
    assert outputs[2]
    assert not any(outputs[3:])
    assert states[6] == "RETURNING"


def test_voice_profile_binding_defaults_to_edge_triggered():
    """Voice stays edge-triggered unless a mapping asks for otherwise.

    Holding used to be impossible here: an explicit "hold" was rewritten to
    "tap".  Latched voice output is now a supported, opt-in mapping, so the
    guarantee this pins is the default, not the rewrite.  test_voice_hold.py
    covers the explicit hold/release forms and their release paths.
    """
    flat = flatten_bindings({
        "voice": {
            "game.profile_slot_01": {"action": {"type": "keyboard", "target": "M"}}
        }
    })
    assert flat["voice.game.profile_slot_01"]["action"]["behavior"] == "tap"


def test_builder_uses_clear_motion_semantics_then_voice_for_remaining_outputs():
    vdf = r'''
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
      "button_menu" "key_press C, Crouch"
      "button_escape" "key_press M, Map"
    }
  }
}
'''
    profile = profile_from_vdf(SeedGame(123, "Example"), vdf, {"file_id": 1})
    assert profile["bindings"]["motions"]["squat"]["action"]["target"] == "C"
    voice = profile["bindings"]["voice"]
    assert voice["game.profile_slot_01"]["action"]["target"] == "M"
    assert "Map" in voice["game.profile_slot_01"]["label"]
