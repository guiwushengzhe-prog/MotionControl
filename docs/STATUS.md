# MotionControl 状态 — 2026-09-05 C2.7 body-guard internal timing normalized

## 当前晋级候选
- 产品来源基线：`d8d1ffc702b1b5685eab65b8291951024f6d4dcf`
- C1：`be1b3cc0f71907628d23077b44815871552be288`
- C2.1：`a78b3e37f882fe6efdf0eb59501d51edbf1a78ad`
- C2.2：`84bbc880b30f65ee84b2045ffd31a5c9ac16eacf`
- C2.3：`155ddf937d2e4bec86364005d4ed1d84ec99a48e`
- C2.4：`c54f54deb0cf5a41ac4033ba44ef3e71ed044957`
- C2.5：`4ac5d69af5aaa6210a8bb1e4524cfc8cf9ec5840`
- C2.6：`f8afc3f81a817a4557625703ed2780dd65bb375c`
- 当前 C2.7：`34bc7e8fee0c9cb5d7c3b66d881840ce244bc5d3`
- GitHub 分支：`head-body-guard-time-normalized-c2-7-20260905`
- 结论：PROMOTE C2.7（独立分支，未合并产品主线）

## C2.7 核心改动
C2.7 不增加新的身体动作抑制规则，只把 C2.4–C2.6 中残留的 body-guard 内部帧计数语义改为单调时钟语义：
- distal/segment chain：`2 帧` -> `30 ms`；
- strong early burst：`4 帧` -> `95 ms`；
- persistent guard 回零 settle：`3 帧` -> `60 ms`。

67 ms transient bridge、100 ms post-burst token TTL、raw/EMA、persistent guard 阈值/hold、C2.5 distal 阈值、C2.6 segment 阈值全部保持不变。

## 30 FPS 真人精确回归
C2.6 -> C2.7，同进程、同 Pose Full 输入：
- 8522：X changed 0；persistent guard changed 0；score changed 0。
- 39997：X changed 0；guard changed 0；score changed 0。
- 30dd：X changed 0；guard changed 0；score changed 0。
- af4e：X changed 0；guard changed 0；score changed 0。
- ed114：X changed 0；guard changed 0；score changed 0。
- 6d210：X changed 0；guard changed 0；score changed 0。

因此 C2.6 已验证的 30 FPS 抑制收益与 clear-yaw / pitch / return 保留逐帧不变。

## 多 FPS 内部时间语义
实测 sampled delay：

| FPS | C2.6 distal | C2.7 distal | C2.6 strong burst | C2.7 strong burst | C2.6 settle | C2.7 settle |
|---:|---:|---:|---:|---:|---:|---:|
| 20 | 50.0 ms | 50.0 ms | 150.0 ms | 100.0 ms | 100.0 ms | 100.0 ms |
| 30 | 33.3 ms | 33.3 ms | 100.0 ms | 100.0 ms | 66.7 ms | 66.7 ms |
| 45 | 22.2 ms | 44.4 ms | 66.7 ms | 111.1 ms | 44.4 ms | 66.7 ms |
| 60 | 16.7 ms | 33.3 ms | 50.0 ms | 100.0 ms | 33.3 ms | 66.7 ms |

C2.6 的这些内部路径在 45/60 FPS 会明显提前；C2.7 把它们恢复到接近 30/95/60 ms 的时间尺度，只剩采样量化误差。

## 仍存在的外部 FPS 依赖
`motion_active` 仍来自现有动作分类器，而动作分类器使用帧计数 debounce；它也能启动 persistent body guard。C2.7 故意没有修改动作识别语义。

Shadow 删除 `motion_active` 对 body guard 的触发后，8522 最终 X 恰好 0 帧变化，但 persistent guard 有 115 帧状态差异，证明这条路径不是死代码，不能直接删除。因此 C2.7 是 body-guard 内部时间归一化，不是“全链路已经完全 FPS invariant”。

## 当前正式头控配置重验证
此前已对 d8 -> C2.6 在相同 `head_control.py` 下重新排名：
- 全视频非零 X：59 -> 20；
- 全视频绝对 X：1367.528 -> 452.418；
- 16 个冻结身体动作窗口非零 X：19 -> 3；
- 动作窗口绝对 X：319.226 -> 25.058。

C2.7 在这批 30 FPS 数据上逐帧等同 C2.6，所以以上当前配置收益全部保留。

## 工程检查
- `pytest -q tests/test_body_motion_head_guard.py`：24 passed
- `py_compile`：pass
- `git diff --check`：pass
- 本地工作树：clean

## 下一步
停止围绕历史 70 帧残差继续堆规则。当前配置下 16 个冻结身体动作窗口只剩 3 个非零 X 帧。后续若继续工程化，优先级有两项：
1. 补“强身体运动 + 同时故意持续 yaw”的冻结真人联合真值；
2. 只在不改变动作识别/游戏触发语义的前提下，研究 `motion_active` 到 body guard 的时间化适配层。

在上述证据缺口解决前，不再增加更长 hold、更低阈值或更多 veto。

## 未验证边界
仍缺“强身体运动 + 同时故意持续 yaw”的冻结真人联合数据。C2.7 不宣称 mixed-intent 已解决。
