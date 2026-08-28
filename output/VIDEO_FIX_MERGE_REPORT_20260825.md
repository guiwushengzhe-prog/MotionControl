# v0.9.7-dev VIDEO-FIX 语义合并记录

日期：2026-08-25

## 输入与边界

- 修复包：`MotionControl-v0.9.7-dev-VIDEO-FIX-HANDOFF-20260825.zip`
- 本地包路径：`G:\我的云端硬盘\MotionControl\v0.9.6-performance-command-transport\MotionControl-v0.9.6-CHATGPT-FINAL\v0.9.7-dev-20260825\MotionControl-v0.9.7-dev-VIDEO-FIX-HANDOFF-20260825.zip`
- SHA256：`846627949DA965945D08444F5B032F31150FA29FD9D84286A366CFA5FA72ED39`
- 解压对照目录：`C:\Users\Lenovo\AppData\Local\Temp\mc-v097-video-fix-20260825-02\MotionControl-v0.9.7-dev-VIDEO-FIX-HANDOFF-20260825`
- 当前工作树 `F:\MotionControl-App` 作为权威；没有用 `PROPOSED_FILES` 整体覆盖。

## 合并内容

- `head_control.py`：加入回正 `RETURNING` 锁存和原始信号稳定条件；回正期间输出保持零，运动稳定后才允许再次转向。
- `control_kernel.py`：双手交叉改为前臂内收、腕部顺序、胸前距离和高度条件；腿交叉以抬起脚踝跨过中线为主，膝只要求向内。另加入门控重新打开后的 3 帧滤波中心采样，避免滤波余波被误当成新上下动作。
- `game_profiles.py`、`server.py`、`steam_vdf.py`、`steaminput_builder.py`：绑定顺序为 Zone → 连续动作 → 离散动作 → 语音；只有明确语义才进入连续动作，无法确定的功能进入中性语音槽，不猜裸键语义。
- `config/voice_commands_v094.json`、`config/generated_voice/*`、`web/app.js`：增加 12 个语音功能槽，生成词库共 39 条。第 10–12 槽使用“体感功能十号/十一号/十二号”，避开 KWS 前缀冲突。
- `tools/replay_pose_video.py`：正式 Full 模型视频回放工具，使用空输出端，不发送鼠标、键盘或手柄，并记录校准与控制状态。
- 新增定向测试：`tests/test_video_fix_bundle_v097.py` 及相关 v0.9.7 测试文件。

## 验证

- Python 语法、JavaScript 语法、`git diff --check`：通过。
- 定向测试：51 passed。
- 正式测试目录：124 passed，19 skipped，0 failed。
- 语音注册表验证：39 条，生成词库 39 行。

### 视频回放

视频：修复包内 `reference_video\test_video.mp4`，2252 帧，30 FPS，28 帧没有有效姿态。

模型：`I:\MotionControl-Pose-Models\models\mediapipe\pose_landmarker_full_compatible_075.task`，SHA256 `5134A3AAD27A58B93DA0088D431F366DA362B44E3CCFBE3462B3827A839011B1`。

报告：

- 默认从 0 秒启动校准：`output\video-replay-v097-video-fix-20260825.json`。视频开头没有足够稳定的中心段，校准在有限时限后安全回退默认，未伪造头控结果。
- 用视频 4 秒处稳定段启动校准：`output\video-replay-v097-video-fix-20260825-cal04.json`。校准成功；40–64 秒头控段中，`RETURNING` 共 418 帧，期间非零 X 输出为 0；非零 X 输出只出现在 `TURN_LEFT`/`TURN_RIGHT`，分别为负/正方向。该段共有 20 个非零 X 样本，最大幅度 50。

重点区间（`cal04`）：

- 23.5–30.5 秒：检测到 `left_leg_cross_right` 4 次，`calf_back` 34 帧；本段没有检测到 `right_leg_cross_left`。
- 34.5–38 秒：检测到 `hands_cross` 10 帧。
- 40–64 秒：检测到头部 yaw 状态和回正锁存；`RETURNING` 状态不产生 X 输出。
- 66.5–68.5 秒：检测到 `hands_up` 43 帧。
- 本视频回放中 `lookGate` 没有进入有效状态，因此 head-pitch 的 Y 输出为 0；这表示该视频没有提供“左手进入门控并做上下动作”的可评估样本，不把它伪称为上下真人验收。

## 当前服务

- 源码服务：`F:\MotionControl-App\server.py`
- Python：`F:\MotionControl\MediaPipe\.venv\Scripts\python.exe`
- URL：`http://127.0.0.1:8765/`
- 页面已打开。
- 摄像头：关闭；输出：关闭。
- 服务 API 版本：`0.9.7-dev`；模型路径为上面的 Full 兼容模型。

## 未覆盖边界

视频回放不是用户真人验收；没有启动摄像头、游戏输出或真实动作测试。尤其上下头控需要用户在页面中进入 lookGate 后真人复测。
