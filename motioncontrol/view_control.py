"""Apply both view axes as one validated and recoverable configuration."""
from __future__ import annotations

import copy
from pathlib import Path
import time

from .config_transaction import replace_documents

HORIZONTAL = {"off", "left", "right", "roll_tilt", "head_turn", "head_responsive"}
VERTICAL = {"off", "left", "right", "head"}


def configure_view_control(kernel, horizontal: str, vertical: str) -> dict:
    if horizontal not in HORIZONTAL or vertical not in VERTICAL:
        raise ValueError("视角来源无效，请重新选择左右和上下控制方式")
    with kernel._lock:
        # Draft controllers cannot save intermediate settings or disturb the
        # currently active calibration/filter state on validation failure.
        head = copy.deepcopy(kernel.head_controller)
        profile_path = head.profile_path
        head.profile_path = None
        hand = copy.deepcopy(kernel.hand_mouse_controller)
        hand.configure({"enabled": horizontal in {"left", "right"} or vertical in {"left", "right"},
                        "horizontal_hand": horizontal if horizontal in {"left", "right"} else "off",
                        "vertical_hand": vertical if vertical in {"left", "right"} else "off"})
        head.configure(enabled=horizontal in {"roll_tilt", "head_turn", "head_responsive"},
                       horizontal_algorithm="gesture_v188" if horizontal == "head_turn"
                       else horizontal if horizontal in {"roll_tilt", "head_responsive"} else None)
        look = dict(kernel.vertical_look)
        look["enabled"] = vertical == "head"
        if kernel._persist:
            payload = kernel.general_settings_payload()
            payload["hand_mouse"] = dict(hand.config)
            payload["vertical_look"]["enabled"] = look["enabled"]
            documents = {kernel._general_settings_path(): payload}
            if profile_path:
                documents[Path(profile_path)] = head.profile_document()
            replace_documents(documents, kernel._general_settings_path().parent / ".view-control.journal")
        head.profile_path = profile_path
        kernel.head_controller = head
        kernel.hand_mouse_controller = hand
        kernel.vertical_look = look
        kernel.vertical_gate_active = False
        kernel._reset_vertical_head_locked()
        kernel._safe_output(kernel.output.apply, 0.0, 0.0)
        return {"kernel": kernel.status_locked(time.monotonic()), "hand_mouse": hand.status()}
