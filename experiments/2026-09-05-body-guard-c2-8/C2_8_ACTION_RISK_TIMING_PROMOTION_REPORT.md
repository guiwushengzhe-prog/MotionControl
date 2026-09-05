# MotionControl C2.8 body-guard action-risk timing report — 2026-09-05

## Decision
PROMOTE C2.8 on an independent branch. Do not merge product main automatically.

Local product branch: `head-body-guard-action-risk-time-c2-8-20260905`
Local product commit: `527604e465c48c0dd977c934bff32730bb4ce731`
Parent C2.7: `34bc7e8fee0c9cb5d7c3b66d881840ce244bc5d3`

## Scope
C2.8 does not modify motion recognition or game/action trigger semantics. `motion_active` and its existing frame-count debounce remain untouched.

Instead, the body guard receives a separate time-debounced mirror of the same raw action conditions (`body_motion_action_risk`). This prevents persistent body guard activation from inheriting frame-rate-dependent motion debounce timing.

30 FPS-equivalent timing used only by the body-guard mirror:
- march: on 0 ms / off 30 ms
- calf_back, squat, hands_up: on 60 ms / off 95 ms
- jumping_jack, side_step_jack, cross_knee_elbow: on 30 ms / off 60 ms

The body guard now uses `body_motion_action_risk` rather than `motion_active` as the action-derived persistent activation source. Raw/EMA, C2.5 distal evidence, C2.6 segment articulation, C2.7 internal timing, 67 ms bridge and bounded post-burst veto remain unchanged.

## Exact 30 FPS real-data A/B: C2.7 -> C2.8
Same-process production module replay:
- 8522: X changed 0; persistent guard changed 0; score changed 0.
- 39997: X changed 0; guard changed 0; score changed 0.
- 30dd: X changed 0; guard changed 0; score changed 0.
- af4e: X changed 0; guard changed 0; score changed 0.
- ed114: X changed 0; guard changed 0; score changed 0.
- 6d210: X changed 0; guard changed 0; score changed 0.

Therefore all promoted C2.7 30 FPS head/body behavior is preserved exactly on the available real datasets.

## Action-output isolation
On all 2559 frames of 8522, C2.7 and C2.8 `motion_active` sets are identical: changed frames = 0. The new risk mirror is body-guard-only and does not feed the game/action output path.

## Multi-FPS debounce audit
Representative sampled first-to-transition delays:

### 3/4-frame class (squat/calf_back/hands_up)
| FPS | old motion on | timed risk on | old motion off | timed risk off |
|---:|---:|---:|---:|---:|
| 20 | 100.0 ms | 100.0 ms | 150.0 ms | 100.0 ms |
| 30 | 66.7 ms | 66.7 ms | 100.0 ms | 100.0 ms |
| 45 | 44.4 ms | 66.7 ms | 66.7 ms | 111.1 ms |
| 60 | 33.3 ms | 66.7 ms | 50.0 ms | 100.0 ms |

### 2/3-frame class (jumping_jack/side_step_jack/cross_knee_elbow)
| FPS | old motion on | timed risk on | old motion off | timed risk off |
|---:|---:|---:|---:|---:|
| 20 | 50.0 ms | 50.0 ms | 100.0 ms | 100.0 ms |
| 30 | 33.3 ms | 33.3 ms | 66.7 ms | 66.7 ms |
| 45 | 22.2 ms | 44.4 ms | 44.4 ms | 66.7 ms |
| 60 | 16.7 ms | 33.3 ms | 33.3 ms | 66.7 ms |

March preserves immediate activation; its two-frame release becomes a ~30 ms time rule.

## Important rejected alternative
Simply deleting the `motion_active` body-guard trigger was rejected. On 8522 it happened to leave final X unchanged, but persistent guard state changed on 115 frames. The action-derived risk path therefore remains meaningful and is time-normalized rather than removed.

## Engineering checks
- `pytest -q tests/test_body_motion_head_guard.py`: 28 passed
- `py_compile`: pass
- `git diff --check`: pass
- local worktree: clean

New tests enforce:
- body-motion action risk can start persistent guard;
- `motion_active` alone no longer directly starts body guard;
- 30 FPS risk transition frames match legacy action-debounce semantics;
- high FPS cannot shrink risk timing below intended durations;
- clear-body resets action-risk state.

## Safety boundary
The same unresolved evidence gap remains: no frozen real-human joint-truth set for simultaneous strong body motion + intentional sustained yaw. C2.8 changes timing architecture, not mixed-intent classification.
