# MotionControl 状态 — 2026-09-05 C2.1 body-motion head guard

## 当前晋级候选
- 产品来源基线：`d8d1ffc702b1b5685eab65b8291951024f6d4dcf`
- C1 本地 commit：`be1b3cc0f71907628d23077b44815871552be288`
- 当前 C2.1 本地 commit：`a78b3e37f882fe6efdf0eb59501d51edbf1a78ad`
- GitHub 分支：`head-body-guard-transient-early-c2-20260905`
- 结论：PROMOTE C2.1（独立分支，未合并产品主线）

## C2.1 核心改动
C1 的 raw/EMA/action persistent guard 完全保留。新增两类只作用于当前帧的 early evidence：

1. 局部肢体启动：>=8 共同速度点，peak >= 2.60 且 second >= 0.50。
2. 躯干整体垂直运动：>=8 共同速度点，肩中心与髋中心同向垂直速度较小值 >= 0.50。

Early evidence 只对当前横向输出归零，不设置 active/hold，不续期，也不改变 C1 的 EMA、迟滞、settle 或 recovery。因此 C2.1 能抢动作开头，但不会把 guard 占用拉长。

## 真人 A/B
8522 全视频，相对 C1：
- 横向非零帧：622 -> 502（-19.29%）
- 横向绝对输出和：28831.490 -> 23745.966（-17.64%）
- 方向切换：48 -> 32
- persistent guard 帧：635 -> 635（逐帧一致）

16 个 RGB 人工冻结身体动作窗口：
- 非零帧：181 -> 122（-32.60%）
- 绝对输出和：8366.406 -> 5834.564（-30.26%）
- 方向切换：13 -> 8
- persistent guard：165 -> 165

独立 RGB 人工动作起点：
- 前 50 ms：12 -> 10 非零帧
- 前 100 ms：20 -> 16
- 前 150 ms：32 -> 22

这是本轮第一次在真实人工动作起点 50/100/150 ms 上得到改善。

五组冻结真人 Pose 完整回归：
- neutral_static 798 帧：changed 0
- pitch_only 1518 帧：changed 0
- return 582 帧：changed 0
- clear_yaw 450 帧：changed 0
- 五个视频所有帧合计：changed 0

工程检查：
- `pytest -q tests/test_body_motion_head_guard.py`: 11 passed
- `py_compile`: pass
- `git diff --check`: pass

## 当前边界
仍缺“强身体运动 + 同时故意持续 yaw”的冻结真人联合数据。因此不能宣称 C2.1 已解决运动中主动转头保留，也不应仅凭 8522 继续机械降低 early thresholds。

## 下一步
1. C2.1 作为当前已确认净收益回退点冻结，不覆盖。
2. 阈值 sweep 只作为 C2.2 探索；除非在现有冻结真人回归零退化之外还能获得足够大的跨窗口净收益，否则不晋级。
3. 优先补采“身体运动 + 故意左右转头”的真人联合数据；之后再研究 head-relative-to-torso yaw evidence/override，而不是简单软放行整个 body guard。
