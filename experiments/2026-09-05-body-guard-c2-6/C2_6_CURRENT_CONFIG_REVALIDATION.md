# C2.6 current-head-config revalidation

Date: 2026-09-05

## Purpose

The original body-guard promotion ladder intentionally used a historical unguarded 8522 Mouse-X stream to isolate body-guard behavior. Late residual analysis showed that several large historical tails no longer exist in the current head-control replay. This addendum therefore re-ranks d8 -> C2.6 using the current head-controller output as the common unguarded X source.

## Configuration integrity

`head_control.py` SHA-256 is identical in all compared product commits:

`f34a9a6a8451201f1af6d7c83eea2e040f98170c231babd94b1c8fbd34d18807`

Verified commits: d8d1ffc, be1b3cc (C1), a78b3e3 (C2.1), 84bbc88 (C2.2), 155ddf9 (C2.3), c54f54d (C2.4), 4ac5d69 (C2.5), f8afc3f (C2.6).

Common current-config no-body-guard head replay:
- 8522 rows: 2559
- nonzero X (>=0.01): 62
- absolute X sum: 1410.141
- file SHA-256: `faec24c0fa9072169064c5cd1a9774643e0512c7363f0af07943ff8a3288cc43`

Exact ranking output SHA-256: `4d9b8622c76d12902f2346807d8ec588d107a38af21260686f80070c5b8c1de1`.

## Same-current-head ranking

| version | full nonzero X | full abs X | persistent guard frames | 16 action-window nonzero X | action-window abs X |
|---|---:|---:|---:|---:|---:|
| d8 | 59 | 1367.528 | 481 | 19 | 319.226 |
| C1 | 56 | 1264.310 | 510 | 18 | 292.762 |
| C2.1 | 42 | 1063.154 | 510 | 8 | 121.542 |
| C2.2 | 39 | 1011.238 | 510 | 5 | 69.626 |
| C2.3 | 38 | 988.723 | 510 | 4 | 47.111 |
| C2.4 | 37 | 966.670 | 510 | 3 | 25.058 |
| C2.5 | 28 | 699.008 | 510 | 3 | 25.058 |
| C2.6 | **20** | **452.418** | **510** | **3** | **25.058** |

## Interpretation

C2.6 remains the clear champion under the current head-control configuration. Its benefit is not an artifact of the older historical X stream.

Relative to d8 under the current head signal:
- full-video nonzero X: 59 -> 20, -66.1%
- full-video absolute X: 1367.528 -> 452.418, -66.9%
- frozen body-action-window nonzero X: 19 -> 3, -84.2%
- frozen body-action-window absolute X: 319.226 -> 25.058, -92.2%

Relative to C1:
- full-video nonzero X: 56 -> 20, -64.3%
- action-window nonzero X: 18 -> 3, -83.3%

The persistent guard lifecycle is unchanged from C1 through C2.6 (510 frames with this current head signal); later gains come from bounded output-only transient evidence.

## Residual audit

Only 3 C2.6 nonzero frames remain inside the 16 frozen body-action windows. All three are at the end of `squat_02` (14.500-14.567 s), total absolute X 25.058.

The other current-config nonzero clusters are outside the 16 frozen body-action windows, around 4.43 s, 14.60-14.70 s, 35.30-35.47 s, and 55.93-56.07 s. RGB review indicates several are post-action/neutral head-control false-turn tails rather than ongoing body-motion detector misses. They should not be used to justify adding more body-motion rules.

## Decision

**C2.6 remains promoted. No C2.7 body-guard escalation from the historical residuals.**

The next high-value evidence gap is simultaneous strong body motion + intentional sustained yaw. Until that frozen real-human joint truth exists, further widening of body suppression would have poor risk/reward.
