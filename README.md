# MotionControl 2.0

> 第一次用？看 **[新手指南](docs/新手指南.md)** —— 图文版，从下载到能玩大约十分钟。

电脑端整理为“开始游戏、游戏配置、设备与设置”三个入口。开始／暂停控制与紧急停止固定在顶部；首次定位只需头部和双肩入镜，区域拖动、大小、重新定位、保存和取消集中在一个编辑器中。

游戏映射按游戏分别自动保存，切换前处理待保存修改；失败保留草稿并提供重试。旧配置首次读取时备份为 `config/game_profile_selection.json.v1.bak` 后迁移。手机协议、模型、识别算法以及实体手柄合成方式保持兼容。

本轮验证使用配置检查和离线浏览器模拟，没有运行模型、真实摄像头或游戏。代码量、删除合并清单与验证记录见 [2.0 交付记录](docs/V2_DELIVERY.md)。

## 既有核心能力

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
tools\BUILD_GAME_LIBRARY.bat
```

流程为：Steam Windows Top Sellers 种子 → SteamInputDB 候选配置 → 下载 VDF → 确定性解析 → 紧凑 JSON Profile → 库审计。默认目标种子数为 500，但它只是目标：**没有“少于 300 个 Profile 就失败”的硬门槛**。网络中断或部分游戏没有可解析配置时，已经生成的 Profile 仍会保存并可正常使用。

构建器默认每 10 个游戏做一次 checkpoint（断点保存）。下次运行会跳过已经存在的有效 Profile，失败项会自动重试，因此可以分多次逐步积累库。

只重建/检查 6 个重点游戏：

```text
tools\BUILD_PRIORITY_PROFILES.bat
```

当前重点集：Red Dead Redemption 2、GTA V Legacy、GTA V Enhanced、Forza Horizon 4、Forza Horizon 5、Black Myth: Wukong。

Profile 是否“真人验证”与自动构建质量分开记录。真人实际进入游戏验证后，可按 Steam AppID 登记，例如：

```text
python tools\mark_profile_verified.py 1293830 --note "已实测区域按键和菜单"
```

之后再次自动刷新同一 Profile，不会清掉这份人工验证记录。`tools\audit_game_profiles.py` 会生成 `game_profiles/audit_report.json`，分别报告 Profile 是否存在、标准六区覆盖、官方配置来源和真人验证状态；审计只报告问题，不阻断运行。

## 本地控制内核（正式运行边界）

浏览器现在只负责 UI（界面）、配置和状态/骨架展示；持续姿态推理、身体区域、七个持续动作、双手交叉姿势、头控、看门狗（超时保护）和游戏输出时序全部由本地 Python 服务维护。网页关闭、最小化或被游戏遮挡时，内核仍可继续接收输入并释放输出。

- 手机正常体感帧使用 `pose_features_v1 / mc25-v1`：只发送 25 个控制必需关键点的紧凑数据，不发送 33 点完整骨架、`world_pose` 或视频；PC 在进程内补成统一 33 点结构后继续走唯一的 Python 控制内核。旧 `pose_frame_v2` 仅保留兼容。
- 兼容完整 `pose_frame_v2` 时，若帧携带 33 点 `world_pose`，PC 仅在头控校准期把它送入 v153 Personal PnP（个人三维脸部模型）；归一化图像点仍独立进入身体区域和运行期头控，运行期 world pose 断流不会改变已激活的横向输出。
- 手机 `sensor_frame` 直接驱动手柄按钮/摇杆；断线或超过约 300 ms 没有新帧会由服务释放。
- 电脑摄像头由本地 Python OpenCV + MediaPipe Pose Full 采集和推理；浏览器不再承担电脑摄像头正式控制链，只显示服务返回的骨架。
- 身体源只能有一个（电脑摄像头或手机摄像头），手持传感器可以并行；切换由服务原子释放旧源。

## 主界面保留

- 本地 MediaPipe Pose Full（33 点）骨架状态展示
- 摄像头来源：电脑摄像头（本地 Python 推理）或手机摄像头（手机本地 MediaPipe 推理，仅发送紧凑 `pose_features_v1` 控制特征；电脑不接收/显示连续手机视频、不重复推理）
- 手机局域网连接状态与 `/ws/input` 地址提示
- 头控：鼠标 / Xbox 360 右摇杆；可通过头控配置选择 v153 Personal PnP 负责水平左右，纵向仍只由左腕进入视角门后右腕上下控制
- 游戏速度倍率
- 身体相对区域触发状态
- 七个持续动作与双手交叉姿势实时状态
- 语音状态与本地/手机来源提示
- 游戏悬浮窗
- F8 总输出开关 / F9 紧急停止

## 固定六区域与场景匹配

正式控制使用固定在摄像头坐标中的 6 个区域，而不是逐帧跟着人体移动。首次建立场景时，内核会从最近约 0.9 秒的多帧姿态取中位数快照，降低单帧抖动导致的区域偏移；如果仍不适合个人站位，主界面可直接拖动 6 个区域并保存。之后区域保持固定，只有用户明确执行“重新识别我的位置”才重新匹配。

默认 6 区：

- 左手：合并头顶/耳外范围 → 默认 `X`（可在游戏设置中修改）
- 右手：合并头顶/耳外范围 → 默认 `B`（可在游戏设置中修改）
- 左脚侧抬/侧迈区 → `LB`
- 右脚侧抬/侧迈区 → `RB`
- 头顶上方小幅跳跃区 → 默认 `A`（可在游戏设置中修改）
- 下巴左侧绿色“上下视角”区 → 无按键，只检测左腕进入/离开

动作区域连续 2 帧进入才按下；视角门离开 1 帧即关闭纵向输出。视角门开启时只允许右腕相对右肩的上下位移控制 Y，右腕 X 坐标完全不参与纵向算法；同时冻结头部水平输出，避免抬手时轻微头部补偿造成斜向视角。旧版四个手区标识仍作为兼容别名读取，但运行时只计算两个合并手区。

## 动作库：自带两个，其余从官方动作库下载

程序自带**原地踏步**和**小腿向后抬起**。其余动作（下蹲、双手举过头、开合跳、侧步开合、提膝碰对侧肘、双手交叉）在云端的**官方动作库**里：在「本游戏 → 动作库 → 官方动作库」点「下载」才有，没下载的动作认不出来。别人分享的配置如果用到了你没下载的动作，映射表上方会写明是哪几个。

下载的是一份动作文件：名字、怎么做、火柴人示范、星级，以及**识别规则**。识别规则是数据，只能拿关节坐标比大小、算距离和角度，执行不了代码；动作文件带官方签名，电脑端验得过签名才装，每次启动还会再验一遍。

每个动作都有星级（1~5 星）：运动强度、识别度（镜头前认得准不准）、上手难度，以及锻炼部位（腿部、臀部、核心、手臂、肩背分别打星）。

发布新动作见 [cloud/official_poses/README.md](cloud/official_poses/README.md)。

“设置 → 当前游戏的输入映射”里可独立启用并映射为：

- 键盘键/组合键
- Xbox 按键
- Xbox 左摇杆方向（如 `LS_UP`）

默认全部关闭，避免未经真人测试就误触。识别规则仍会运行用于状态显示；仅“开合跳”和“双手举过头”不能同时映射，其他动作可以同时配置。

当前规则（数字是 2026-09-25 拿真人录像回放定的，手机竖屏放在人正前方；下载的动作的规则写在各自的动作文件里）：

- **原地踏步**：一只脚的下缘比另一只高出 0.07 个躯干以上算抬起，左右相反脚在 0.10～1.50 秒内各完成一次才开始前进；单腿挪动不会启动。0.8 秒没有下一步则结束。站姿高低差会先扣掉，脚跟和脚尖也会参与下缘计算。绑了小腿后抬或提膝碰肘时，一步要等脚抬到最高再定，免得把那两个算成踏步。
- **小腿向后抬起**：一只脚往后抬到膝盖那么高——脚踝比另一只高出 0.40 个躯干以上、离膝盖不到 0.15，膝盖几乎不升。下蹲状态优先，不同时判定。
- **下蹲**：左右膝角都 `<145°`，并且髋到膝的垂直距离明显缩短。
- **双手举过头**：左右腕都高于鼻子，左右肘同时抬到肩附近以上；单手举不算。
- **开合跳**：两脚分开到肩宽的 1.0 倍以上，同时双腕明显高于肩。
- **侧步开合**：两脚分开到肩宽的 1.0 倍以上，同时双臂向身体两侧展开。
- **提膝碰对侧肘**：任一膝明显抬高，并且和对侧手肘靠到 0.95 个躯干以内。同一次抬腿碰到了手肘，就只算它，不再算成踏步或小腿后抬。
- **双手交叉**：前臂向内且左右腕形成交叉顺序，位于胸前；只在进入姿势时触发一次。

动作和它做的时候会扫过的圈都绑了键时（比如双手举过头和手区），动作做着的时候那几个圈不按；举手、开合跳这类还没认出来就先扫过圈的，那几个圈平时也会晚 0.25 秒按下。绑键时界面上会写清楚。头顶区不让：开合跳本身就在跳。

设置页只会拦截“开合跳”和“双手举过头”同时映射；原地踏步、小腿向后抬起、下蹲及其他动作均可同时配置。

动作结束会立即释放对应持续输入。动作状态由本地 Python 内核按单调时钟维护，超过约 300 ms 没有姿态帧时自动释放，避免卡键。

## 设置页

为了保持主界面不需要向下翻，以下内容都收进弹出的“设置”：

- 七个持续动作及双手交叉姿势的输出映射
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

服务监听两个面，各自信任级别不同：

| 面 | 地址 | 内容 |
|---|---|---|
| 设备接入面 | `0.0.0.0:8765` | 只有 `/ws/input`，外加只读的 `/api/models`、`/api/model/mp-full` |
| 本机管理面 | `127.0.0.1:8766` | 网页界面与全部 `/api/*` |

管理面只绑回环地址，局域网**从网络层就连不上**，而不是靠每个接口自己检查。改动之前 `do_GET` 里只有 `/api/shutdown` 有回环判断，同一 Wi-Fi 下任何设备都能读到摄像头预览、场景参考照片、内核状态和当前游戏配置。浏览器从 8766 加载、也只调 8766，因此始终同源。

手机只需要 8765，`adb reverse tcp:8765 tcp:8765` 与页面显示的 `ws://.../ws/input` 地址都不受影响。**注意分面并不能阻止伪造输入**：`/ws/input` 接受同一网络里的任何连接，而 `sensor_frame` 会直接产生手柄输出。设备配对曾经做过，因为用不到已经去掉，所以只在自己信任的网络里用。

因此手机和电脑应在同一局域网。打开网页后，在“摄像头来源”选择“手机摄像头”，把页面显示的 `ws://.../ws/input` 地址填入手机端；手机紧凑姿态 `pose_features_v1`、`voice_command(command_id)` 和手持传感器共用 `/ws/input`。PC 将 25 个必要姿态点在进程内补成统一结构后继续走同一份 ControlKernel；手机不发送 `world_pose`。

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

如果 8765 或 8766 已被旧的 MotionControl 实例占用，启动窗口会指明是哪一个面并退出当前实例；不会自动结束旧进程，可先关闭旧实例，或用 `--port` / `--admin-port` 分别指定其他端口。

## 许可证

Copyright (C) 2026 guiwushengzhe

本项目采用 **GNU Affero General Public License v3.0**（AGPL-3.0），完整条文见
[LICENSE](LICENSE)。

简单说：你可以自由使用、研究、修改和分发这份代码。但如果你分发修改版，或者
把修改版当成网络服务给别人用（AGPL 第 13 条，这是它和 GPL 的唯一区别），你
必须同样以 AGPL-3.0 公开你那份的完整源码。

    This program is free software: you can redistribute it and/or modify it
    under the terms of the GNU Affero General Public License as published by
    the Free Software Foundation, either version 3 of the License, or (at
    your option) any later version.

    This program is distributed in the hope that it will be useful, but
    WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero
    General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.

第三方组件各自遵循自己的许可证，不受本项目许可证影响：ViGEmBus（BSD-3-Clause，
随发布包附 `LICENSE-ViGEmBus.txt`）、MediaPipe（Apache-2.0）、Vosk（Apache-2.0）。
