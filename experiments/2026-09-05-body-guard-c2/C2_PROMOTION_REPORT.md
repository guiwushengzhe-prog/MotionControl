# MotionControl C2 transient early-evidence 晋级报告

日期：2026-09-05

## 结论

PROMOTE C2.1（transient early evidence）。

本候选以已晋级 C1 `be1b3cc` 为唯一代码基线。C1 的持续 body-motion guard 状态机保持不变；新增证据只在当前帧做横向输出抑制，不获得持续 guard 生命周期所有权。

## 设计

新增两类早期身体运动证据，均要求至少 8 个共同速度关键点：

1. 局部肢体启动：最快关键点速度 >= 2.60，同时第二快关键点速度 >= 0.50。
2. 躯干整体垂直运动：肩中心与髋中心同向垂直速度的较小值 >= 0.50。

这些 early evidence 只作用于当前帧：

- 若 C1 persistent guard 已经 active，完全沿用 C1。
- 若 C1 persistent guard 未 active，但 early evidence 成立，则当前横向输出归零。
- early evidence 不设置 hold、不续期、不改变 EMA、raw、motion_active、settle/recovery。

因此 C2 不会再出现早期证据反复把 guard 拉长的问题。

## 8522 真人身体动作 A/B

输入：2559 帧真人 Pose Full；头控横向输入复用既有 8522 未门控 replay 的逐帧真实 X 序列。

### 全视频

| 指标 | C1 | C2.1 | 改善 |
|---|---:|---:|---:|
| 横向非零帧 | 622 | 502 | -19.29% |
| 横向绝对输出和 | 28831.490 | 23745.966 | -17.64% |
| 方向切换次数 | 48 | 32 | -33.33% |
| persistent guard 帧 | 635 | 635 | 完全相同 |

### 16 个 RGB 人工冻结身体动作窗口

| 指标 | C1 | C2.1 | 改善 |
|---|---:|---:|---:|
| 横向非零帧 | 181 | 122 | -32.60% |
| 横向绝对输出和 | 8366.406 | 5834.564 | -30.26% |
| 方向切换次数 | 13 | 8 | -38.46% |
| persistent guard 帧 | 165 | 165 | 完全相同 |

### 人工 RGB 动作起点之后

| 时间窗 | C1 非零帧 | C2.1 非零帧 | C1 绝对输出 | C2.1 绝对输出 |
|---|---:|---:|---:|---:|
| 前 50 ms | 12 | 10 | 526.206 | 469.330 |
| 前 100 ms | 20 | 16 | 875.460 | 732.277 |
| 前 150 ms | 32 | 22 | 1453.144 | 1045.858 |

这是本轮第一次在独立 RGB 人工动作起点的 50/100/150 ms 指标上得到真实改善，而不是只把 EMA 触发提前。

## 五组冻结真人头控回归

使用冻结 `evaluation_windows_v1.json`，C1 与 C2.1 完整 replay：

- neutral_static：798 帧，changed = 0
- pitch_only：1518 帧，changed = 0
- return：582 帧，changed = 0
- clear_yaw：450 帧，changed = 0
- 五个视频所有帧（包括非评分区）：changed = 0

因此在现有真人头控证据范围内：合法 yaw、pitch、return、neutral 均没有新增输出差异。

## 状态机安全性

C1 与 C2.1 的 persistent guard active/raw/EMA 序列逐帧一致。因此：

- guard 总占用不增加；
- hold 不增加；
- settle/recovery 不增加；
- 不引入新的 ON/OFF 抖动；
- 不引入新的恢复跳变机制。

## 测试

- `python -m pytest -q tests/test_body_motion_head_guard.py`：11 passed
- `python -m py_compile control_kernel.py tests/test_body_motion_head_guard.py`：通过
- `git diff --check`：通过

## 未验证边界

仍然没有冻结真人数据覆盖“强身体运动 + 同时故意持续左右转头”。因此 C2.1 只能晋级为：

- 纯身体动作误晃抑制：有明确净收益；
- 静止/普通 clear-yaw：无回归；
- 身体运动中主动 yaw 保留：仍未证明。

下一轮若继续，应优先采集这一缺口，而不是继续机械降低 early thresholds。
