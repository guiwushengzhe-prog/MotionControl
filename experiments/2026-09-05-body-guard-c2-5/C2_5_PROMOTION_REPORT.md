# MotionControl body-motion head guard C2.5 promotion report

Date: 2026-09-05
Baseline: C2.4 `c54f54deb0cf5a41ac4033ba44ef3e71ed044957`
Candidate: supported two-frame distal-chain transient evidence

## Change

- Preserve C2.4 early thresholds, 67 ms output bridge, bounded post-burst veto, C1 raw/EMA/persistent guard, recovery, and head algorithm.
- Add per-chain distal evidence only when >=8 common velocity points are available.
- Arm: wrist normalized speed >= 1.20 for two consecutive frames, with same-side elbow speed >= 0.10 on both frames.
- Leg: ankle normalized speed >= 1.35 for two consecutive frames, with same-side knee speed >= 0.10 on both frames.
- This signal joins only the output-only transient early-evidence path. It cannot directly activate or renew persistent guard.
- Two-frame persistence plus proximal support is intentionally required to reject isolated one-landmark spikes.

## Real-data A/B versus C2.4

- 8522 full-video horizontal nonzero frames: **441 -> 380** (-61, -13.83%).
- Full-video absolute X: **20930.414 -> 18151.743** (-2778.671, -13.28%).
- Persistent guard frames: **635 -> 635** (identical).
- 16 frozen RGB action-window nonzero frames: **94 -> 78** (-17.02%).
- Action-window absolute X: **4564.839 -> 3823.951** (-16.23%).
- Action-window sign switches: **8 -> 8**.

- First 50 ms nonzero frames: **10 -> 8**.
- First 100 ms nonzero frames: **15 -> 12**.
- First 150 ms nonzero frames: **18 -> 13**.

Previously difficult windows now improve:
- `calf_back_03`: 22 -> 21 nonzero frames.
- `calf_back_04`: 21 -> 18.
- `hands_cross_03`: 11 -> 8.
- `cross_knee_elbow_02`: 11 -> 6.
- `cross_knee_elbow_03`: 2 -> 0.

## Frozen real-head regression

- `39997`: X changed 0, persistent guard changed 0, score changed 0.
- `30dd`: X changed 0, persistent guard changed 0, score changed 0.
- `af4e`: X changed 0, persistent guard changed 0, score changed 0.
- `ed114`: X changed 0, persistent guard changed 0, score changed 0.
- `6d210`: X changed 0, persistent guard changed 0, score changed 0.

- In `6d210`, additional transient evidence occurs in the scorable `front-facing head + large arm motion` neutral window and excluded setup/end portions; clear-yaw, return, and pitch-only gain no new transient evidence.
- Across all five complete videos final Mouse-X remains byte-for-byte numerically identical at every replay frame relative to C2.4.

## Engineering validation

- `pytest -q tests/test_body_motion_head_guard.py`: **19 passed**.
- `py_compile`: pass.
- `git diff --check`: pass.

## Decision

**PROMOTE C2.5.**

Reason: strong repeatable real-data reduction, including the prior distal-articulation blind spots, while the persistent guard lifecycle is unchanged and all five frozen real-head videos show zero output/guard/score regression.

Remaining boundary: strong body motion + simultaneous intentional sustained yaw still lacks frozen joint truth; do not claim mixed-intent is solved.
