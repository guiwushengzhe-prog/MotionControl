# MotionControl body-motion head guard C2.6 promotion report

Date: 2026-09-05
Baseline: C2.5 `4ac5d69af5aaa6210a8bb1e4524cfc8cf9ec5840`
Candidate: supported two-frame limb-segment articulation evidence

## Change

- Preserve all C2.5 distal evidence, C2.4 67 ms bridge/post-burst veto, and C1 raw/EMA/persistent guard/recovery.
- Add a segment-deformation signal: speed of the distal-minus-proximal limb vector, which cancels rigid translation of the whole limb.
- Arm segments: deformation speed >= 0.80 for two consecutive frames.
- Leg segments: deformation speed >= 1.20 for two consecutive frames.
- Same-chain proximal landmark speed must be >= 0.10, >=8 common velocity points remain required.
- The signal is output-only transient evidence; it cannot activate or renew persistent guard.

## Frozen-data separation

- Maximum observed two-frame segment deformation in legal head windows:
  - left arm <= ~0.224, right arm <= ~0.188;
  - left leg <= ~0.671, right leg <= ~0.797.
- Promoted thresholds therefore retain a large arm margin and ~0.40 minimum leg margin over observed legal two-frame motion.
- Rigid limb translation is explicitly covered by a negative unit test and does not trigger this path.

## Real-data A/B versus C2.5

- 8522 full-video nonzero X: **380 -> 355** (-6.58%).
- Full-video absolute X: **18151.743 -> 16813.304** (-7.37%).
- Persistent guard frames: **635 -> 635** (identical).
- 16 frozen action-window nonzero X: **78 -> 70** (-10.26%).
- Action-window absolute X: **3823.951 -> 3367.943** (-11.93%).
- Sign switches: **8 -> 8**.
- First 50/100/150 ms nonzero frames remain 8/12/13 -> 8/12/13; C2.6 targets mid-action articulation leakage, not initial onset.

Changed action windows:
- `squat_01`: 5 -> 2 nonzero frames.
- `calf_back_04`: 18 -> 14.
- `cross_knee_elbow_02`: 6 -> 5.

## Frozen real-head regression

- `39997`: X changed 0, guard changed 0, score changed 0.
- `30dd`: X changed 0, guard changed 0, score changed 0.
- `af4e`: X changed 0, guard changed 0, score changed 0.
- `ed114`: X changed 0, guard changed 0, score changed 0.
- `6d210`: X changed 0, guard changed 0, score changed 0.

- In `6d210`, extra transient evidence is confined to the frozen front-facing-head + large-arm-motion neutral region and excluded portions; clear-yaw, return, and pitch-only remain untouched.

## Engineering validation

- `pytest -q tests/test_body_motion_head_guard.py`: **20 passed**.
- `py_compile`: pass.
- `git diff --check`: pass.

## Decision

**PROMOTE C2.6.**

Reason: physically distinct articulation evidence removes additional real body-motion leakage across multiple action types, while every frozen real-head output/guard/score frame remains unchanged and persistent guard occupancy is identical.

Remaining boundary: simultaneous strong body motion + intentional sustained yaw still lacks frozen real-human joint truth.
