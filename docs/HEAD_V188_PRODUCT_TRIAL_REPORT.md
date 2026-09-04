# Head-control v188 product trial integration

Date: 2026-09-04

Branch: `codex/head-v188-realdata-20260904`

## Decision

`gesture_v188` is added as an explicit **trial** option. It is not the default
and it does not replace `classic`, `gesture_v153`, or direct `frozen22`.

The product implementation keeps the existing personal-PnP v153 yaw source and
relative ratchet. It adds the frozen v187 time-based Frozen22 confidence gate,
the evaluated C0 zero committed-fallback, and the v188 pitch-dominance guard as
an output-only layer. The gate never feeds back into the ratchet state.

This is a product-port experiment, not a claim that `head_control.py` is
byte-identical to the standalone research v188 controller. The separate
research controller also contains v160 real-shape fallback logic that the
product v153 base does not contain.

## Frozen evidence

- Frozen v187 SHA256:
  `96F287A0B0983DB670C941645919DB1F5E41004D7C8B194871002AB46067E8A7`.
- Frozen v187 gate test SHA256:
  `0194F28973C690E80AA412809B7EEC6078CA505BBBD2AE3ECF397BD7AD35C810`.
- Versioned standalone v188 research candidate SHA256:
  `A0A2E75A33178F70DA5A74D973E33DAB1E48443DE1311E697D520D6B9472BC3C`.
- RGB-only evaluation-window freeze commit:
  `cc3887a611b5702c190bd3bfa7fedce63dc0509d`.
- Product A/B replay directory:
  `I:\MotionControl_HeadC0_20260903\product_gesture_v188_replay_01`.
- Replay `frames.csv` SHA256:
  `8606829E3FBE102264A0B135CFF2A379CE7B6F1C30A5FF68990A29A8E611F5E9`.
- Replay `window_metrics.csv` SHA256:
  `675825F43C57554598CA320194E82A1ECEC1E8F397687BEC9C519DCE1F9F5C3A`.

All variants used the same 9,682 recorded Pose33/world frames and the same
precommitted evaluation windows. No mouse, controller, keyboard, service, or
game output was enabled during replay.

## Product A/B results

| Window class | Product v153 | Product v188 trial | Change |
|---|---:|---:|---:|
| Neutral-static activity (890 frames) | 71.24% | 0.00% | -71.24 pp |
| Neutral-static absolute integral | 5.6744 | 0.0000 | -100.0% |
| Pitch-only activity (1,518 frames) | 61.73% | 2.83% | -58.90 pp |
| Pitch-only absolute integral | 8.8889 | 0.4952 | -94.4% |
| Clear-yaw correct frames (450 frames) | 256 | 229 | -27 |
| Clear-yaw wrong-direction frames | 40 | 17 | -23 |
| Clear-yaw absolute integral | 4.8875 | 4.1913 | -14.2% |
| Return reverse-residual integral | 0.3632 | 0.3586 | -1.3% |
| Historical motion absolute integral | 1.0300 | 0.6467 | -37.2% |

The trial fixes the dominant visible drift in the recorded data and sharply
reduces wrong-direction clear-yaw output, but it remains conservative on the
historical motion windows and does not materially solve the measured return residual.
Therefore it is deliberately exposed for live user evaluation rather than
promoted as the default.

## Focused checks

- Python compile: `head_control.py`, replay tool, and directed v188 test pass.
- JavaScript syntax: `web/app.js` passes.
- Directed tests: v188 selection/recalibration, static blocking, clear-yaw gate
  opening at 20/30/45/60 FPS, pitch-dominant lease suppression, existing v153,
  and existing Frozen22 integration: 12 passed.

These checks are offline/static only. They are not live-camera, real mouse,
real-game, phone, or release acceptance.
