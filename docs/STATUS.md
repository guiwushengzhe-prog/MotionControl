# MotionControl 状态 — 2026-09-05 C2.2 body-motion head guard

## 当前晋级候选
- 产品来源基线：`d8d1ffc702b1b5685eab65b8291951024f6d4dcf`
- C1 本地 commit：`be1b3cc0f71907628d23077b44815871552be288`
- C2.1 本地 commit：`a78b3e37f882fe6efdf0eb59501d51edbf1a78ad`
- 当前 C2.2 本地 commit：`84bbc880b30f65ee84b2045ffd31a5c9ac16eacf`
- GitHub 分支：`head-body-guard-transient-hold-c2-2-20260905`
- 结论：PROMOTE C2.2（独立分支，未合并产品主线）

## C2.2 核心改动
保持 C2.1 的 early-evidence 阈值和 C1 persistent guard 全部不变。唯一新增：early evidence 出现时记录一个 67 ms 的 output-only transient suppression 窗口，用来桥接身体运动证据与头控误 X 之间约 1–2 个 30 FPS 帧的时序错位。

67 ms transient bridge：
- 不设置 `body_motion_guard_active`；
- 不续 persistent hold；
- 不修改 raw / EMA / motion_active；
- 不修改 settle / recovery；
- persistent guard 序列与 C1/C2.1 逐帧完全一致。

不采用 100–150 ms：现有离线数据虽然还能继续降低误输出，但缺“强身体运动 + 同时故意持续 yaw”的冻结真人联合真值，扩大临时屏蔽时间会增加未量化的合法 yaw 风险。

## 真人 A/B
C2.1 -> C2.2，8522 全视频：
- 横向非零帧：502 -> 472（-5.98%）
- 横向绝对输出和：23745.966 -> 22314.499（-6.03%）
- persistent guard：635 -> 635（逐帧一致）

C2.1 -> C2.2，16 个 RGB 人工冻结身体动作窗口：
- 非零帧：122 -> 107（-12.30%）
- 绝对输出和：5834.564 -> 5083.937（-12.86%）
- persistent guard：165 -> 165

相对 C1 的累计收益：
- 全视频非零帧：622 -> 472（-24.12%）
- 全视频绝对输出：28831.490 -> 22314.499（-22.60%）
- 动作窗口非零帧：181 -> 107（-40.88%）
- 动作窗口绝对输出：8366.406 -> 5083.937（-39.23%）
- RGB 动作起点前 50/100/150 ms 非零帧：12/20/32 -> 10/16/20

五组冻结真人 Pose，C2.1 -> C2.2：
- 39997：全视频 changed 0
- 30dd：全视频 changed 0
- af4e：全视频 changed 0
- ed114：全视频 changed 0
- 6d210：全视频 changed 0

因此 neutral_static / pitch_only / return / clear_yaw 全部无新增输出差异。

工程检查：
- `pytest -q tests/test_body_motion_head_guard.py`: 12 passed
- `py_compile`: pass
- `git diff --check`: pass

## 当前残差
目前 16 个动作窗口里两个重复动作几乎完全没有从 C2.1/C2.2 获益：
- `calf_back_03`：22 个非零帧，abs X 1033.214；
- `hands_cross_03`：11 个非零帧，abs X 520.327。

下一步先解释这两个窗口为什么没有形成可用 early evidence，重点看：speed_count/core visibility、peak/second 速度、coherent vertical、raw/EMA、证据与 X 的时间关系。禁止为了两个单独重复动作直接降低全局阈值或继续拉长 transient hold。

## 未验证边界
仍缺“强身体运动 + 同时故意持续 yaw”的冻结真人联合数据。C2.2 只证明纯身体动作误晃抑制有净收益，以及现有静止/普通 clear-yaw/pitch/return 真人数据不退化；不能宣称运动中主动 yaw 已解决。
