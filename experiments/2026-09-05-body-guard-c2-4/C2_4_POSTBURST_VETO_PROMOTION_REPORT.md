# MotionControl C2.4 post-burst bounded veto 晋级报告

日期：2026-09-05

## 结论
PROMOTE C2.4。

基线为已冻结 C2.3 `155ddf937d2e4bec86364005d4ed1d84ec99a48e`。C2.4 不调整 C2.3 early detector 阈值，不改变 67 ms transient bridge，也不改变 C1 raw/EMA/persistent guard、motion_active、hold、settle 或 recovery。

唯一新增机制：若 early evidence 连续出现至少 4 帧，且随后 burst 结束，则进入一个 100 ms 的 post-burst 资格窗，发放 2 个一次性 veto token。普通 67 ms bridge 先执行且不消耗 token；bridge 结束后，只有真正出现非零横向输出时才消耗一个 token并屏蔽该帧。最多屏蔽 2 个非零帧，第三个非零帧必须立即放行。若 persistent guard 接管，token 立即清零。

## C2.3 -> C2.4 真人 A/B
- 8522 全视频横向非零帧：448 -> 441（-1.56%）
- 全视频绝对输出：21175.411 -> 20930.414（-1.16%）
- sign switches：28 -> 28
- persistent guard：635 -> 635；raw/EMA/guard state 逐帧一致

16 个 RGB 人工冻结动作窗口：
- 非零帧：99 -> 94（-5.05%）
- 绝对输出：4739.836 -> 4564.839（-3.69%）
- sign switches：8 -> 8
- persistent guard：165 -> 165

收益集中于：squat_01 7->5；squat_02 8->6；squat_03 1->0。

RGB 动作起点前 50/100/150 ms：10/15/18 -> 10/15/18。C2.4 不负责抢动作起点，只处理持续强 body evidence 结束后的 delayed head-jitter，因此保持不变符合设计。

## 五组冻结真人完整回归
同一绝对时间基准、同一进程并行执行 C2.3/C2.4：
- 39997：X changed 0；guard changed 0；score changed 0
- 30dd：X changed 0；guard changed 0；score changed 0
- af4e：X changed 0；guard changed 0；score changed 0
- ed114：X changed 0；guard changed 0；score changed 0
- 6d210：X changed 0；guard changed 0；score changed 0

因此冻结 clear-yaw、return、pitch-only、neutral-static 均无新增输出损失。

## 累计相对 C1
- 全视频非零帧：622 -> 441（-29.10%）
- 全视频绝对输出：28831.490 -> 20930.414（-27.40%）
- 动作窗口非零帧：181 -> 94（-48.07%）
- 动作窗口绝对输出：8366.406 -> 4564.839（-45.44%）
- 前 50/100/150 ms：12/20/32 -> 10/15/18

## 工程验证
- `pytest -q tests/test_body_motion_head_guard.py`：16 passed
- `py_compile`：pass
- `git diff --check`：pass
- local product commit：`c54f54deb0cf5a41ac4033ba44ef3e71ed044957`

## 未验证边界
仍缺“强身体运动 + 同时故意持续 yaw”的冻结真人联合真值。C2.4 没有解决 mixed-intent，但相较 blanket hold，它把新增潜在合法 yaw 损失硬限制为每个强 burst 最多 2 个非零帧。