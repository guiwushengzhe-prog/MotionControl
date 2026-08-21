# MotionControl 0.8.1 头控算法审查说明

本文是给外部 AI 或代码审查者的自包含说明。范围只包括“MediaPipe 33 点 → 头部 yaw/pitch → Windows 鼠标 X/Y”和自动头控校准；不包含手机实现、身体区域动作、语音和游戏映射。

## 1. 目标和验收语义

产品目标不是让用户记住摄像头坐标的正负，而是让以下物理动作具有直觉结果：

| 人的动作 | 归一化输出 | Windows 鼠标结果 |
| --- | ---: | --- |
| 物理向左转头 | `x < 0` | `dx < 0`，光标向左 |
| 物理向右转头 | `x > 0` | `dx > 0`，光标向右 |
| 抬头 | `y < 0` | `dy < 0`，光标向上 |
| 低头 | `y > 0` | `dy > 0`，光标向下 |
| 正视/中心 | `x = 0, y = 0` | 不移动 |

网页预览可以固定镜像，但镜像只是显示层。控制内核使用规范化的未镜像坐标；手机若在 `pose_frame_v2` 中声明 `coordinates_mirrored=true`，PC 接收边界只把 x 反一次，电脑摄像头和 `false` 则不反。来源（电脑/手机）不改变控制公式。

自动校准不是使用头控的前提。启动时先使用默认参数；用户主动校准时，流程必须在有限时间内结束，不能因为 up/down 的最终检查失败而无限重启。

## 2. 输入和必需关键点

入口：`F:\MotionControl-App\control_kernel.py`。

- `ControlKernel.pose_map_from_message()` 将消息中的 33 点按 `MP_NAMES` 映射为 canonical 名称。
- 头控最小输入由 `_calibration_pose_diagnostics()` 和 `_point_has_xy()` 定义：
  - `nose`（MediaPipe 索引 0）可用，score/visibility/presence 至少 `0.35`，x/y 为有限数；
  - 左右眼组合（索引 2、5）优先，或左右耳组合（索引 7、8）作为替代；每一点同样至少 `0.35` 且 x/y 有限；
  - 双肩、双髋、手、脚都不是头控可用性的门槛。双肩只在兼容字段中保留旧的尺度采样，不再参与 yaw/pitch 计算。
- 其它 MediaPipe 点仍由整体姿态链保留，但低 visibility 不应阻塞头控校准。

## 3. raw yaw 计算（`head-face-v3`）

算法版本常量为 `HEAD_SIGNAL_VERSION = "head-face-v3"`。

对每一组可用脸部点对，计算鼻子到左右点的欧氏距离：

```text
dL = distance(nose, left_face_point)
dR = distance(nose, right_face_point)
yaw_pair = ln((dL + 0.0001) / (dR + 0.0001))
```

`_yaw_signal()` 会分别尝试双眼和双耳；可用的组取平均。若鼻子不可用、两组都不完整，返回 `NaN`，该帧不能作为校准有效样本，也不会向输出发送有效头控量。

这是一种相对距离信号，不把图像左右边界当作角度，不依赖肩宽或髋宽。镜像已经在消息入口最多处理一次，`_yaw_signal()` 不再额外反号。

## 4. raw pitch 计算（同一套脸部尺度）

`_face_geometry()` 先选择尺度：

1. 双眼均可用：`face_width = distance(left_eye, right_eye)`；
2. 双眼不可用但双耳均可用：`face_width = distance(left_ear, right_ear) * 0.5`（用耳距的一半近似眼距）；
3. 最终尺度：`face_scale = face_width * 5.0`，其中 `HEAD_FACE_SCALE_MULTIPLIER = 5.0` 是固定值。

脸部中点为选中左右点的中点。`_pitch_signal()` 使用：

```text
pitch = (nose.y - face_midpoint.y) / face_scale
```

这套定义从眼点切到耳点时仍保持同一“脸宽”尺度，避免一套公式突然换成另一套尺度。双髋消失不会让 pitch 变为 `None`；肩部也不再是 pitch 的必需输入。

## 5. 归一化、死区、曲线和滤波

### 5.1 三锚点分段归一化

`ControlKernel._normalize_axis(raw, center, left_anchor, right_anchor)` 只使用标签语义定义符号，不猜原始数值正负：

```text
delta = raw - center
left_delta  = left_anchor  - center
right_delta = right_anchor - center

若 raw 位于 center 与 left_anchor 同侧：
    normalized = -clamp(abs(delta / left_delta), 0, 1.5)
若 raw 位于 center 与 right_anchor 同侧：
    normalized = +clamp(abs(delta / right_delta), 0, 1.5)
```

对 yaw 调用时标签是 `left/center/right`；对 pitch 调用时同一函数传入 `up/center/down`，因此 `up`（抬头）标签固定得到负值，`down`（低头）标签固定得到正值。若旧 profile 的两个锚点落在同一数值侧，函数只做最近标签锚点的兼容回退，不在输出端再叠加全局反号。

### 5.2 默认锚点和个人锚点

当前代码默认值（`DEFAULT_HEAD_PARAMS`）：

```text
yaw0=0.00, yaw_left=-0.10, yaw_right=+0.10
pitch0=0.15, pitch_up=0.08, pitch_down=0.22
deadzone_x=0.08, deadzone_y=0.08
gamma=2.2
max_percent_x=60.0, max_percent_y=45.0
enabled=true, invert_x=false, invert_y=false
```

这些锚点是在没有个人 profile 时立即使用的安全默认值。五段校准成功的轴使用该轴各阶段有效样本的中位数；失败或超时的轴保留启动前的同版本个人值，若没有则使用上述默认值。旧 `head-shoulder-v2` profile 因版本不匹配会被忽略。

### 5.3 曲线与滤波实际数值

`_curve_axis()`：

```text
若 abs(normalized) <= deadzone：target = 0
否则 amount = (abs(normalized)-deadzone)/(1-deadzone)
target = sign(normalized) * max_percent * amount^gamma
```

启动默认 `gamma=2.2`；网页配置允许范围：deadzone_x `0.03..0.30`、deadzone_y `0.04..0.35`、gamma `1.4..3.2`、max_x `20..120%`、max_y `15..100%`。

`_filter_axis()` 是一阶平滑：

```text
intensity = clamp(abs(target)/max(1, max_percent), 0, 1)
alpha = 0.10 + 0.36 * sqrt(intensity)
filtered = current + alpha * (target-current)
```

如果归一化值回到死区，滤波输出立即归零。内核最短头控更新间隔为 `0.028 s`（约 35.7 Hz）。

## 6. Windows 鼠标最终输出和符号

最终路径是：

```text
ControlKernel._update_head_locked()
  -> OutputManager.apply(output_x / 100.0, output_y / 100.0)
  -> OutputManager.apply() 的 mouse 分支
  -> self.mouse.move(dx, dy)
```

`F:\MotionControl-App\output_backend.py` 的实际公式：

```text
dt = clamp(now - last_update, 0, 0.08)   # 首帧为 1/30 s
amount_x = x * mouse_speed_x * dt + residual_x
amount_y = y * mouse_speed_y * dt + residual_y
dx = int(amount_x)
dy = int(amount_y)
```

代码默认 `mouse_speed_x=600.0`、`mouse_speed_y=450.0`；配置范围分别为 `80..3000` 和 `60..2500`。运行时状态可能因用户设置不同（本次真实快照曾显示 960/720），这只改变幅度，不改变符号。

当前边界没有隐藏的 `-x` 或 `-y`。X 的左右语义在三锚点归一化中一次确定；Y 的抬头/低头语义同样在 `up/center/down` 锚点中一次确定。`invert_x/invert_y` 是用户显式设置项，默认均为 false，不属于镜像自动逻辑。

参考只读包 `C:\Users\Lenovo\Downloads\MotionControl-v0.7.3-body-zones-actions.zip` 的 `output_backend.py` 也直接发送 `amount_x=x*speed*dt`、`amount_y=y*speed*dt`，因此不能在最终输出边界再补一个反号。

## 7. 自动校准状态机

实现位置：`ControlKernel.start_calibration()`、`_advance_calibration_locked()`、`_update_calibration_locked()`、`_timeout_calibration_stage_locked()`、`_finish_calibration_locked()`。

### 7.1 阶段和时间

| 阶段 | 进入条件 | 有效样本要求 | 墙钟上限 | 成功动作 |
| --- | --- | ---: | ---: | --- |
| 准备 | 用户点击开始，完全不检查 source/pose | 不采样 | 5.0 s | 进入正视 |
| 正视 | 准备截止 | 累计 1.5 s，yaw 和 pitch 均有限 | 6.0 s | 固定中心样本，进入左转 |
| 左转 | 正视过渡 0.6 s | 累计 1.5 s 有效 yaw | 6.0 s | 固定左锚点 |
| 右转 | 左转过渡 0.6 s | 累计 1.5 s 有效 yaw | 6.0 s | 固定右锚点 |
| 抬头 | 右转过渡 0.6 s | 累计 1.5 s 有效 pitch | 6.0 s | 固定上锚点 |
| 低头 | 抬头过渡 0.6 s | 累计 1.5 s 有效 pitch | 6.0 s | 固定下锚点 |

准备和五个采样段的理论最大墙钟为 `5 + 5*6 + 4*0.6 = 37.4 s`；正常有效输入通常约为准备 5 秒 + 五段各 1.5 秒 + 过渡时间。

### 7.2 有效、暂停和超时

- 每帧先检查鼻、眼/耳组合和当前轴所需 raw 信号；缺点或信号非有限时，`stage_pause_reason`、`stage_missing_parts` 和诊断计数更新，累计有效时间暂停，不清除已经完成的阶段。
- 恢复有效后从当前 `stage_valid_s` 继续累计；不是按缺帧墙钟重置。
- 6 秒截止时，`_timeout_calibration_stage_locked()` 固化该阶段诊断，清掉该阶段未完成锚点，记录 yaw 或 pitch 超时，并进入 0.6 秒过渡。该轴标记为“已有个人值”或“默认值”，流程继续，不重新开始旧阶段。
- 用户取消、身体源切换、停止摄像头、watchdog 断源、紧急停止或退出调用 `_abort_calibration_locked()`：丢弃本次临时样本、恢复完整默认参数、输出归零，并留下取消原因。
- watchdog 和姿态新鲜度默认窗口均为 `0.30 s`（`watchdog_timeout`/`CALIBRATION_POSE_TIMEOUT_S`）；超过后身体输出清零，校准界面显示人体来源过期，但采样阶段仍受 6 秒墙钟约束，不会无限等待。

### 7.3 结束和原子保存

`_finish_calibration_locked()` 分别检查 yaw 与 pitch 的中位数锚点：

```text
finite = center/left(or up)/right(or down) 全有限
ordered = (left-center) * (right-center) < 0
separated = min(abs(anchor-center)) >= 0.0005
axis_ok = finite and ordered and separated
```

`CALIBRATION_MIN_AXIS_SEPARATION=0.0005` 是信号单位中的最小非零分离，不是像素或度数。某轴 `axis_ok` 时只替换该轴的个人中心/端点和依据中心噪声计算的死区；否则只回退该轴。最终提示明确写出“左右个人值/已有个人值/默认值、上下个人值/已有个人值/默认值”，不会再出现“全部阶段已完成但整体失败并重启”。

profile 通过临时文件写入 `%LOCALAPPDATA%\MotionControl\head_profile.json` 后 `os.replace` 原子替换；文件携带 `signal_version`，只加载同为 `head-face-v3` 的 profile。

## 8. 最新真人失败证据（修改前）

原始快照：`F:\MotionControl-App\output\head-calibration-latest-real-failure-v2.json`。它是在重启或改动前只读保存的现场，快照时输出开关为 false，所以没有可用于证明 Windows 光标移动方向的样本。

在快照中最近一次完整五段 final check 的关键中位数为：

```text
center pitch = 0.053220265935754484   (41 valid frames, invalid 0)
up pitch     = 0.058648042910811916   (40 valid frames, invalid 0)
down pitch   = 0.05620967053444237    (41 valid frames, invalid 0)

up-center   = +0.005427776975057433
down-center = +0.002989404598687885
minimum separation = 0.002989404598687885
threshold = 0.0005
pitch_order_correct = false
pitch_ok = false
```

因此失败点不是“没有有效帧”，也不是 visibility 让阶段停住，而是旧实现强制要求 up 与 down 分居 center 两侧；两者都在 center 上方，最终 `pitch_order_correct=false`。用户随后又开始了新的 down 阶段并取消：该段 26 个有效帧、0.798 秒、invalid 0。旧实现的全局失败/重启逻辑使用户感觉“抬头低头反复做也不推进”。

快照中的 yaw 同时满足顺序：center `0.0656870926357501`、left `0.11233034746900727`、right `-0.16938220598237713`，`yaw_order_correct=true`。这也说明旧问题集中在 pitch final check，不应通过改鼠标 X 或统一降低所有阈值来掩盖。

## 9. 本轮修改前后对照

| 项目 | 修改前（`head-shoulder-v2`） | 当前（`head-face-v3`） |
| --- | --- | --- |
| pitch 尺度 | 鼻/脸中点差 ÷ 双肩宽；髋缺失会间接使尺度无效 | 鼻/脸中点差 ÷（眼距或半耳距）× 5；肩髋不阻塞 |
| 必需点 | 鼻、锁定眼/耳、双肩 | 鼻、可用双眼或双耳；肩可选 |
| 阶段时序 | 1.5 秒有效，但 final check 失败会重启阶段，可能无限循环 | 1.5 秒有效 + 6 秒墙钟；轴级超时回退并继续 |
| final check | yaw/pitch 任一失败就整体失败 | yaw、pitch 分别决定个人值或回退值 |
| 个人参数 | 旧信号版本可能被错误复用风险 | 只加载 `head-face-v3`，旧版本自动回默认 |
| 鼠标 Y | 参考边界直接 `y*speed_y*dt` | 仍直接 `y*speed_y*dt`；up/down 标签决定负/正 |
| 输出边界反号 | 容易因散落 `-x/-y` 二次反向 | 没有自动反号；仅保留用户显式 invert 开关 |

## 10. 关键源码定位

- `F:\MotionControl-App\control_kernel.py`
  - 常量：`HEAD_SIGNAL_VERSION`、`HEAD_FACE_SCALE_MULTIPLIER`、校准时序常量、`DEFAULT_HEAD_PARAMS`。
  - 输入镜像：`ControlKernel.pose_map_from_message()`。
  - 关键点门槛：`_point_has_xy()`、`_calibration_pose_diagnostics()`。
  - 信号：`_face_geometry()`、`_yaw_signal()`、`_pitch_signal()`。
  - 归一化/曲线/滤波：`_normalize_axis()`、`_curve_axis()`、`_filter_axis()`。
  - 校准状态机：`start_calibration()`、`_begin_calibration_stage_locked()`、`_update_calibration_locked()`、`_advance_calibration_locked()`、`_timeout_calibration_stage_locked()`、`_finish_calibration_locked()`。
  - profile：`_load_head_profile_locked()`、`_persist_head_profile_locked()`。
  - 输出调用：`_update_head_locked()`。
- `F:\MotionControl-App\output_backend.py`
  - `OutputManager.apply()` 的 mouse 分支负责 dt、残差累积、`dx/dy` 和 `self.mouse.move(dx,dy)`，不负责理解“左转/右转/抬头/低头”。
- `F:\MotionControl-App\web\app.js`
  - `renderCalibrationOverlay()` 只显示内核返回的阶段、剩余墙钟、有效采样、暂停原因和信号值，不重新计算控制信号。
- 只读参考：`C:\Users\Lenovo\Downloads\MotionControl-v0.7.3-body-zones-actions.zip`。

## 11. 仍未知、必须由真人确认

1. 本轮没有替用户合成姿态、移动鼠标或运行摄像头；快照时输出关闭。因此必须由真人在真实电脑/手机姿态源上确认 up/down 的物理方向和灵敏度。
2. `head-face-v3` 的默认 pitch 锚点是与新尺度匹配的工程默认值，仍需要真人确认普通幅度抬头/低头不会被死区吃掉。
3. 用户显式打开 `invert_x/invert_y` 后可改变符号，这是配置语义，不应与显示镜像混淆。
4. 只有同版本 profile 才会加载；首次使用新版本或旧 profile 被忽略时，页面应显示默认参数，完成一次真实校准后再判断个人值是否更稳定。
