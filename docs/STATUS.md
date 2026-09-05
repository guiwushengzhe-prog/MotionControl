# MotionControl 状态 — 2026-09-05 C2.3 body-motion head guard

## 当前晋级候选
- 产品来源基线：`d8d1ffc702b1b5685eab65b8291951024f6d4dcf`
- C1 本地 commit：`be1b3cc0f71907628d23077b44815871552be288`
- C2.1 本地 commit：`a78b3e37f882fe6efdf0eb59501d51edbf1a78ad`
- C2.2 本地 commit：`84bbc880b30f65ee84b2045ffd31a5c9ac16eacf`
- 当前 C2.3 本地 commit：`155ddf937d2e4bec86364005d4ed1d84ec99a48e`
- GitHub 分支：`head-body-guard-early-threshold-c2-3-20260905`
- 结论：PROMOTE C2.3（独立分支，未合并产品主线）

## C2.3 核心改动
C2.2 的 67 ms output-only transient bridge、C1 raw/EMA/persistent guard、motion_active、settle/recovery 全部不变。只调整 early detector：
- limb peak：2.60 -> 2.40
- second：0.50 -> 0.40
- coherent vertical：0.50 -> 0.35
- 仍要求 >=8 个共同速度点

参数不是继续追最低阈值：冻结 return 窗口中满足 second>=0.40 的 peak 最大约 2.155，因此 2.40 仍留约 0.245 实测余量；2.20 只剩约 0.045，不采用。clear-yaw / return / pitch 的 coherent vertical 最大约 0.075 / 0.121 / 0.093，仍显著低于 0.35。

## 真人 A/B
C2.2 -> C2.3，8522 全视频：
- 横向非零帧：472 -> 448（-5.08%）
- 横向绝对输出：22314.499 -> 21175.411（-5.10%）
- persistent guard：635 -> 635，逐帧一致

16 个 RGB 人工冻结动作窗口：
- 非零帧：107 -> 99（-7.48%）
- 绝对输出：5083.937 -> 4739.836（-6.77%）
- 前 50/100/150 ms：10/16/20 -> 10/15/18
- persistent guard：165 -> 165

累计相对 C1：
- 全视频非零帧：622 -> 448（-27.97%）
- 全视频绝对输出：28831.490 -> 21175.411（-26.55%）
- 动作窗口非零帧：181 -> 99（-45.30%）
- 动作窗口绝对输出：8366.406 -> 4739.836（-43.35%）
- RGB 起点前 50/100/150 ms：12/20/32 -> 10/15/18

五组冻结真人 Pose，C2.2 -> C2.3：
- 39997：全视频 changed 0
- 30dd：全视频 changed 0
- af4e：全视频 changed 0
- ed114：全视频 changed 0
- 6d210：全视频 changed 0

额外 transient eligibility 检查：clear-yaw 0 帧、return 0 帧、pitch-only 0 帧；新增资格只落在 `6d210` 的 front-facing + large-arm-motion 安全负样本。

工程检查：
- `pytest -q tests/test_body_motion_head_guard.py`: 14 passed
- `py_compile`: pass
- `git diff --check`: pass

## 当前残差与下一步
C2.3 后 16 个动作窗口仍有 99 个横向非零帧。最大残差集中在：
- `calf_back_03`：22 帧，abs X 1033.214；
- `calf_back_04`：21 帧，abs X 1036.0；
- `hands_cross_03`：11 帧，abs X 520.327；
- `cross_knee_elbow_02`：11 帧，abs X 599.0。

下一步不继续全局降阈值，也不拉长 67 ms。先按残差相对 early evidence 的时序拆解，研究短时刚体 yaw 趋势、动作重复周期/证据重入，以及是否存在可证明的 risk-only suppression；任何新增机制必须继续保持五组真人全视频 0 变化，并且不能把不稳定的 world-rigid 绝对角当真值。

## 未验证边界
仍缺“强身体运动 + 同时故意持续 yaw”的冻结真人联合数据。C2.3 不宣称 mixed-intent 已解决；在这组联合真值补齐前，不扩大长时屏蔽窗口。
