# MotionControl 状态 — 2026-09-05 C2.6 body-motion head guard

## 当前晋级候选
- 产品来源基线：`d8d1ffc702b1b5685eab65b8291951024f6d4dcf`
- C1：`be1b3cc0f71907628d23077b44815871552be288`
- C2.1：`a78b3e37f882fe6efdf0eb59501d51edbf1a78ad`
- C2.2：`84bbc880b30f65ee84b2045ffd31a5c9ac16eacf`
- C2.3：`155ddf937d2e4bec86364005d4ed1d84ec99a48e`
- C2.4：`c54f54deb0cf5a41ac4033ba44ef3e71ed044957`
- C2.5：`4ac5d69af5aaa6210a8bb1e4524cfc8cf9ec5840`
- 当前 C2.6：`f8afc3f81a817a4557625703ed2780dd65bb375c`
- GitHub 分支：`head-body-guard-segment-articulation-c2-6-20260905`
- 结论：PROMOTE C2.6（独立分支，未合并产品主线）

## C2.6 核心改动
保留 C2.5 distal evidence、C2.4 的 67 ms bridge / bounded post-burst veto，以及 C1 raw/EMA/persistent guard/recovery。

新增“肢体段形变速度”证据，只进入 transient output suppression：
- 计算 wrist-elbow 或 ankle-knee 向量的逐帧变化速度，自动消掉整条肢体的刚性平移；
- 手臂段连续 2 帧 >=0.80；
- 腿段连续 2 帧 >=1.20；
- 同链近端关节速度必须 >=0.10；
- 仍要求 >=8 个共同速度点；
- 不能直接启动或续期 persistent guard。

冻结合法头动的两帧连续形变上界：left/right arm 约 0.224/0.188，left/right leg 约 0.671/0.797，因此晋级阈值仍保留明确安全间隔。rigid-limb translation 有独立负测试。

## 真人 A/B
C2.5 -> C2.6，8522 全视频：
- 非零 X：380 -> 355（-6.58%）
- 绝对 X：18151.743 -> 16813.304（-7.37%）
- persistent guard：635 -> 635，完全一致

16 个 RGB 人工冻结动作窗口：
- 非零 X：78 -> 70（-10.26%）
- 绝对 X：3823.951 -> 3367.943（-11.93%）
- sign switches：8 -> 8
- 前 50/100/150 ms：8/12/13 -> 8/12/13

主要新增收益：
- squat_01：5 -> 2
- calf_back_04：18 -> 14
- cross_knee_elbow_02：6 -> 5

五组冻结真人完整同进程 C2.5/C2.6 A/B：39997 / 30dd / af4e / ed114 / 6d210 的 X changed、persistent guard changed、score changed 均为 0。

`6d210` 新增 transient evidence 仍只落在冻结的 `front-facing head + large arm motion` neutral 区及 excluded 部分；clear-yaw、return、pitch-only 均无新增 evidence。

工程检查：
- `pytest -q tests/test_body_motion_head_guard.py`：20 passed
- `py_compile`：pass
- `git diff --check`：pass

## 累计相对 C1
- 全视频非零 X：622 -> 355（-42.93%）
- 全视频绝对 X：28831.490 -> 16813.304（-41.68%）
- 动作窗口非零 X：181 -> 70（-61.33%）
- 动作窗口绝对 X：8366.406 -> 3367.943（-59.75%）
- RGB 起点前 50/100/150 ms：12/20/32 -> 8/12/13

## 下一步
C2.6 后动作窗口仍有 70 个非零帧，主要集中在 calf_back_03（21）、calf_back_04（14）、hands_cross_03（8）等。下一阶段只研究剩余残差与已验证 evidence 的相位关系/周期结构；不再通过降低全局阈值或拉长 blanket hold 挤收益。

## 未验证边界
仍缺“强身体运动 + 同时故意持续 yaw”的冻结真人联合数据。C2.6 不宣称 mixed-intent 已解决。
