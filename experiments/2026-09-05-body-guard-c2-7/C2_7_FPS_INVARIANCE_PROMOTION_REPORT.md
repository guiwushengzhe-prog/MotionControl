# MotionControl C2.7 FPS-invariance promotion report — 2026-09-05

## Decision
PROMOTE C2.7 on an independent branch. Do not merge product main automatically.

Local product branch: `head-body-guard-time-normalized-c2-7-20260905`
Local product commit: `34bc7e8fee0c9cb5d7c3b66d881840ce244bc5d3`
Parent C2.6: `f8afc3f81a817a4557625703ed2780dd65bb375c`

## Scope
C2.7 does not add a new body-motion suppression rule. It converts three promoted frame-count temporal semantics into monotonic-time semantics while preserving the promoted 30 FPS behavior:

- distal/segment chain confirmation: 2 frames -> 30 ms
- strong transient burst qualification: 4 frames -> 95 ms
- persistent-guard zero-output settle: 3 frames -> 60 ms

Existing time-based behavior remains unchanged: 67 ms output-only bridge, 100 ms post-burst token TTL, raw/EMA/persistent guard thresholds and hold/recovery logic.

## Exact 30 FPS real-data A/B: C2.6 -> C2.7
Same-process production module replay with identical `head_control.py` and identical Pose Full inputs.

- 8522 body-motion video: X changed 0; persistent guard changed 0; score changed 0.
- 39997: X changed 0; persistent guard changed 0; score changed 0.
- 30dd: X changed 0; persistent guard changed 0; score changed 0.
- af4e: X changed 0; persistent guard changed 0; score changed 0.
- ed114: X changed 0; persistent guard changed 0; score changed 0.
- 6d210: X changed 0; persistent guard changed 0; score changed 0.

Therefore all previously promoted C2.6 30 FPS body-jitter suppression and frozen clear-yaw/pitch/return behavior are preserved exactly in this replay.

## Multi-FPS timing audit
Measured sampled transition delays (first supported/evidence sample to transition):

| FPS | C2.6 distal confirm | C2.7 distal confirm | C2.6 strong-burst span | C2.7 strong-burst span | C2.6 settle | C2.7 settle |
|---:|---:|---:|---:|---:|---:|---:|
| 20 | 50.0 ms | 50.0 ms | 150.0 ms | 100.0 ms | 100.0 ms | 100.0 ms |
| 30 | 33.3 ms | 33.3 ms | 100.0 ms | 100.0 ms | 66.7 ms | 66.7 ms |
| 45 | 22.2 ms | 44.4 ms | 66.7 ms | 111.1 ms | 44.4 ms | 66.7 ms |
| 60 | 16.7 ms | 33.3 ms | 50.0 ms | 100.0 ms | 33.3 ms | 66.7 ms |

C2.6 therefore became materially more permissive at 45/60 FPS. C2.7 restores approximately 30/95/60 ms semantics, subject only to camera sampling quantization. 20 FPS remains coarsely quantized because one frame is 50 ms.

## Engineering checks
- `pytest -q tests/test_body_motion_head_guard.py`: 24 passed
- `python -m py_compile control_kernel.py tests/test_body_motion_head_guard.py`: pass
- `git diff --check`: pass before commit
- final worktree: clean

## Changed files
- `control_kernel.py`
- `tests/test_body_motion_head_guard.py`

## Safety boundary
The main unresolved evidence gap remains unchanged: no frozen real-human joint-truth set for simultaneous strong body motion + intentional sustained yaw. C2.7 does not claim that mixed-intent case is solved. Its promotion rationale is engineering invariance, not additional suppression on the 30 FPS datasets.
