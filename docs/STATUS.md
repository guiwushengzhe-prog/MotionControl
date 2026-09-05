# MotionControl 状态 — 2026-09-05 C2.5 body-motion head guard

## 当前晋级候选
- 产品来源基线：`d8d1ffc702b1b5685eab65b8291951024f6d4dcf`
- C1：`be1b3cc0f71907628d23077b44815871552be288`
- C2.1：`a78b3e37f882fe6efdf0eb59501d51edbf1a78ad`
- C2.2：`84bbc880b30f65ee84b2045ffd31a5c9ac16eacf`
- C2.3：`155ddf937d2e4bec86364005d4ed1d84ec99a48e`
- C2.4：`c54f54deb0cf5a41ac4033ba44ef3e71ed044957`
- 当前 C2.5：`4ac5d69af5aaa6210a8bb1e4524cfc8cf9ec5840`
- GitHub 分支：`head-body-guard-distal-chain-c2-5-20260905`
- 结论：PROMOTE C2.5（独立分支，未合并产品主线）

## C2.5 核心改动
C2.4 的全局 early thresholds、67 ms output-only bridge、bounded post-burst veto、C1 raw/EMA/persistent guard/recovery 全部不变。

新增“同侧远端关节连续运动”证据，只进入 transient output suppression：
- 仍要求 >=8 个共同速度点；
- 手臂：同侧 wrist 归一化速度连续 2 帧 >=1.20，同时 elbow 每帧 >=0.10；
- 腿：同侧 ankle 连续 2 帧 >=1.35，同时 knee 每帧 >=0.10；
- 单帧远端 spike 不触发；
- 没有近端支持不触发；
- 不能直接启动/续期 persistent guard。

这个证据补的是 C2.4 的结构盲区：腿后摆/交叉手等动作常由 wrist/ankle 高速运动主导，而 elbow/knee 只轻微移动，因此全局 second-fastest 门会漏掉。

## 真人 A/B
C2.4 -> C2.5，8522 全视频：
- 非零 X：441 -> 380（-13.83%）
- 绝对 X：20930.414 -> 18151.743（-13.28%）
- persistent guard：635 -> 635，完全一致

16 个 RGB 人工冻结动作窗口：
- 非零 X：94 -> 78（-17.02%）
- 绝对 X：4564.839 -> 3823.951（-16.23%）
- sign switches：8 -> 8
- 前 50/100/150 ms：10/15/18 -> 8/12/13

此前难处理窗口开始出现明确收益：
- calf_back_03：22 -> 21
- calf_back_04：21 -> 18
- hands_cross_03：11 -> 8
- cross_knee_elbow_02：11 -> 6
- cross_knee_elbow_03：2 -> 0

累计相对 C1：
- 全视频非零帧：622 -> 380（-38.91%）
- 全视频绝对输出：28831.490 -> 18151.743（-37.04%）
- 动作窗口非零帧：181 -> 78（-56.91%）
- 动作窗口绝对输出：8366.406 -> 3823.951（-54.29%）
- RGB 起点前 50/100/150 ms：12/20/32 -> 8/12/13

五组冻结真人 C2.4/C2.5 完整同进程 A/B：39997 / 30dd / af4e / ed114 / 6d210 的 X changed、persistent guard changed、score changed 均为 0。

`6d210` 新增 transient evidence 只落在已冻结的 `front-facing head + large arm motion` neutral window及 excluded setup/end；clear-yaw、return、pitch-only 都没有新增 evidence。

工程检查：
- `pytest -q tests/test_body_motion_head_guard.py`：19 passed
- `py_compile`：pass
- `git diff --check`：pass

## 下一步
C2.5 后 16 个动作窗口仍有 78 个非零帧。最大残差仍集中在 calf_back_03/04、hands_cross_03，以及部分 cross_knee_elbow_02。下一阶段先分析这些剩余帧与 distal evidence 的时间错位/周期相位，不继续直接降全局阈值或延长 blanket hold。

## 未验证边界
仍缺“强身体运动 + 同时故意持续 yaw”的冻结真人联合数据。C2.5 仍只证明纯身体运动误晃抑制和现有 clear-yaw/pitch/return 不退化，不宣称 mixed-intent 已解决。
