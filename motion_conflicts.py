"""Shared motion-selection conflict rules.

The recognizer may still observe every motion for diagnostics.  These rules
only constrain which motion actions can be mapped at the same time, so an
overlapping pose cannot produce two game outputs.
"""

from __future__ import annotations

from collections.abc import Mapping


# Keep only conflicts that are explicit in the detector semantics or that can
# describe the same end pose.  A group means at most one member may be mapped.
MOTION_CONFLICT_GROUPS: tuple[tuple[str, ...], ...] = (
    ("jumping_jack", "hands_up"),
    ("jumping_jack", "side_step_jack"),
    ("march", "calf_back", "squat"),
)

MOTION_DISPLAY_NAMES = {
    "march": "原地踏步",
    "calf_back": "小腿向后",
    "squat": "下蹲",
    "hands_up": "双手过头",
    "jumping_jack": "开合跳",
    "side_step_jack": "侧步开合",
    "cross_knee_elbow": "提膝碰对侧肘",
    "hands_cross": "双手交叉",
}


def _normalise_id(value) -> str:
    ident = str(value or "").strip()
    if ident.startswith("motion."):
        ident = ident[7:]
    return ident


def _has_target(value) -> bool:
    if isinstance(value, (list, tuple, set)):
        return any(str(item or "").strip() for item in value)
    return bool(str(value or "").strip())


def find_motion_conflicts(selected_ids) -> tuple[tuple[str, ...], ...]:
    """Return conflicting groups present in *selected_ids*."""

    selected = {_normalise_id(value) for value in (selected_ids or [])}
    return tuple(
        tuple(ident for ident in group if ident in selected)
        for group in MOTION_CONFLICT_GROUPS
        if sum(ident in selected for ident in group) > 1
    )


def motion_conflict_message(conflicts) -> str:
    parts = []
    for group in conflicts or ():
        names = [MOTION_DISPLAY_NAMES.get(ident, ident) for ident in group]
        parts.append("、".join(names))
    if not parts:
        return ""
    return "动作不能同时映射：" + "；".join(parts) + "。请先取消其中一个动作。"


def validate_motion_ids(selected_ids) -> None:
    conflicts = find_motion_conflicts(selected_ids)
    if conflicts:
        raise ValueError(motion_conflict_message(conflicts))


def selected_motion_ids_from_config(items) -> set[str]:
    """Extract enabled legacy motion-config identifiers."""

    selected = set()
    for item in items or ():
        if not isinstance(item, Mapping) or not bool(item.get("enabled")):
            continue
        ident = _normalise_id(item.get("id"))
        if ident and _has_target(item.get("target")):
            selected.add(ident)
    return selected


def validate_motion_config(items) -> None:
    validate_motion_ids(selected_motion_ids_from_config(items))


def selected_motion_ids_from_bindings(bindings: Mapping | None) -> set[str]:
    """Extract mapped motion identifiers from grouped or flattened bindings."""

    if not isinstance(bindings, Mapping):
        return set()
    grouped = bindings.get("motions")
    if isinstance(grouped, Mapping):
        items = grouped.items()
    else:
        items = (
            (_normalise_id(key), value)
            for key, value in bindings.items()
            if _normalise_id(key) != str(key) or str(key).startswith("motion.")
        )
    selected = set()
    for raw_ident, binding in items:
        if not isinstance(binding, Mapping) or bool(binding.get("disabled")):
            continue
        action = binding.get("action") if isinstance(binding.get("action"), Mapping) else binding
        if not isinstance(action, Mapping):
            continue
        if str(action.get("type", "")).strip() and _has_target(action.get("target")):
            ident = _normalise_id(raw_ident)
            if ident:
                selected.add(ident)
    return selected


def validate_motion_bindings(bindings: Mapping | None) -> None:
    validate_motion_ids(selected_motion_ids_from_bindings(bindings))


def motion_conflict_payload() -> list[dict]:
    """Return a JSON-safe description for optional clients/diagnostics."""

    return [
        {
            "ids": list(group),
            "names": [MOTION_DISPLAY_NAMES.get(ident, ident) for ident in group],
        }
        for group in MOTION_CONFLICT_GROUPS
    ]

