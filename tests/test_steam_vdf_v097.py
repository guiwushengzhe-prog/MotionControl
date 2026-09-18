from __future__ import annotations

from motioncontrol.steam_vdf import extract_physical_bindings, parse_binding_action, parse_vdf


SAMPLE = r'''
"controller_mappings"
{
  "group"
  {
    "id" "0"
    "mode" "four_buttons"
    "bindings"
    {
      "button_A" "key_press SPACE, Jump"
      "button_B" "mouse_button LEFT, Fire"
      "button_X" "xinput_button X"
      "button_Y" "xinput_button Y"
    }
  }
  "group_source_bindings"
  {
    "0" "button_diamond active"
  }
  "switch_bindings"
  {
    "bindings"
    {
      "left_bumper" "mouse_wheel SCROLL_DOWN, Previous Weapon"
      "right_bumper" "xinput_button shoulder_right"
      "button_menu" "xinput_button start"
    }
  }
}
'''


def test_vdf_parser_keeps_duplicate_keys_and_nested_blocks():
    tree = parse_vdf('"root" { "group" { "id" "1" } "group" { "id" "2" } }')
    assert tree[0][0] == "root"
    root = tree[0][1]
    groups = [value for key, value in root if key == "group"]
    assert len(groups) == 2


def test_binding_action_normalization():
    assert parse_binding_action("key_press LEFT_SHIFT, Sprint")[0] == {"type": "keyboard", "target": "SHIFT", "behavior": "hold"}
    assert parse_binding_action("mouse_button RIGHT")[0] == {"type": "mouse_button", "target": "RIGHT", "behavior": "hold"}
    assert parse_binding_action("mouse_wheel SCROLL_UP")[0] == {"type": "mouse_wheel", "target": "SCROLL_UP", "behavior": "tap"}
    assert parse_binding_action("xinput_button TRIGGER_LEFT")[0] == {"type": "gamepad_trigger", "target": "LT", "behavior": "hold"}
    assert parse_binding_action("xinput_button JOYSTICK_RIGHT")[0] == {"type": "gamepad", "target": "R3", "behavior": "hold"}
    action, _, reason = parse_binding_action("game_action Jump")
    assert action is None and "not directly convertible" in reason


def test_extract_physical_bindings_from_realistic_vdf_shape():
    bindings = extract_physical_bindings(SAMPLE)
    assert bindings["button_a"].action == {"type": "keyboard", "target": "SPACE", "behavior": "hold"}
    assert bindings["button_b"].action["type"] == "mouse_button"
    assert bindings["button_x"].action["target"] == "X"
    assert bindings["button_y"].action["target"] == "Y"
    assert bindings["left_bumper"].action == {"type": "mouse_wheel", "target": "SCROLL_DOWN", "behavior": "tap"}
    assert bindings["right_bumper"].action == {"type": "gamepad", "target": "RB", "behavior": "hold"}
    assert bindings["button_menu"].action["target"] == "START"

V3_NESTED = r'''
"controller_mappings"
{
  "version" "3"
  "group"
  {
    "id" "0"
    "mode" "four_buttons"
    "inputs"
    {
      "button_a" { "activators" { "Full_Press" { "bindings" { "binding" "xinput_button A" } } } }
      "button_B" { "activators" { "Full_Press" { "bindings" { "binding" "key_press SPACE, Jump" } } } }
      "button_X" { "activators" { "Long_Press" { "bindings" { "binding" "key_press R" } } "Full_Press" { "bindings" { "binding" "key_press PAGE_UP" } } } }
      "button_y" { "activators" { "Full_Press" { "bindings" { "binding" "key_press PAGE_DOWN" } } } }
    }
  }
  "group"
  {
    "id" "7"
    "mode" "switches"
    "inputs"
    {
      "left_bumper" { "activators" { "Full_Press" { "bindings" { "binding" "mouse_button BACK" } } } }
      "right_bumper" { "activators" { "Full_Press" { "bindings" { "binding" "mouse_button FORWARD" } } } }
    }
  }
  "preset" { "id" "0" "name" "Default" "group_source_bindings" { "7" "switch active" "0" "button_diamond active" } }
}
'''


def test_v3_nested_activators_and_preset_sources():
    parsed = extract_physical_bindings(V3_NESTED)
    assert parsed["button_a"].action == {"type": "gamepad", "target": "A", "behavior": "hold"}
    assert parsed["button_b"].action == {"type": "keyboard", "target": "SPACE", "behavior": "hold"}
    assert parsed["button_x"].action == {"type": "keyboard", "target": "PAGEUP", "behavior": "hold"}
    assert parsed["button_y"].action == {"type": "keyboard", "target": "PAGEDOWN", "behavior": "hold"}
    assert parsed["left_bumper"].action == {"type": "mouse_button", "target": "X1", "behavior": "hold"}
    assert parsed["right_bumper"].action == {"type": "mouse_button", "target": "X2", "behavior": "hold"}


def test_v3_does_not_promote_long_press_when_no_normal_press_exists():
    text = r'''"controller_mappings" { "version" "3" "group" { "id" "0" "inputs" { "button_a" { "activators" { "Long_Press" { "bindings" { "binding" "key_press Q" } } } } } } }'''
    assert "button_a" not in extract_physical_bindings(text)


def test_v3_uses_active_default_preset_group_not_inactive_layer():
    text = r'''
"controller_mappings"
{
  "group"
  {
    "id" "10"
    "inputs" { "button_a" { "activators" { "Full_Press" { "bindings" { "binding" "key_press F" } } } } }
  }
  "group"
  {
    "id" "20"
    "inputs" { "button_a" { "activators" { "Full_Press" { "bindings" { "binding" "xinput_button A" } } } } }
  }
  "preset"
  {
    "id" "0"
    "name" "Default"
    "group_source_bindings"
    {
      "10" "button_diamond inactive"
      "20" "button_diamond active"
    }
  }
}
'''
    bindings = extract_physical_bindings(text)
    assert bindings["button_a"].action == {"type": "gamepad", "target": "A", "behavior": "hold"}
    assert bindings["button_a"].source.startswith("group:20:")
