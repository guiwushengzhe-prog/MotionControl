# MotionControl C2.3 early-threshold 晋级报告

日期：2026-09-05

## 结论

PROMOTE C2.3。

C2.3 以已冻结 C2.2 `84bbc880b30f65ee84b2045ffd31a5c9ac16eacf` 为唯一基线。67 ms transient bridge、C1 raw/EMA/persistent guard、settle/recovery 全部不变；只把 early detector 从 `peak>=2.60 + second>=0.50 / coherent_vertical>=0.50` 调整为更有真人数据余量依据的 `peak>=2.40 + second>=0.40 / coherent_vertical>=0.35`。

## C2.2 -> C2.3 真人 A/B

### 8522 全视频

- 横向非零帧：472 -> 448（-5.08%）
- 横向绝对输出：22314.499 -> 21175.411（-5.10%）
- persistent guard：635 -> 635，逐帧完全一致。

### 16 个 RGB 人工冻结身体动作窗口

- 非零帧：107 -> 99（-7.48%）
- 绝对输出：5083.937 -> 4739.836（-6.77%）
- persistent guard：165 -> 165，完全一致。

### 人工 RGB 动作起点

- 前 50 ms：10 -> 10
- 前 100 ms：16 -> 15
- 前 150 ms：20 -> 18

## 为什么不继续降到 2.2 / 0.3

冻结真人数据给出了安全边际：

- clear-yaw 中满足 `second>=0.40` 时 peak 最大约 1.241；
- return 中满足 `second>=0.40` 时 peak 最大约 2.155；
- pitch-only 中对应最大约 1.033；
- clear-yaw / return / pitch 的 coherent vertical 最大约 0.075 / 0.121 / 0.093。

因此 peak=2.40 对最接近的正常 return 扰动仍保留约 0.245 的实测余量；而 2.20 只剩约 0.045，证据下不值得为少量额外负样本收益牺牲安全余量。vertical=0.35 仍远高于所有正常头动冻结窗口。

## 五组冻结真人头控回归

- 39997：changed 0
- 30dd：changed 0
- af4e：changed 0
- ed114：changed 0
- 6d210：changed 0

五个视频所有帧输出完全一致。额外检查 transient eligibility：clear-yaw 0 帧、return 0 帧、pitch-only 0 帧；新增资格只落在 `6d210` 的 front-facing + large-arm-motion 安全负样本。

## 工程验证

- `pytest -q tests/test_body_motion_head_guard.py`：14 passed
- `py_compile`：pass
- `git diff --check`：pass

## 累计相对 C1

- 全视频非零帧：622 -> 448（-27.97%）
- 全视频绝对输出：28831.490 -> 21175.411（-26.55%）
- 动作窗口非零帧：181 -> 99（-45.30%）
- 动作窗口绝对输出：8366.406 -> 4739.836（-43.35%）
- 前 50/100/150 ms：12/20/32 -> 10/15/18。

## 边界

仍缺“强身体运动 + 同时故意持续 yaw”的冻结真人联合真值。C2.3 不宣称解决 mixed-intent，也不继续延长 67 ms bridge。
