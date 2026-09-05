# MotionControl C2.2 transient early hold 晋级报告

日期：2026-09-05

## 结论

PROMOTE C2.2。

C2.2 以已冻结 C2.1 `a78b3e37f882fe6efdf0eb59501d51edbf1a78ad` 为唯一基线，不调整 early evidence 阈值，不修改 C1 persistent guard。唯一新增：early evidence 出现后，把“输出层临时抑制资格”保持 67 ms，用于覆盖身体动作证据与头控误 X 之间约 1–2 帧的时序错位。

## 为什么是 67 ms

离线时间窗扫描比较了 0 / 33 / 50 / 67 / 75 / 100 / 133 / 150 ms。

- 33–50 ms：只能桥接约 1 个 30 FPS 帧，收益有限。
- 67 ms：约桥接 2 个 30 FPS 帧，仍保持五组真人头控全帧 0 输出变化。
- 100–150 ms：在现有数据上还能继续压误输出，但缺少“身体运动中故意持续 yaw”真人联合真值，继续扩大临时屏蔽窗口会增加未量化风险，因此不采用。

## C2.1 -> C2.2 增量收益

### 8522 全视频

| 指标 | C2.1 | C2.2 | 增量 |
|---|---:|---:|---:|
| 横向非零帧 | 502 | 472 | -30（-5.98%） |
| 横向绝对输出和 | 23745.966 | 22314.499 | -1431.467（-6.03%） |
| persistent guard 帧 | 635 | 635 | 完全相同 |

### 16 个 RGB 人工冻结身体动作窗口

| 指标 | C2.1 | C2.2 | 增量 |
|---|---:|---:|---:|
| 横向非零帧 | 122 | 107 | -15（-12.30%） |
| 横向绝对输出和 | 5834.564 | 5083.937 | -750.627（-12.86%） |
| persistent guard 帧 | 165 | 165 | 完全相同 |

### RGB 人工动作起点

| 时间窗 | C2.1 | C2.2 |
|---|---:|---:|
| 前 50 ms 非零帧 | 10 | 10 |
| 前 100 ms 非零帧 | 16 | 16 |
| 前 150 ms 非零帧 | 22 | 20 |

C2.2 的作用不是把人体动作识别再提前，而是覆盖 early evidence 之后紧随出现的头控误输出，所以前 50/100 ms 不应被强行优化。

## 相对 C1 的累计收益

- 全视频非零帧：622 -> 472（-24.12%）
- 全视频绝对输出：28831.490 -> 22314.499（-22.60%）
- 动作窗口非零帧：181 -> 107（-40.88%）
- 动作窗口绝对输出：8366.406 -> 5083.937（-39.23%）
- 前 50/100/150 ms：12/20/32 -> 10/16/20

## 五组冻结真人头控回归

C2.1 -> C2.2：

- 39997：changed 0
- 30dd：changed 0
- af4e：changed 0
- ed114：changed 0
- 6d210：changed 0

即五个视频所有帧输出完全一致。冻结 scorable 窗口当然也全部为 0 变化：neutral_static / pitch_only / return / clear_yaw 均无新增损失。

## 状态机安全性

67 ms hold 只存在于输出层 transient suppression：

- 不设置 `body_motion_guard_active`；
- 不设置或续期 persistent hold；
- 不改变 raw / EMA；
- 不改变 motion_active；
- 不改变 settle / recovery；
- C1 persistent guard 序列逐帧完全相同。

因此 C2.2 不复活早期实验 C2 的 45% guard 占用问题。

## 工程验证

- `python -m pytest -q tests/test_body_motion_head_guard.py`：12 passed
- `python -m py_compile control_kernel.py tests/test_body_motion_head_guard.py`：通过
- `git diff --check`：通过

## 边界

仍未验证“强身体运动 + 同时故意持续 yaw”。因此 67 ms 是当前证据下的保守上限，不继续扩到 100–150 ms。
