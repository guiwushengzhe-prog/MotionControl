# MotionControl head-control-v2 算法审查说明

本文描述当前 PC 本地内核的头控实现，供代码审查和后续真人验收使用。范围只包括 MediaPipe 33 点到头控信号、校准、鼠标/手柄输出，不包含手机实现、身体区域动作和语音。

## 1. 目标和边界

头控的产品语义是：正视时不输出，物理向左/向右转头控制水平视角，抬头/低头控制垂直视角；没有个人校准也必须能工作；一次校准不能无限等待或因为方向顺序失败而反复重启。

当前算法版本是 head-control-v2。旧的 head-face-v3、head-shoulder-v2 个人参数不会套用到 v2；检测到旧版本时使用新版默认值，并提示重新设置中心。

网页预览固定镜像只属于显示层。内核始终使用原始规范坐标；手机消息若 coordinates_mirrored=true，仅在 pose_map_from_message() 中把 x 反一次。来源（电脑/手机）不改变头控公式。

## 2. 输入点和有效性

control_kernel.py 的 MP_NAMES 按 MediaPipe 33 点顺序映射 canonical 名称：

| 部位 | MediaPipe 索引 | 用途 |
| --- | ---: | --- |
| 鼻 nose | 0 | yaw 两侧距离、pitch 垂直/深度信号 |
| 左眼/右眼 left_eye/right_eye | 2/5 | 优先脸部点对 |
| 左耳/右耳 left_ear/right_ear | 7/8 | 眼点不足时的替代点对 |

头控有效性由 _calibration_pose_diagnostics() → _point_has_xy() 判断：鼻子和一组完整的左右眼或左右耳必须存在，点的 score（优先 visibility/presence）至少 0.35，x/y 必须为有限数。双肩、双髋、手、脚不再是头控门槛；双肩只作为旧调用的兼容参数，不参与 v2 yaw/pitch。

pose_map_from_message() 仍要求消息中有完整 33 点列表，但低 visibility 的无关点不会让头控信号失效。姿态源断开或 watchdog 触发时，_clear_body_outputs_locked() 清空头控输出。

## 3. yaw（水平转头）

ControlKernel._yaw_signal() 对每一组可用的眼点对和耳点对计算鼻子到左右点的二维欧氏距离：

    dL = distance(nose, left_face_point)
    dR = distance(nose, right_face_point)
    yaw_pair = ln((dL + 0.0001) / (dR + 0.0001))
    yaw_raw = mean(yaw_pair for available eye/ear pairs)

如果鼻子或所有脸部点对无效，raw yaw 为 NaN，该帧不参与中心记录。_normalize_v2_yaw(raw, center, span) 将中心和默认/个人范围转换为物理语义：

    x = clamp((yaw_center - yaw_raw) / yaw_range, -1, +1)

因此 x < 0 是物理左，x > 0 是物理右。这个符号只在该函数出现一次；OutputManager.apply() 不再额外反号。

## 4. pitch（抬头/低头）

_face_geometry() 选择同一套脸部点对作为中点和尺度：

1. 双眼可用：face_width = distance(left_eye, right_eye)；
2. 仅双耳可用：face_width = distance(left_ear, right_ear) * 0.5；
3. face_scale = face_width * 5.0（HEAD_FACE_SCALE_MULTIPLIER=5.0）。

脸部中点同时带有 y 和 z。_pitch_signal() 的两个分量为：

    face_ratio = (nose.y - face_midpoint.y) / face_scale
    z_ratio    = (nose.z - face_midpoint.z) / face_scale   # z 非有限时取 0
    pitch_raw  = 0.7 * face_ratio + 0.3 * z_ratio

权重常量为 HEAD_PITCH_FACE_WEIGHT=0.70、HEAD_PITCH_Z_WEIGHT=0.30；每帧记录的 raw_pitch_face 和 raw_pitch_z 可在状态中查看。双髋消失不会使 pitch 变成 None，也不会切换到肩髋尺度。

_normalize_v2_pitch(raw, center, span) 为：

    y = clamp((pitch_raw - pitch_center) / pitch_range, -1, +1)

这里约定 y < 0 为抬头（Windows 鼠标上移），y > 0 为低头（Windows 鼠标下移），中心为 0。没有第二个隐藏的 y 反号。

## 5. 默认参数、死区、曲线和输出数值

默认参数在 DEFAULT_HEAD_PARAMS 中立即生效，不需要先校准：

| 参数 | 当前值 | 单位/含义 |
| --- | ---: | --- |
| yaw0 | 0.0 | yaw 原始信号中心 |
| yaw_range | 0.20 | raw yaw 半范围；约对应设计中的 ±15°，不是角度换算值 |
| pitch0 | 0.05 | pitch 原始信号中心 |
| pitch_range | 0.04 | raw pitch 半范围；约对应设计中的 ±10°，不是角度换算值 |
| deadzone_x/y | 0.08 | 归一化信号中心 8% 内不输出，可在 0–8% 调整 |
| gamma | 1.5 | 输出曲线，配置范围 1.0–3.0 |
| max_percent_x | 60 | 水平输出最大百分比 |
| max_percent_y | 45 | 垂直输出最大百分比 |
| HEAD_UPDATE_INTERVAL_S | 0.028 | 约 28 ms 一次头控输出 |
| watchdog | 0.30 | 超过 300 ms 没姿态即归零 |

_curve_axis() 先应用死区，再按 sign(value) * maximum * normalized**gamma 形成目标百分比。_filter_axis() 使用 alpha = 0.10 + 0.36 * sqrt(intensity) 的一阶平滑；这不是额外的方向变换。

鼠标模式中 OutputManager.apply(x/100, y/100) 使用 mouse_speed_x=600、mouse_speed_y=450（像素/秒近似值）和单调时钟 dt，保留小数残差后调用 Windows SendInput。因此最终符号是：+x 向右、-x 向左、+y 向下、-y 向上。手柄模式沿用同一语义，VX360Gamepad.set_right_stick() 只为 XInput 的 +Y 朝上约定做一次 y 转换。

## 6. 校准状态机

旧的“准备 + 正视/左/右/抬头/低头五段”已经取消。v2 只有一个中心记录阶段：

1. 默认状态：calibrated=true、calibrating=false、center_capture_pending=true，使用 DEFAULT_HEAD_PARAMS。第一次收到有效头部姿态会自动进入中心记录；用户也可点击“重新设置中心”或“立即设置中心”。
2. 开始：_start_center_capture_locked() 保存当前个人/默认 profile 作为 fallback，清空临时数组，设置 stage="center"、stage_label="设置中心"、stage_deadline=now+3.0，并将输出保持为 0。开始按钮不以摄像头或 pose 为前置条件。
3. 采样：_update_calibration_locked() 只采集 nose+眼/耳有效且 raw yaw/pitch 有限的帧。每两个有效帧之间累计有效时间，单次增量上限 0.20 s；短暂缺点只记录 stage_pause_reason/stage_missing_parts，不污染已收集样本。
4. 结束：单调时钟到 3.0 s 后调用 _finish_calibration_locked()。有效累计至少 1.0 s 且 yaw/pitch 中位数有限时成功，中心替换为中位数，按固定 raw range 推导左右/上下端点；profile 和诊断先写临时文件再原子替换。输出继续保持中性直到下一帧。
5. 不足/取消/断源：有效样本不足时回到原有个人 profile（若存在）或新版默认 profile；用户取消、来源切换、摄像头停止、watchdog、退出均调用 _abort_calibration_locked()，清除临时样本、归零输出，不会留下半套个人参数。
6. 手动中心：set_current_center() 将当前有限 raw yaw/pitch 立即设为个人中心并原子保存；没有有效姿态则返回错误，不改变旧参数。

诊断只保存摘要，不保存视频或整帧姿态：%LOCALAPPDATA%\\MotionControl\\calibration_diagnostics.jsonl，包括有效/无效帧数、暂停原因、缺失部位、yaw/pitch 分布和最终事件。

## 7. 旧失败记录与本次变化

修改前的真实摘要保存在 F:\\MotionControl-App\\output\\head-calibration-latest-real-failure-v2.json。旧五段流程曾记录：

    center pitch median = 0.053220265935754484
    up     pitch median = 0.058648042910811916
    down   pitch median = 0.05620967053444237
    up-center   = +0.005427776975057433
    down-center = +0.002989404598687885
    old pitch_order_correct = false

旧流程把方向标签顺序和最终“上下必须分居中心两侧”绑定，导致有效帧已经采到仍被 final check 否决，用户重复动作时又回到阶段流程。v2 不再采集方向锚点，也不做这个 final check；只记录一个稳定中心，默认范围负责后续连续输入。

## 8. 关键源码位置

- F:\\MotionControl-App\\control_kernel.py
  - ControlKernel.pose_map_from_message()：33 点映射与手机显示镜像一次性归一化。
  - ControlKernel._calibration_pose_diagnostics() / _point_has_xy()：头控最小输入门槛。
  - ControlKernel._yaw_signal()：raw yaw 对数距离比。
  - ControlKernel._face_geometry() / _pitch_signal()：脸部尺度与 70/30 pitch 融合。
  - ControlKernel._normalize_v2_yaw() / _normalize_v2_pitch()：中心、范围、方向和限幅的唯一归一化位置。
  - ControlKernel._start_center_capture_locked() / _update_calibration_locked() / _finish_calibration_locked()：3 秒中心状态机与原子回退。
  - ControlKernel._watch_loop()：300 ms watchdog 和断源释放。
- F:\\MotionControl-App\\output_backend.py
  - OutputManager.apply()：将百分比信号转换为 Windows 鼠标增量或手柄右摇杆。
  - VX360Gamepad.set_right_stick()：仅处理 XInput 的 y 轴坐标约定。
- F:\\MotionControl-App\\web\\app.js：只显示状态、倒计时和“重新设置中心/取消设置中心”，不参与姿态推理或输出时序。
- F:\\MotionControl-App\\web\\index.html：固定显示镜像和简化后的中心记录层。

## 9. 本轮验证边界

本轮只计划进行 Python/JavaScript 语法检查和 git diff --check，不使用合成姿态、不代替用户做真人摄像头/鼠标测试、不启动游戏或输出。后续真人验收应重点观察：默认状态是否能控制、首次中心记录是否约 3 秒完成、左右和上下是否符合直觉、姿态断流是否在 300 ms 内归零。
