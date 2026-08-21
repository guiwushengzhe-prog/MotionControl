# 工作记录

## 2026-08-20：PC 手机输入源最小适配

- 基于 Git baseline `d363248`，仅在 `F:\MotionControl-App` 修改；未修改 `F:\switch`，未打包。
- 保留电脑摄像头的 `getUserMedia` + 浏览器 MediaPipe Full 路径，新增手机 `/ws/input` 姿态转接和手持 sensor 分流。
- 使用同一个 `processPoseMap` 进入身体区域、四动作、头控和现有输出函数；手机模式不显示/传输手机视频。
- 增加来源切换、手机地址/连接状态、断线和 300 ms 看门狗归零测试；真人手机与游戏实机尚未验证。

## 2026-08-20：本地控制内核下沉

- 基于 `f4c6e418792d410bff94db7b76de1059ad78d216` 继续修改 `F:\MotionControl-App`；未修改 `F:\switch`，未打包。
- 新增 `control_kernel.py`：把原网页中的身体相对区域、四动作、头控和输出时序移入 Python；姿态、手持传感器和 300 ms 看门狗使用单调时钟，源切换/断线统一清理。
- `input_bridge.py` 的手机 `pose_frame_v2` / `sensor_frame` 直接调用内核；`server.py` 增加身体源选择、内核状态和头控配置接口。`web/app.js` 仅做展示/配置，移除浏览器视频推理与 `requestAnimationFrame` 控制循环。
- 新增 `NativeCameraService`，电脑摄像头由 Python OpenCV + MediaPipe Tasks VIDEO mode 处理；当前默认 Python 缺少 `mediapipe`，已验证会给出明确错误，待安装依赖后再做一次真实摄像头冒烟。
- 最低自动验证：28 项通过；包含离线伪造 33 点触发 `hands_up`、经 `/ws/input` 的手机姿态转发、手持按钮/摇杆与清理、300 ms 看门狗释放。真人手机、电脑摄像头硬件和游戏实机尚未验证。

## 2026-08-20：本地语音源收口

- 按架构更正取消手机 PCM `/ws/audio` 方案；电脑身体源由 Python 本地麦克风识别，手机身体源发送小型 `voice_text` 到现有 `/ws/input`，两者共用 Vosk 词表、唤醒词“体感”和 `voice_mappings.json`。
- 新增紧急停止、开始/停止、上下左右、加速/刹车、攻击/闪避、确认/返回等可编辑短语；仅当前身体源可执行，手持传感器消息不进入语音解析。
- Vosk 模型已复制到本项目相对目录并加入忽略规则（不进入 Git）；运行时依赖 `vosk`，电脑麦克风另需 `sounddevice`。未打包、未修改 `F:\switch`。
- 模型复核：14 个文件、68,292,271 字节；项目副本与 `F:\switch\models\vosk-model-small-cn-0.22` 全文件 SHA256 一致，`am\final.mdl` 为 `91EDA2C04C4F599361CB92B0E5298CCDF6B3C7A1FA52BFCEFCD2C4E07AA1C131`。
- 本轮验收重点为服务端源切换/断开释放、手机文本接收与拒绝规则、无网页运行；真人语音、真实麦克风设备和手机 0.7.5 发送端尚未验证。

## 2026-08-21：电脑摄像头预览与性能闸门

- 基于 `810ae40ffe1ebbffb5c2f76da1053abbd861bad2` 仅修改 `F:\MotionControl-App`，未修改 `F:\switch`，未打包。
- 原异常链定位为：网页此前只有黑色 canvas、仅绘制 12 条躯干连接，不能核对真实帧；镜像由 CSS、区域矩形由 JS 分别处理，存在二次变换风险。现在 OpenCV 原始未镜像帧同时供 MediaPipe 和独立 JPEG 预览，33 点完整连接表、骨架、区域统一由同一显示变换组处理；内核坐标保持未镜像规范。
- `NativeCameraService` 改为单帧最新值采集、独立推理和独立 JPEG 编码线程，记录 capture/inference FPS、平均/P95、延迟、丢帧/跳帧和人体数；新增 `/api/camera/preview.jpg`、`/api/performance`、每 5 秒单行 `PERF` 摘要和网页性能折叠面板。
- 真实 15 秒电脑摄像头验收使用已验证的 0.10.5 兼容 Full 模型：`640x480`，采集约 `16.8 FPS`，推理约 `16.8 FPS`，平均推理约 `18.5 ms`，P95 约 `22.5 ms`，总延迟约 `15–31 ms`，人体数 `1`，无持续新增跳帧；截图见 `F:\MotionControl-build-075\camera-gate\camera-preview-performance.png`。
- 另外确认 `I:\MotionControl-Pose-Models` 当前 Full 模型（SHA256 `F0D808...`）在 MediaPipe 0.10.5 报 metadata normalization 错误；本次未修改模型，验收使用已验证兼容副本（SHA256 `5134A3...`）。

## 2026-08-21：Windows 摄像头采集后端 hotfix

- 基于 `dfab70e1b3bcb2306acd0bec6508cfcfdfdc6102`，只修改 `F:\MotionControl-App`；未改 `F:\switch`、动作算法、坐标变换、手机或语音，也未打包。
- `NativeCameraService` 现在请求 `640×480@30 FPS`、`CAP_PROP_BUFFERSIZE=1`；自动模式在首次打开时依次短测 MSMF 与 DirectShow（DirectShow 请求 MJPG），每个候选释放后再打开，选有效读帧率更高者。选定后写入 `%LOCALAPPDATA%\MotionControl\camera_backend.json`，缓存打开失败时才重新探测；也支持手动选择后端。
- `/api/camera/config`、`/api/performance`、网页性能面板与每 5 秒 `PERF` 行均报告 backend、requested_fps、actual_capture_fps；推理仍只消费最新帧，预览线程独立。
- 30 项既有自动测试通过。使用已验证的 MediaPipe 0.10.5 兼容 Full 模型做真实硬件验收：MSMF 2.2 秒有效读帧约 `26.83 FPS`，DirectShow+MJPG 约 `25.09 FPS`；胜者 MSMF 缓存命中后 10 秒采集约 `29.86 FPS`、推理约 `29.87 FPS`、分辨率 `640×480`、人体数 `1`、无错误。旧基线约 `16.8 FPS`，本次实际提升到接近请求的 30 FPS。由于候选短测受摄像头预热和曝光影响，日常仍应以性能面板实际值为准。

## 2026-08-21：固定显示镜像与头控轴向修订

- 仅修改 `F:\MotionControl-App`，未修改 `F:\switch`，本轮不打包。
- 移除网页端“画面：不镜像/镜像”选择；电脑预览、骨架和区域固定使用显示层水平镜像，内核姿态坐标保持未镜像规范。
- 头控水平轴在内核中只补偿一次显示镜像，避免左右反向；垂直信号改用脸部尺度归一化，并将默认垂直死区从 `12%` 调为 `8%`、幅度从 `45%` 调为 `60%`；鼠标垂直速度默认同步为 `600`。
- 按本轮边界只做 Python/JavaScript 语法检查与必要编译；未运行浏览器、摄像头、真人、性能、游戏或自动化功能测试。便携包需待手机任务完成后重建。

## 2026-08-21：按 0.7.3 只读参考回归头控鼠标算法

- 只读解压 `C:\Users\Lenovo\Downloads\MotionControl-v0.7.3-body-zones-actions.zip` 到独立临时目录，核对旧版 `web/app.js` 与 `output_backend.py` 的实际公式和时序。
- 参考版中心为 1.5 秒正视阶段的 yaw/pitch 中位数；左右/上下各 1.5 秒取中位数，方向不足时回退 yaw ±0.10、pitch ±0.04；自动死区为 yaw `clamp(q99*1.8+0.02, 0.06, 0.30)`、pitch `clamp(q99*2+0.025, 0.08, 0.34)`，无可靠方向时沿用手动死区。
- 参考版 pitch 为鼻尖相对耳/眼中点的 y 位移除以躯干长度；`normalizeAxis → curveAxis(gamma=2.2) → filterAxis(alpha=.10+.36*sqrt(intensity))`，输出限幅为归一化 `[-1,1]`，鼠标速度为 X `600`、Y `450` 乘 UI 强度，发送间隔不低于 `28 ms`。
- 撤销 `c3db0c5` 的脸部尺度归一化、额外水平轴补偿、垂直 `8%/60%` 和 Y `600` 调参；以上公式现在由 Python 本地内核执行，网页仍只显示，固定镜像 UI 保留。
- 本轮只做语法检查与 `git diff --check`，未运行功能、浏览器、摄像头、真人、性能、游戏或自动化测试，未打包。
## 2026-08-21：默认头控与校准状态机修订

- 基于 `909bf95bdaa61a910fb0c92334216382ea26a1e1` 仅修改本地内核、输出层、服务版本和网页状态；未修改 `F:\switch`，未打包。
- 头控启动即使用参考版默认中心、死区、曲线、幅度和滤波参数；主动校准时固定五段各 1.5 秒、总计 7.5 秒，只有五段样本完整且方向有效才原子切换为个人校准。
- 校准开始增加人体来源/33 点有效姿态闸门；来源停止、姿态丢失、紧急停止、退出或取消都会丢弃临时样本并恢复默认参数，不再无限等待或保留半套参数。
- 鼠标最终输出边界对水平量只乘一次 `-1`，以匹配固定镜像显示下的用户实测方向；未改 yaw、校准、死区、滤波和速度公式。
- 页面/API 版本更新为 `0.7.6`；端口占用时给出旧 MotionControl 实例提示，不结束其他进程。本轮仅完成 Python/JS 语法检查与 `git diff --check`，未启动服务或做功能测试。

## 2026-08-21：0.7.7 校准最小关键点闸门与真人方向诊断入口

- 用户实测反馈校准仍卡、鼠标方向仍需真实确认；当前 `8765` 无监听进程，因此不能把旧实例作为本机当前证据。版本/API/网页标识统一升为 `0.7.7`，端口冲突仍只提示、不结束未知进程。
- 校准有效性不再要求 33 点全部可见：只检查鼻、左右眼或左右耳至少一组、左右肩和左右髋；其余点低 visibility 不会阻塞头控校准。姿态不足会走已有单调时钟失败路径，整次恢复默认参数。
- 新增 `head_mouse_e2e_live.py`：只接受真人+真实电脑摄像头+真实 Windows SendInput 链路，不合成姿态、不启动服务；记录四向光标前后坐标，结束关闭输出并恢复原光标位置。本轮环境没有真人方向验收，未运行该入口。
- 本轮只做 Python/JavaScript 语法检查、必要编译和 `git diff --check`；未运行服务、浏览器、摄像头、游戏、性能或自动化功能测试。

## 2026-08-21：0.7.8 校准准备阶段

- 校准按钮现在无条件进入固定 5 秒 `prepare`（准备）阶段；此阶段不采样、不检查来源/姿态、不替换默认参数，网页显示倒计时并允许取消。
- 准备结束后才进入正视、左转、右转、抬头、低头五段，每段 1.5 秒；仍只检查鼻、双眼/双耳和肩髋最小关键点，任一阶段失败整次恢复默认参数，五段完成后才原子切换个人校准。
- 服务、网页、README 和诊断入口版本统一为 `0.7.8`，新增 loopback-only `/api/shutdown` 供后续实例正常退出。当前旧 0.7.7 不具备该接口，已按已知 PID 关闭后再启动新版本。

## 2026-08-21：0.7.9 全屏可暂停校准流程

- 校准改为全屏高对比层，集中显示阶段、动作提示、倒计时/有效采样进度、结构化暂停原因和唯一的取消按钮，避免普通控制界面遮蔽状态。
- 五个动作阶段改为累计 1.5 秒有效样本；关键点暂时不足只暂停并保留已完成阶段，不再由短 watchdog 自动结束。方向幅度不足时只重做相关阶段，直到成功或用户主动取消。
- 内核状态新增 `stage_valid_s`、`stage_required_s`、`stage_pause_reason`、`stage_missing_parts`、`stage_transition_message` 等字段；成功后个人参数原子生效并保留 2 秒成功层，取消/切源/停摄像头持久显示默认参数状态。
- 本轮只做语法检查和必要编译，未替用户进行真人校准、模拟姿态或功能长测。

## 2026-08-21：0.7.10 校准失败现场诊断记录

- 只读检查正在运行的 0.7.9：摄像头/姿态流仍在运行，校准状态已是“用户取消，当前使用默认参数”；旧版本没有阶段计数、暂停原因计数或抬头/低头 pitch 样本持久化，不能从取消后的状态伪造最终幅度结论。
- 当前实时帧的鼻、双眼/双耳、双肩分数均接近 1，但左右髋分数约 `0.0101/0.0049`，低于校准要求 `0.30`；因此躯干尺度无法计算、`raw_pitch=null`。这确认当前帧的阻塞点是双髋/躯干可用性，不是已证实的 `0.008` 俯仰幅度阈值问题。
- 新增有界的本地诊断 JSONL：每次真实校准记录各阶段有效/无效帧数、暂停原因/缺失部位计数、yaw/pitch 的 min/max/median，以及最终中心/上/下值、方向和阈值比较；诊断写入 `%LOCALAPPDATA%\\MotionControl\\calibration_diagnostics.jsonl`，写失败不影响控制。
- 已保存本次现场快照：`output/calibration_failure_diagnostic_20260821_132722.json`。本轮不改俯仰阈值，不用模拟姿态，不启动新校准。

## 2026-08-21：0.7.10 头肩信号修复

- 旧公式为 `(鼻尖.y - 耳/眼中点.y) / 肩髋中点距离`；躯干尺度依赖双髋，且耳点缺失时每帧切换到眼点，可能造成俯仰无值或跳变。
- 新增 `head-shoulder-v2`：鼻、固定的双眼或双耳点对、双肩是唯一必需输入；yaw 使用固定点对的鼻尖距离对数比，pitch 使用同一点对的垂直差除以双肩宽。脸部点对在当前人体来源期间锁定，不逐帧切换；双髋完全不参与头控信号。
- 新默认俯仰信号中心/端点为 `0.15 / 0.08 / 0.22`；个人校准仍以五段中位数原子替换，旧信号版本不复用，自动回默认并提示重新校准。诊断记录保留并增加 signal version/face pair。
- 本轮不做模拟姿态或真人校准；只完成源码静态检查、版本提交和后续必要编译准备。

## 2026-08-21：0.7.10 编译与真人试用交接

- 使用既有 PyInstaller 便携构建环境完成 `MotionControl-0.7.10.exe`；只出现既有 MediaPipe `model_maker` 子模块收集警告，构建成功。
- 已通过回环 `/api/shutdown` 正常关闭 0.7.9，再启动 0.7.10；当前 PID `32188`，监听 `8765`，页面/API 均报告 `0.7.10` 与 `head-shoulder-v2`，头控默认参数已启用。
- 未启动摄像头、未启动校准、未替用户真人测试；仅读页面/状态确认版本和初始状态。

## 2026-08-21：0.8.0 手机连接收口

- 仅修改 PC `F:\MotionControl-App`；手机端保持冻结，不修改 `F:\switch`。产品版本统一为 `0.8.0`，姿态协议名继续为 `pose_frame_v2`，头控算法版本继续为 `head-shoulder-v2`。
- 静态核对手机 commit `6dab65fe4f077c81d7d0b924ad5d104c9e2d57dd`：`pose_frame_v2` 携带 33 点、`actual_model`（full/lite）、`coordinates_mirrored`、`camera_facing`、`device_id`、`sequence`、`captured_at_ms`、可选 `world_pose`/`inference_ms`；`sensor_frame` 携带四元数、旋转率、加速度、touches；`voice_text` 携带 camera role、device/sequence/timestamp/text 和可选 confidence。PC 校验与转换链均兼容，无需手机字段改动。
- 手机姿态仍由 `/ws/input` 进入同一 `ControlKernel`，不在电脑重复运行 MediaPipe；断线/切源沿用 300 ms watchdog 和原子释放。页面从 `/api/input/status` 显示实际私有局域网地址，当前只读发现为 `ws://192.168.1.35:8765/ws/input`。
- 只读核对发现 8765 由已知旧 0.7.10 进程占用，未结束未知进程；Windows 防火墙未发现 MotionControl 专用规则，本轮未改防火墙。
- 本轮只做 Python/JavaScript 语法检查、必要编译和 `git diff --check`；未操作摄像头、校准、游戏或输出。

## 2026-08-21：0.8.0 真实联调启动

- 便携 EXE 首次启动失败的根因是组装目录沿用了早期构建的 `_internal`，而新 EXE 的 `pyi_rth_pkgres` 需要 `jaraco/text/Lorem ipsum.txt`；按主任务优先级停止继续修包，未把该 EXE 交给用户使用。
- 使用完整 Python 3.11 固定依赖环境直接运行当前源码 `server.py`，`--host 0.0.0.0 --port 8765`，MediaPipe Full 模型来自已验证资源目录；当前联调 PID 为 `28924`，服务日志在 `F:\MotionControl-build-075\live-0.8.0-source`。
- 只读确认页面标题/API/kernel/input 均为 `0.8.0`，头控信号为 `head-shoulder-v2`，模型可用，输出 `/api/output-status.enabled=false`；未启动摄像头、校准、游戏或输出。

## 2026-08-21：0.8.1 手机版本对齐

- 手机实际安装版本为 0.8.1，PC 产品标识同步升为 `0.8.1`；`pose_frame_v2` 协议和 `head-shoulder-v2` 信号版本不变，未修改手机端或便携构建。

## 2026-08-21：0.8.1 手机姿态边缘坐标接收修复

- `pose landmarks are invalid` 在 PC 接收层只有一处直接抛出：33 点姿态中任一点不是对象、缺少/含非有限 `x/y/z/visibility`，或旧逻辑把 normalized image `x/y`（以及 visibility）限制在 `[0,1]`。点数错误会先抛出 `each pose must contain exactly 33 landmarks`；`world_pose` 和手部有各自的错误文本，不会触发这条消息。
- 软件任务约定的 `F:\switch\output\pose-frame-rejected-081.json` 在本次修复前尚未出现，因此没有伪造具体数值；真实错误与旧 `[0,1]` 边界条件一致。
- 新逻辑保留有限性和 visibility `[0,1]` 校验，对 `x/y/z` 使用绝对值 `<=10` 的防损坏界限，不在接收层裁剪坐标；原值继续交给内核关系计算，显示层再负责裁剪。

## 2026-08-21：0.8.1 头控校准与鼠标方向收口

- 在不重启服务的前提下保存了最新真人失败现场：`output/head-calibration-latest-real-failure.json`。最近一次记录的各阶段多数已有约 1.5 秒有效样本；失败来自旧收口固定阈值（yaw 0.025、pitch 0.008）和重试后把后续阶段样本累积到下一次 final check，而不是单纯缺少人体帧。
- 保留 `head-shoulder-v2` 的头部最小输入（鼻、锁定双眼/双耳、双肩）；新增结构化当前信号/相对中心/幅度参考进度，前端在全屏校准层显示，髋部仍不参与头控。
- 校准锚点采用低但非零的最小分离 `0.0005`，只在左右或上下方向次序未形成/几乎无差异时停在对应阶段；重试从该阶段起清空后续锚点，避免历史样本污染。校准期间输出保持中性，默认参数继续有效。
- 鼠标 X 改为唯一的标记锚点映射：left/center/right 统一归一化到负/零/正；删除 OutputBackend 末端散落的全局 `-x`。参考 0.7.3 的 SendInput 边界本身不翻转 X，镜像只属于显示层。

## 2026-08-21：0.8.1 head-face-v3 有限校准与上下轴收口

- 真实快照 `output/head-calibration-latest-real-failure-v2.json` 已在修改前保存。最后一次用户取消发生在 down 阶段：26 个有效帧、0.798 秒；此前一次完整五段记录的 pitch 中位数为 center `0.0532203`、up `0.0586480`、down `0.0562097`，有效帧均约 40、无效帧为 0，但旧 final check 要求 up/down 位于 center 两侧，故 `pitch_order_correct=false` 并不断重启。
- 头控信号升级为 `head-face-v3`：鼻 + 双眼或双耳是必需输入；肩、髋、手、脚不参与 head yaw/pitch 的可用性门槛。pitch 使用眼距或半耳距估计脸宽并乘固定 5.0 的单一尺度，避免髋部缺失导致 pitch 无值。
- 校准改为 5 秒准备、五段各累计 1.5 秒有效样本、每段 6 秒墙钟上限。阶段超时按 yaw/pitch 轴回退到已有个人值或默认值，继续到末尾；最终总是显示“校准完成（个人值/已有个人值/默认值）”并原子保存同版本 profile。输出在校准期间保持中性。
- 参考版与当前 OutputBackend 均直接发送 `y * speed_y * dt`，没有隐藏的 `-y`；上下方向由 up/center/down 标签归一化保证抬头为负（Windows 光标上移）、低头为正。未在本轮代替用户做真人方向测试，且快照时输出开关为关闭。

## 2026-08-21：head-control-v2 设计落地

- 按 `I:\MotionControl_head_control_v2_design.md` 收口，不再维护五段方向校准；信号版本改为 `head-control-v2`。
- yaw 使用鼻子到左右眼/耳的距离对数比，pitch 使用脸部垂直比例 70% + MediaPipe z 深度 30%，尺度统一由眼距或半耳距乘 5.0 得到，双肩/双髋不再成为头控依赖。
- 默认 raw 范围为 yaw 0.20、pitch 0.04，死区默认 8%、gamma 1.5；启动即可控制。第一次有效姿态或用户手动操作会记录 3 秒中心，至少 1 秒有效样本才替换个人中心，否则保留已有个人值/默认值。
- 旧版本个人 profile 按 signal_version 拒绝加载并提示重新设置中心；诊断继续只追加摘要 JSONL。当前仅完成源码修改，真人方向、摄像头和游戏输出留给用户验收。

## 2026-08-21：head-control-v2 方向链与真人采集准备

- 审计 dd00313 后的未提交修改，移除只在输出末端补符号的 `invert_yaw`；保留 `coordinates_mirrored` 单次归一化、`_normalize_v2_yaw` 唯一水平方向映射和用户显式 `invert_x`。
- `tools/head_signal_capture.py` 改为正式 Full task + 真实摄像头入口，输出 raw yaw/pitch 分量、normalized yaw/pitch、output x/y、face pair 和模型 SHA256，默认落盘到 `F:\MotionControl-App\output`。
- 只完成语法、定向 head-control-v2 测试和帮助入口检查；未替用户采集、移动鼠标或启动摄像头。

## 2026-08-22：融合 v0.9.3 reference-video-tuned 头控

- 参考包 `MotionControl-v0.9.3-reference-video-tuned.zip` SHA256 为 `7EC1B2AEFBE6AF786D242B76D4A49960FE44F28EF1074226A06C8CCAF9BD8F1F`，解压到临时只读目录后审计。
- `head_control.py` 原样复制，工作树 SHA256 与参考一致；运行头控和中心校准统一由 `HeadController` 管理。`control_kernel.py` 采用参考版内核并仅增加外围 `set_current_center()` 兼容入口。
- 保留当前手机 `/ws/input`、语音、身体区域、四动作、摄像头后端、正式模型路径和输出链；服务头控配置与网页头控设置改为参考版 algorithm/deadzone/sensitivity 接口。
- 产品标识更新为 v0.9.3，协议名仍为 `pose_frame_v2`；未安装依赖、未下载模型、未使用 `.pylibs`，未进行真人/摄像头/鼠标/游戏测试。

## 2026-08-21：Git 恢复与 head-control-v2 pair recenter 收口

- F 盘工作树的 3790fb2 变更文件已逐个核验；临时 C 盘仓库仍存在并提供完整父提交链。
- 为避开 F 盘 `.git/objects` 的历史写入问题，保留原 `.git` 备份后，将完整 Git 元数据放到 `I:\MotionControl-App\_git`，F 盘工作树使用 separate-git-dir 指针。
- 发现旧实现的 pair reselect 只设置等待标志；现在切换 eyes/ears 后复用现有 center capture，直到新中心成功前输出保持零，并新增回归测试覆盖该安全边界。
## 2026-08-22：0.9.3 电脑摄像头模型兼容性修复

- 用户真人点击电脑摄像头后真实返回：`Input tensor has type float32: it requires specifying NormalizationOptions metadata to preprocess input images.`；原始证据保存于 `output/camera-model-compat-failure-2026-08-22.json`。
- 正式环境仍为 `F:\MotionControl\MediaPipe\.venv\Scripts\python.exe`、Python 3.11.11、MediaPipe 1.0.0、OpenCV 5.0.0；未安装/升级依赖，未使用 `.pylibs`。
- 参考包 WORKLOG/PITFALLS 已记录同一兼容性问题及已验证副本。使用本机已有 `F:\MotionControl-build-075\isolated-075\models\mediapipe\pose_landmarker_full.task`（SHA256 `5134A3AAD27A58B93DA0088D431F366DA362B44E3CCFBE3462B3827A839011B1`），复制为版本化 `I:\MotionControl-Pose-Models\models\mediapipe\pose_landmarker_full_compatible_075.task`；原始 `pose_landmarker_full.task`（SHA256 `F0D8086050426E969DFE570E980E456EEB93B2B0842A02EF5EF5345D7B7980DA`）未覆盖。
- `server.resolve_full_model()` 仅增加外围候选优先级：存在兼容副本时优先，否则回退原始文件；头控/自动校正核心未修改。
- 兼容模型已用正式 Python 完成一次 `PoseLandmarker` create/close 成功确认；待重启服务后做一次最小真实摄像头启动确认。

最小链路已完成：重启后的 0.9.3 服务实际使用版本化兼容副本，摄像头在 MSMF、640×480 下启动成功，`last_error=null`、实际读帧约 22.8–24.0 FPS；随后已停止摄像头，最终状态为 `running=false`，输出仍关闭。

## 2026-08-22：Vosk 与语音校准命令

- 参考包 `requirements-voice.txt` 与历史便携构建均锁定 `vosk==0.3.45`。正式 Python 仅安装该包及其必要导入依赖（`requests`、`srt`、`tqdm`、`websockets` 等），没有升级/重装 MediaPipe、OpenCV、NumPy 或 sounddevice，也没有使用 `.pylibs`。
- 正式环境验证：Vosk 0.3.45 从 `F:\MotionControl\MediaPipe\.venv\Lib\site-packages\vosk\__init__.py` 导入；模型 `F:\MotionControl-App\models\vosk-model-small-cn-0.22` 真实加载成功；服务 `/api/voice/status` 为 `model_ready=true`、grammar、80 条支持词、无 unsupported。
- `开始校准` 已作为 `system:HEAD_CALIBRATION_START` 加入默认词库；`体感开始校准` 与 `体感 开始校准` 经过同一唤醒词解析。服务端外围回调在执行前检查精确语音源仍在线，并确认电脑摄像头或手机姿态源仍是当前身体源，然后调用现有 `RUNTIME.start_calibration()`；头控核心未改。

## 2026-08-22：摄像头性能短基线复测

- 真实摄像头、输出关闭、MSMF、640×480、兼容 Full 模型：修改前短基线约 29.75–30.34 capture/inference FPS，推理平均 15.0–19.2 ms，P95 23.2–33.0 ms，总延迟 15–16 ms；期间无新增 drop/skip（旧累计值为 655）。
- 本轮没有发现可安全获得收益的重复转换、排队或预览阻塞问题，因此没有修改 `NativeCameraService` 性能代码，也没有降低输入质量或更换模型。重启后同口径短复测约 29.86 FPS、平均 12.6 ms、P95 14.1 ms，属于短测波动范围；摄像头随后已关闭，输出仍关闭。
