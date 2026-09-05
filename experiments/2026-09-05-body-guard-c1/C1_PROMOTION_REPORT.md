# MotionControl body-motion head guard C1 promotion report

Date: 2026-09-05
Baseline: `d8d1ffc702b1b5685eab65b8291951024f6d4dcf`
Candidate: inactive-only raw onset (`raw >= 2.50`) requiring >=8 common velocity points; legacy EMA/hysteresis/recovery unchanged.

## Input verification

- 8522 video SHA256: `FD6365CAAB16CCCA7E37019E6624A6FCA87A95C61B131B2886B5BFAEBE18C2D2`
- MediaPipe Full model SHA256: `5134A3AAD27A58B93DA0088D431F366DA362B44E3CCFBE3462B3827A839011B1`
- Pose JSONL: 2559 rows, frame_index 0..2558, all detection/world_pose valid.
- Pose JSONL SHA256: `3856E6615668AC681AD2ACF61ACDC9B0D73255E3C018ED16EA253A1E9A71CC6A`
- Delivery ZIP SHA256: `CD6C65A596C92AA550ECF1EE26F98736C592B35E2E0E80E6041B8052664761CF`

## 8522 production guard replay

Replay method: use the newly supplied Pose stream to run the production body-motion classifier + body guard state machine, and feed each frame the historical unguarded Mouse-X from `motioncontrol-8522-current-replay.json`. This keeps the horizontal-head signal identical between A/B and avoids unrelated horizontal-policy/profile differences.

Self-consistent same-data A/B:

- Full-video horizontal nonzero frames: **637 -> 622**, -15 (-2.35%).
- Full-video absolute X sum: **29607.490 -> 28831.490**, -776 (-2.62%).
- Guard-active frames: **606 -> 641**, +35 frames; occupancy 23.68% -> 25.05%.
- Historical `d8d1ffc` guard replay is reproduced except 10/2559 frames, localized to 54.966-55.266 s. None of those 10 frames overlap the C1 incremental removals, so the measured C1 delta is unaffected by that residual re-extraction mismatch.

Balanced manually frozen RGB action windows (16 windows: 4 squat, 4 calf-back, 4 hands-cross, 4 cross-knee-elbow):

- Nonzero frames inside action windows: **185 -> 181**.
- Absolute X sum inside action windows: **8506.406 -> 8366.406**, -140.
- Sign switches: **8 -> 8** (no worsening).
- Action-window guard occupancy: 24.88% -> 26.30%.
- Triggered windows: **7/16 -> 7/16**; C1 improves onset timing but not detection recall.
- Trigger delay P50: **0.366 -> 0.366 s**.
- Trigger delay P90: **0.540 -> 0.473 s**.
- Trigger delay max: **0.600 -> 0.534 s**.
- Recovery delay P50/P90: **0/0 -> 0/0 s**; max **0.167 -> 0.167 s**.

The measurable manual-window gains occur on the cross-knee-elbow subset:

- cross_knee_elbow_01: trigger 0.500 -> 0.433 s; output unchanged.
- cross_knee_elbow_02: trigger 0.600 -> 0.534 s; nonzero 17 -> 15; abs X 825 -> 727.
- cross_knee_elbow_03: trigger 0.434 -> 0.367 s; nonzero 7 -> 5; abs X 286 -> 244.
- cross_knee_elbow_04: trigger 0.300 -> 0.267 s; output unchanged.

Important limitation: using the independently frozen human RGB onset, the first 50/100/150 ms error metrics are **unchanged**. C1 advances the velocity guard by roughly 1-2 frames where raw crosses before EMA, but the raw body-speed signal itself often appears hundreds of milliseconds after the visible human action begins. Therefore C1 does not solve true action-onset leakage.

## Frozen real head-control regression

Five-Pose exact A/B remains unchanged in all scorable frozen windows:

- 24 clear-yaw windows: correct-output integral retention worst/P50/P90 = **1.0 / 1.0 / 1.0**.
- Correct-sign frames: **44 -> 44**.
- Correct integral: **0.45529539199446956 -> identical**.
- Wrong-sign integral: **0.1109881572890033 -> identical**.
- First correct response latency P50: **0.133333 s -> identical**; P90: **0.373333 s -> identical**.
- In-action intermittency P50/P90: **0/0 -> identical**.
- Neutral-static: nonzero **45 -> 45**, integral **0.3777749190576734 -> identical**.
- Pitch-only: nonzero **64 -> 64**, integral **0.19445538026757148 -> identical**.
- The only five-Pose state differences are in excluded setup/end portions of `6d210...`; no scorable window changes.

## Engineering validation

- `python -m pytest -q tests/test_body_motion_head_guard.py` -> **7 passed**.
- `python -m py_compile control_kernel.py tests/test_body_motion_head_guard.py` -> pass.
- `git diff --check` -> pass.

## Decision

**PROMOTE C1 as an incremental net-benefit candidate.**

Reason: exact real-data replay shows a small but repeatable reduction in body-motion horizontal leakage with zero observed loss in the frozen clear-yaw, neutral, or pitch windows and no recovery-delay regression.

Do not describe this as the final fix. Remaining boundary:

1. Human-visible action onset first 50/100/150 ms is not improved.
2. 9/16 manually sampled body-action windows still never trigger the speed/body guard during the annotated repetition.
3. There is still no frozen真人 dataset for simultaneous strong body motion + intentional sustained yaw, so preservation of deliberate yaw during exercise remains unproven.
4. No live game / real output acceptance was performed.
