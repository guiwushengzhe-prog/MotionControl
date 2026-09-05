# MotionControl 状态 — 2026-09-05 C1 body-motion head guard

## 当前晋级候选
- 本地来源基线：`d8d1ffc702b1b5685eab65b8291951024f6d4dcf`
- 本地候选 commit：`be1b3cc0f71907628d23077b44815871552be288`
- 候选：raw-onset C1
- 结论：PROMOTE（独立分支，未合并产品主线）

## 改动
身体运动防晃保持原 EMA/迟滞维持逻辑，同时增加高置信度 raw fast-path：仅当可用共同速度点数 >= 8 且 raw >= 2.50 时允许立即起门。raw 只负责提前起门，不续 hold；退出仍走原 EMA/hold/settle/recovery。

## 真人 A/B
8522：
- 横向非零误输出：637 -> 622（-15，-2.35%）
- 横向绝对输出量：29607.49 -> 28831.49（-776，-2.62%）
- 16 个 RGB 人工动作窗口：185 -> 181 非零帧
- 触发延迟 P90：约 540 ms -> 473 ms
- 最大触发延迟：约 600 ms -> 534 ms
- 恢复延迟未变差

五组冻结真人 Pose：
- 24 个 clear-yaw 窗口正确方向积分保留率：1.0
- neutral-static：无新增差异
- pitch-only：无新增差异

工程检查：
- `pytest -q tests/test_body_motion_head_guard.py`: 7 passed
- `py_compile`: pass
- `git diff --check`: pass

## 明确边界
C1 没有改善 RGB 人工动作起点后的前 50/100/150 ms。当前瓶颈不是 EMA 本身，而是身体运动证据出现得晚。另缺“身体明显运动 + 同时故意持续 yaw”的冻结真人联合真值，因此下一阶段不能直接把二值屏蔽改成大幅软放行。

## 下一步
1. 从现有 8522 Pose33/world_pose 做动作前兆特征研究，重点比较躯干中心速度、肩髋刚性平移、四肢加速度/jerk、左右肢体相对躯干速度、可见度骤变等，寻找比当前 fastest-half velocity 更早但静止误触低的因果证据。
2. 只允许前兆信号负责“预起门/风险升高”，原 C1 EMA 继续负责稳定维持与恢复，避免降低 raw 阈值造成长期误门。
3. 单独收集并冻结“身体运动中故意左右转头”真人数据，再研究 yaw evidence override/soft attenuation；在此之前不宣称联合意图已解决。
4. 任何 C2 必须同时优于 C1 的动作起始漏帧，并保持五组冻结 clear-yaw/neutral/pitch 不退化，才可晋级。
