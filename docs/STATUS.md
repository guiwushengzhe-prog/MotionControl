# MotionControl 状态 — 2026-09-05 C2.8 body-guard action-risk time normalization

## 当前晋级候选
- 产品来源基线：`d8d1ffc702b1b5685eab65b8291951024f6d4dcf`
- C1：`be1b3cc0f71907628d23077b44815871552be288`
- C2.1：`a78b3e37f882fe6efdf0eb59501d51edbf1a78ad`
- C2.2：`84bbc880b30f65ee84b2045ffd31a5c9ac16eacf`
- C2.3：`155ddf937d2e4bec86364005d4ed1d84ec99a48e`
- C2.4：`c54f54deb0cf5a41ac4033ba44ef3e71ed044957`
- C2.5：`4ac5d69af5aaa6210a8bb1e4524cfc8cf9ec5840`
- C2.6：`f8afc3f81a817a4557625703ed2780dd65bb375c`
- C2.7：`34bc7e8fee0c9cb5d7c3b66d881840ce244bc5d3`
- 当前 C2.8：`527604e465c48c0dd977c934bff32730bb4ce731`
- GitHub 分支：`head-body-guard-action-risk-time-c2-8-20260905`
- 结论：PROMOTE C2.8（独立分支，未合并产品主线）

## C2.8 核心改动
C2.8 不修改 `motion_active`、动作识别规则、动作 debounce 或任何游戏输出语义。

新增 body-guard 专用 `body_motion_action_risk`：复用同一批 raw motion 条件，但用时间去抖，避免 persistent body guard 继续继承帧率相关的动作分类触发时刻。

30 FPS 等效 timing：
- march：on 0 ms / off 30 ms；
- calf_back / squat / hands_up：on 60 ms / off 95 ms；
- jumping_jack / side_step_jack / cross_knee_elbow：on 30 ms / off 60 ms。

Body guard 的 action-derived persistent activation 从 `motion_active` 改为 `body_motion_action_risk`。C2.7 的 internal time normalization、raw/EMA、C2.5 distal、C2.6 segment、67 ms bridge、bounded post-burst veto 均保持不变。

## 30 FPS 真人精确回归
C2.7 -> C2.8，同进程 production module A/B：
- 8522：X changed 0；persistent guard changed 0；score changed 0。
- 39997：0 / 0 / 0。
- 30dd：0 / 0 / 0。
- af4e：0 / 0 / 0。
- ed114：0 / 0 / 0。
- 6d210：0 / 0 / 0。

因此现有 30 FPS 真人行为逐帧保留。

## 动作输出隔离
8522 全 2559 帧中，C2.7 与 C2.8 的 `motion_active` 集合 changed = 0。新 action-risk 只服务 body guard，不反馈游戏动作输出。

## 多 FPS timing
3/4-frame 类（squat/calf_back/hands_up）：
- 30 FPS：legacy on/off 66.7/100 ms；timed risk 66.7/100 ms；
- 60 FPS：legacy 会缩到 33.3/50 ms；timed risk 保持约 66.7/100 ms。

2/3-frame 类（jumping_jack/side_step_jack/cross_knee_elbow）：
- 30 FPS：33.3/66.7 ms；
- 60 FPS legacy：16.7/33.3 ms；timed risk：33.3/66.7 ms。

20/45 FPS 仅受采样量化影响，不再按帧率线性缩短。

## 被拒绝的替代方案
直接删除 `motion_active` 对 body guard 的作用被拒绝：8522 虽然最终 X 恰好 0 变化，但 persistent guard 有 115 帧状态变化，说明 action-derived risk 不是死路径，应该时间化而不是删除。

## 当前正式头控配置结果
C2.6 之后已经在相同正式 `head_control.py` 下重验证：
- 全视频非零 X：d8 59 -> C2.6 20；
- 16 个冻结动作窗口：19 -> 3；
- 动作窗口绝对 X：319.226 -> 25.058。

C2.7/C2.8 在现有 30 FPS 真人数据上逐帧保持 C2.6 行为，因此上述收益全部保留。

## 工程检查
- `pytest -q tests/test_body_motion_head_guard.py`：28 passed
- `py_compile`：pass
- `git diff --check`：pass
- 本地工作树：clean

新增硬测试确保：
- timed action-risk 能启动 persistent guard；
- `motion_active` 单独存在不再直接启动 body guard；
- 30 FPS transition frame 与 legacy debounce 一致；
- 45/60 FPS 不会缩短到过早帧数；
- clear-body 会清理 action-risk 状态。

## 下一步
body-guard 的主要 FPS 依赖已经从内部 transient/settle 与 action-derived trigger 两层拆掉，而动作识别/游戏触发语义完全保持。

当前继续堆防晃规则的收益已经很低：正式当前头控下 16 个冻结身体动作窗口只剩 3 个非零 X 帧。后续最高价值工作不是继续加 hold/veto，而是补“强身体运动 + 同时故意持续 yaw”的冻结真人联合真值；在此之前不再扩大 suppression。

## 未验证边界
仍缺“强身体运动 + 同时故意持续 yaw”的冻结真人联合数据。C2.8 不宣称 mixed-intent 已解决。
