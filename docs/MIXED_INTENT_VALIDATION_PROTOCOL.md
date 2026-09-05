# MotionControl mixed-intent 真人验证协议

日期：2026-09-05
适用候选：C2.8 `527604e465c48c0dd977c934bff32730bb4ce731`

## 目的
补齐当前唯一关键证据缺口：强身体运动期间，用户同时明确、持续地左右转头时，body-motion guard 是否错误删除合法 Mouse-X。

这不是继续调参的数据集。窗口必须先从原始 RGB 冻结，再运行 C2.8 replay；禁止根据算法输出重选窗口。

## 最小录制集
同一机位、普通 RGB、全身与脸同时清楚可见。优先 30 FPS；如果设备稳定支持，再额外录同一协议 60 FPS 作为工程 FPS 验证，但 60 FPS 不是第一轮必需。

每段开始先正视静止 3 秒，结束后正视静止 2 秒。

### A. 纯身体动作负样本
每种动作连续做 3 次，头尽量保持正视：
1. 下蹲 squat
2. 后摆腿 calf_back
3. 双手交叉 hands_cross
4. 提膝碰对侧肘 cross_knee_elbow

### B. mixed-intent 正样本
每种身体动作分别录以下组合，每项 2 次：
1. 动作持续过程中向屏幕右转头并保持约 0.6–0.8 秒，再回正
2. 动作持续过程中向屏幕左转头并保持约 0.6–0.8 秒，再回正
3. 身体动作刚开始时同步向右转头
4. 身体动作刚开始时同步向左转头

第一轮最小规模：4 种动作 × 4 个 mixed 条件 × 2 次 = 32 次 mixed-intent 事件，外加 12 次 body-only 事件。

## 动作幅度要求
- yaw 必须是肉眼明确的正常转头，不要只做极微角度。
- 身体动作要达到平时实际游戏动作幅度，不需要故意夸张。
- 转头期间不要刻意停掉身体动作；我们要验证的正是两种意图同时存在。
- 不需要真实游戏、键鼠或 Xbox 输出。

## RGB 先验冻结
只看原始 RGB，先人工标注以下半开区间 `[start_s, end_s)`：
- calibration / neutral
- body_only
- mixed_body_yaw_right
- mixed_body_yaw_left
- yaw_return_during_body
- excluded（遮挡、离开画面、启动/停止录像等）

mixed 窗口至少记录：
- body_motion_onset_s：第一帧明显主动身体动作
- yaw_onset_s：第一帧明显主动横向转头
- clear_yaw_start_s / clear_yaw_end_s：方向清楚且持续的区间
- yaw_return_start_s / yaw_return_end_s
- expected_mouse_x_sign：screen_right = +1；screen_left = -1

冻结窗口提交后不得因 C2.8 输出而改动。

## Pose 提取
必须使用与现有正式数据一致的 MediaPipe Tasks PoseLandmarker Full：
- model SHA-256: `5134A3AAD27A58B93DA0088D431F366DA362B44E3CCFBE3462B3827A839011B1`
- RunningMode = VIDEO
- num_poses = 1
- detection/presence/tracking confidence = 0.35
- 保存逐帧 Pose33 + world_pose
- 保留 frame_index、原始时间戳和真实漏检帧
- 禁止插值或补造 landmark

## C2.8 验收指标
### body-only
- 非零 Mouse-X 帧数 / 时间占比
- abs Mouse-X integral
- 50/100/150 ms onset leakage
- wrong-sign / sign switch
- persistent guard occupancy / recovery delay

### mixed-intent
- clear-yaw correct-sign coverage
- clear-yaw abs integral retention：相对同配置无 body guard
- first correct response latency
- in-action intermittency
- wrong-sign frame rate / integral
- body motion开始后到合法 yaw 首次被放行的额外延迟
- return 期间反向残留

## 晋级硬门槛
C2.8 当前先保持冻结，不因新数据自动改参数。

只有同时满足以下条件，才允许继续 C2.9：
1. body-only 防误晃没有明显倒退；
2. mixed clear-yaw 正确方向覆盖没有灾难性损失；
3. 正常幅度 mixed yaw 的输出积分原则上保留 >=90%；
4. 不能通过延长 blanket hold 或降低/抬高单一全局阈值来“修”某一条视频；
5. 如果 C2.8 本身已达到要求，直接停止算法迭代，不为了版本号继续改。

## 当前原则
当前正式头控配置下，现有 16 个冻结身体动作窗口只剩 3 个非零 X 帧。因此这批 mixed-intent 数据的优先级高于继续压 body-only 残差。
