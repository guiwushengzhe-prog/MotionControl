# MotionControl 1.00 — Product Release

基于 2026-08-25 的 v0.9.6 快照继续开发。继续沿用 MediaPipe Full、头控、语音、场景区域和悬浮窗；本版新增离线 Game Profile（游戏配置）、统一输出动作、三种边沿姿势、Steam Input VDF 转换与 PC→手机当前配置同步。

## 核心能力：游戏配置与统一映射

运行时只预读很小的 `game_profiles/catalog.json`；真正的单游戏 Profile 只在选择该游戏时加载，因此几百个 Profile 不会拖慢启动。

- 六个原有身体区域继续保持默认 `Y / X / B / A / LB / RB`，旧配置可继续使用。
- 输出统一为 `keyboard / mouse_button / mouse_wheel / gamepad / gamepad_trigger / gamepad_axis`。
- 支持键盘组合键、鼠标左/右/中键与侧键、滚轮、Xbox A/B/X/Y、LB/RB、L3/R3、方向键、Start/Back、LT/RT、左摇杆四方向。
- 滚轮永远是一次脉冲，不允许因为身体停留在区域里而持续滚动。
- 新增双手交叉、右腿向左交叉、左腿向右交叉三个姿势；只在进入姿势的边沿触发一次，不作为持续按住输入。
- PC 是唯一映射决策端；手机只接收 `control_config_v1` 并展示当前游戏与六区最终映射，断线时保留上次缓存。

### 离线 Profile 库一键构建

Windows 直接运行：

```text
BUILD_GAME_LIBRARY.bat
```

流程为：Steam Windows Top Sellers 种子 → SteamInputDB 候选配置 → 下载 VDF → 确定性解析 → 紧凑 JSON Profile → 库审计。默认目标种子数为 500，但它只是目标：**没有“少于 300 个 Profile 就失败”的硬门槛**。网络中断或部分游戏没有可解析配置时，已经生成的 Profile 仍会保存并可正常使用。

构建器默认每 10 个游戏做一次 checkpoint（断点保存）。下次运行会跳过已经存在的有效 Profile，失败项会自动重试，因此可以分多次逐步积累库。

只重建/检查 6 个重点游戏：

```text
BUILD_PRIORITY_PROFILES.bat
```

当前重点集：Red Dead Redemption 2、GTA V Legacy、GTA V Enhanced、Forza Horizon 4、Forza Horizon 5、Black Myth: Wukong。

Profile 是否“真人验证”与自动构建质量分开记录。真人实际进入游戏验证后，可按 Steam AppID 登记，例如：

```text
python tools\mark_profile_verified.py 1293830 --note "已实测区域按键和菜单"
```

之后再次自动刷新同一 Profile，不会清掉这份人工验证记录。`tools\audit_game_profiles.py` 会生成 `game_profiles/audit_report.json`，分别报告 Profile 是否存在、标准六区覆盖、官方配置来源和真人验证状态；审计只报告问题，不阻断运行。

## 本地控制内核（正式运行边界）

浏览器现在只负责 UI（界面）、配置和状态/骨架展示；持续姿态推理、身体区域、四动作、头控、看门狗（超时保护）和游戏输出时序全部由本地 Python 服务维护。网页关闭、最小化或被游戏遮挡时，内核仍可继续接收输入并释放输出。

- 手机正常体感帧使用 `pose_features_v1 / mc25-v1`：只发送 25 个控制必需关键点的紧凑数据，不发送 33 点完整骨架、`world_pose` 或视频；PC 在进程内补成统一 33 点结构后继续走唯一的 Python 控制内核。旧 `pose_frame_v2` 仅保留兼容。
- 手机 `sensor_frame` 直接驱动手柄按钮/摇杆；断线或超过约 300 ms 没有新帧会由服务释放。
- 电脑摄像头由本地 Python OpenCV + MediaPipe Pose Full 采集和推理；浏览器不再承担电脑摄像头正式控制链，只显示服务返回的骨架。
- 身体源只能有一个（电脑摄像头或手机摄像头），手持传感器可以并行；切换由服务原子释放旧源。

## 主界面保留

- 本地 MediaPipe Pose Full（33 点）骨架状态展示
- 摄像头来源：电脑摄像头（本地 Python 推理）或手机摄像头（手机本地 MediaPipe 推理，仅发送紧凑 `pose_features_v1` 控制特征；电脑不接收/显示连续手机视频、不重复推理）
- 手机局域网连接状态与 `/ws/input` 地址提示
- 头控：鼠标 / Xbox 360 右摇杆，头部只控制水平左右；纵向只由左腕进入视角门后右腕上下控制
- 游戏速度倍率
- 身体相对区域触发状态
- 四个动作实时状态
- 语音状态与本地/手机来源提示
- 游戏悬浮窗
- F8 总输出开关 / F9 紧急停止

## 固定七区域与场景匹配

正式控制使用固定在摄像头坐标中的 7 个区域，而不是逐帧跟着人体移动。首次建立场景时，内核会从最近约 0.9 秒的多帧姿态取中位数快照，降低单帧抖动导致的区域偏移；如果仍不适合个人站位，主界面可直接拖动 7 个区域并保存。之后区域保持固定，只有用户明确执行“重新识别我的位置”才重新匹配。

默认 7 区：

- 左手：头顶/耳外两区 → `Y / X`
- 右手：头顶/耳外两区 → `B / A`
- 左脚外侧 → `LB`
- 右脚外侧 → `RB`
- 下巴左侧绿色“上下视角”区 → 无按键，只检测左腕进入/离开

前 6 区连续 2 帧进入才按下；视角门离开 1 帧即关闭纵向输出。视角门开启时只允许右腕相对右肩的上下位移控制 Y，右腕 X 坐标完全不参与纵向算法；同时冻结头部水平输出，避免抬手时轻微头部补偿造成斜向视角。

## 四个动作

“设置 → 四个动作与按键”里可独立启用并映射为：

- 键盘键/组合键
- Xbox 按键
- Xbox 左摇杆方向（如 `LS_UP`）

默认全部关闭，避免未经真人测试就误触。

当前规则：

- **原地踏步**：左右膝/踝出现明显交替抬高，左右事件间隔约 0.1–1.15 秒；连续交替后保持前进状态，约 0.7 秒没有下一次有效交替则结束。
- **小腿向后（左/右）**：任一侧膝角 `<115°`，同时该膝仍保持在髋部下方，踝向膝靠近；下蹲状态优先，不同时判定。
- **下蹲**：左右膝角都 `<135°`，并且髋到膝的垂直距离明显缩短。
- **双手举过头顶**：左右腕都高于鼻子，左右肘同时抬到肩附近以上。

动作结束会立即释放对应持续输入。动作状态由本地 Python 内核按单调时钟维护，超过约 300 ms 没有姿态帧时自动释放，避免卡键。

## 设置页

为了保持主界面不需要向下翻，以下内容都收进弹出的“设置”：

- 四个动作及输出映射
- 自定义语音词表/映射
- 头控算法、稳定区、左右速度/反转，以及“上下视角用右手还是头部”；两种上下方式都复用左腕 `lookGate` 门控

动作映射保存在：

```text
config\motion_mappings.json
```

## 头控校准

头控核心位于 `head_control.py`，当前信号版本为 `head-control-v4.3-reference-video-tuned`。PnP（3D 头姿）和 ratio（脸部比例）估计器、中心采集、One Euro 滤波与噪声自适应稳定区继续保留；水平控制采用意图控制：平滑后的 yaw 角度决定速度级别，yaw 角速度决定是否真的在转头，角加速度只作为减速/停止的辅助证据；一旦检测到回正方向，即使头仍在偏侧也立即停止 Mouse X，停在偏角不再造成持续漂移。

上下视角在设置中可选 `右手上下` 或 `头部抬头/低头`，但两种模式都必须先让左腕进入固定 `lookGate`。右手模式继续使用右腕相对右肩的 Y 位移；头部模式在每次 `lookGate` 从关闭进入开启时，把当时真实 pitch 作为本次临时中心，再用 pitch 意图阈值驱动 Mouse Y。左腕离门会立即清空临时中心、速度/加速度状态和 Mouse Y。垂直门只授权 Y，不冻结水平头控，因此门打开时仍可正常用头左右观察。旧的双眼/双耳切换、face/z 融合和五段方向校准不再作为运行路径。

校准是一次有限的会话级中心采集：约 1.0 秒准备，至少 2.2 秒、32 个有效样本，墙钟上限 6.0 秒；最多只跳过低置信度帧或明显跳点，不会因轻微模型抖动清空历史。墙钟到期仍有至少 20 个有效样本即可成功，失败时保留已有个人中心或安全零输出。中心和噪声使用 robust median + MAD/IQR，噪声偏大只扩大稳定区，不把正常抖动当成校准失败。

每次校准只保存摘要到 `%LOCALAPPDATA%\MotionControl\head_profile.json` 和诊断 JSONL，不保存视频或完整姿态帧。参考调参记录见 `docs\reference\REFERENCE_VIDEO_TUNING_2026-08-21.md`。

## 本地语音

语音控制使用 Vosk 中文小模型的受限 grammar（只允许当前命令短语的识别语法）。grammar 由当前唤醒词、紧急停止、实际映射及其同义词生成，中文按单字 token（词元）交给 Vosk，parser（解析器）再去除空格严格匹配。

- 电脑麦克风：本地服务把 16 kHz PCM16 实时音频直接送入 Vosk 受限 grammar；不上传音频。
- 手机麦克风：官方 Vosk Android `SpeechService` 独占录音和缓冲，最终文本才通过 `voice_text` 发给 PC 再按同一唤醒词与映射解析。
- 手机端不再使用自写 `AudioRecord + acceptWaveForm` 链，避免历史原生崩溃路径；也不分发授权未澄清且本轮质量不足的 Sherpa KWS 权重。
- 离线对比中表现最好的轻量预处理含整句裁静音，无法等价用于实时流，因此 1.00 基线不加入实时 DSP（数字信号处理）或自动增益。
- 手机仍可在本机 UI 显示识别到的中文短语，但该短语不是网络执行所必需的数据。

因此两条移动端网络链明确分开：

```text
体感：camera -> MediaPipe(local) -> 25-point pose_features_v1 -> PC ControlKernel
语音：mic -> Vosk constrained grammar(local) -> voice_text -> PC parser/action
```

只有场景捕获/重新匹配时，PC 才会按需请求一次 JPEG `scene_snapshot`；它不是持续视频流。

## 启动

```text
START.bat
```

服务默认监听 `0.0.0.0:8765`，因此手机和电脑应在同一局域网。打开网页后，在“摄像头来源”选择“手机摄像头”，把页面显示的 `ws://.../ws/input` 地址填入手机端；手机紧凑姿态 `pose_features_v1`、`voice_command(command_id)` 和手持传感器共用 `/ws/input`。PC 将 25 个必要姿态点在进程内补成统一结构后继续走同一份 ControlKernel；手机不发送 `world_pose`。

选择“电脑摄像头”后，点击“启动本地摄像头”即可让 Python 服务打开摄像头并运行 MediaPipe Full，同时启动电脑本地麦克风语音。当前启动 Python 必须同时具备 `opencv-python`、`numpy`、`mediapipe` 和 `sounddevice`；若缺少依赖，服务会明确报告错误，不会退回浏览器推理或浏览器录音。

Windows 摄像头采集默认请求 `640×480@30 FPS`，并把缓冲区压到 1 帧以减少延迟。首次启动会在释放设备后分别短测 MSMF（Media Foundation）和 DirectShow，选择实际有效读帧率更高者；结果缓存到用户目录 `%LOCALAPPDATA%\MotionControl\camera_backend.json`，下次直接复用，打开失败才重新探测。主界面“摄像头来源”旁可以选择“自动 / MSMF / DirectShow”；运行中的摄像头需先停止后切换。性能面板和服务端 `PERF` 行会同时显示后端、请求 FPS 与实际采集 FPS。

手机断线、来源切换或超过约 300 ms 没有新帧时，身体区域、动作持续输入和头控会自动归零。手持手机的 `sensor_frame` 中 A/B/X/Y/LB/RB/LT/RT/START/BACK 和左摇杆直接进入 Xbox 输出；四元数、陀螺仪和加速度本版本只保留状态，不做复杂映射。

MediaPipe Full：

```text
I:\MotionControl-Pose-Models\models\mediapipe\pose_landmarker_full.task
```

当前正式 Python/MediaPipe 环境优先使用同目录的已验证兼容副本：

```text
I:\MotionControl-Pose-Models\models\mediapipe\pose_landmarker_full_compatible_075.task
```

原始 `pose_landmarker_full.task` 保留不覆盖；服务找不到兼容副本时才回退到原始文件。

如果 8765 已被旧的 MotionControl 实例占用，启动窗口会明确提示并退出当前实例；不会自动结束旧进程，可先关闭旧实例或用 `--port` 指定其他端口。
