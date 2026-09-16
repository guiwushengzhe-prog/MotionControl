"""Shared helpers for driving head control in tests.

v5.1 replaced v4.4's "deflection maps to velocity" model with an intent model:
an outward *turn* produces movement, and the controller looks at how the signal
is changing, not just where it currently sits. A test that jumps the yaw from 0
to 9 degrees between one frame and the next therefore produces no output at
all -- there was no turn to detect, only a teleport.

Measured on this build, which is what these helpers encode:

    instant jump to 9 deg ->  0.0, 0.0, 0.0, 0.0
    ramp 0 -> 9 deg       ->  0.0, 0.168, 0.336, 0.504, 0.566, 0.568 ...

So a test that wants output has to turn the head over several frames the way a
person would. That is what `turn_head` does.
"""

from __future__ import annotations

FRAME_S = 1.0 / 30.0


def turn_head(controller, target_yaw: float, *, start_at: float = 1.0,
              pitch: float = 0.0, frames: int = 6, frame_s: float = FRAME_S,
              start_yaw: float = 0.0):
    """Turn the head from `start_yaw` to `target_yaw` over `frames` updates.

    Returns (last_x, last_y, next_time) so a caller can carry on from where the
    turn finished.
    """
    x = y = 0.0
    now = start_at
    for index in range(1, frames + 1):
        share = index / frames
        yaw = start_yaw + (target_yaw - start_yaw) * share
        # Pitch is ramped alongside rather than jumped to: a pitch that appears
        # all at once looks pitch-dominant and trips the guard that keeps
        # looking up and down from swinging the view sideways.
        x, y = controller.update({"yaw": yaw, "pitch": pitch * share}, 640, 480, now=now)
        now += frame_s
    return x, y, now


def hold_head(controller, yaw: float, *, start_at: float, pitch: float = 0.0,
              frames: int = 3, frame_s: float = FRAME_S):
    """Keep the head still at `yaw`; returns (last_x, last_y, next_time)."""
    x = y = 0.0
    now = start_at
    for _ in range(frames):
        x, y = controller.update({"yaw": yaw, "pitch": pitch}, 640, 480, now=now)
        now += frame_s
    return x, y, now
