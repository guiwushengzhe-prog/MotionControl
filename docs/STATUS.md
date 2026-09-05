# MotionControl 状态 — 2026-09-05 C2.4 body-motion head guard

## 当前晋级候选
- 产品来源基线：`d8d1ffc702b1b5685eab65b8291951024f6d4dcf`
- C1：`be1b3cc0f71907628d23077b44815871552be288`
- C2.1：`a78b3e37f882fe6efdf0eb59501d51edbf1a78ad`
- C2.2：`84bbc880b30f65ee84b2045ffd31a5c9ac16eacf`
- C2.3：`155ddf937d2e4bec86364005d4ed1d84ec99a48e`
- 当前 C2.4：`c54f54deb0cf5a41ac4033ba44ef3e71ed044957`
- GitHub 分支：`head-body-guard-postburst-veto-c2-4-20260905`
- 结论：PROMOTE C2.4（独立分支，未合并产品主线）

## C2.4 核心改动
C2.3 的 early detector 阈值、67 ms output-only bridge、C1 persistent guard 全部不变。新增一个强 burst 后的有界一次性 veto：
- early evidence 必须连续 >=4 帧；
- burst 结束后 token 有效期 100 ms；
- 最多发 2 个 token；
- 67 ms bridge 先执行且不消耗 token；
- bridge 后每个真正非零 X 才消耗 1 token；
- 第三个非零 X 必须立即放行；
- persistent guard 一旦接管，token 立即清空。

因此它不是新的 blanket hold，额外潜在合法 yaw 损失被硬限制为每个强 burst 最多 2 个非零帧。

## 真人 A/B
C2.3 -> C2.4，8522 全视频：
- 非零 X：448 -> 441（-1.56%）
- 绝对 X：21175.411 -> 20930.414（-1.16%）
- sign switches：28 -> 28
- persistent guard：635 -> 635，raw/EMA/score/guard state 逐帧一致

16 个 RGB 人工冻结动作窗口：
- 非零 X：99 -> 94（-5.05%）
- 绝对 X：4739.836 -> 4564.839（-3.69%）
- sign switches：8 -> 8
- persistent guard：165 -> 165
- 前 50/100/150 ms：10/15/18 -> 10/15/18

累计相对 C1：
- 全视频非零帧：622 -> 441（-29.10%）
- 全视频绝对输出：28831.490 -> 20930.414（-27.40%）
- 动作窗口非零帧：181 -> 94（-48.07%）
- 动作窗口绝对输出：8366.406 -> 4564.839（-45.44%）
- RGB 起点前 50/100/150 ms：12/20/32 -> 10/15/18

五组冻结真人使用同一时间基准、同一进程 C2.3/C2.4 精确 A/B：39997 / 30dd / af4e / ed114 / 6d210 的 X changed、guard changed、score changed 均为 0。

工程检查：
- `pytest -q tests/test_body_motion_head_guard.py`：16 passed
- `py_compile`：pass
- `git diff --check`：pass

## 当前残差与下一步
C2.4 后 16 个动作窗口仍有 94 个非零帧。最大残差仍是 `calf_back_03`、`calf_back_04`、`hands_cross_03`、`cross_knee_elbow_02`。继续全局降 early 阈值或延长 blanket hold 已经不划算。

下一步优先研究：这些残差是否来自一种现有 early detector 没覆盖的运动形态，例如腿后摆的低幅持续相对速度/周期运动；必须找到在 clear-yaw / return / pitch 中有明显安全间隔的新证据才可进入 C2.5。

## 未验证边界
仍缺“强身体运动 + 同时故意持续 yaw”的冻结真人联合数据。C2.4 不宣称 mixed-intent 已解决。
