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

## 2026-08-23：合并 v0.8.3-seven-zones 场景适配功能

- 基于 Google Drive `G:\我的云端硬盘\MotionControl\v0.8.3-seven-zones\` 的 ChatGPT 交付，将 scene-adapt（固定空间区域 / 七圈推荐 / 场景重新匹配 / 垂直视角门控）功能合并到本地 v0.9.3 项目。
- 优先采用差异合并，未粗暴覆盖。v0.8.2→v0.8.3 patch 仅修改 scene_layout.py（核心 seven-zones）、server.py、web 和测试；control_kernel.py、input_bridge.py、voice_backend.py 在 v0.8.2/v0.8.3 间完全相同。
- 新增 `scene_layout.py`（v0.8.3 seven-zones 版本）：SceneLayoutManager 管理参考场景、7 个推荐固定圈（头顶左/右、耳外左/右、脚踢左/右、下巴左侧视角门）、ORB 特征匹配重新匹配、垂直视角配置。
- `control_kernel.py`：新增 fixed_zones / vertical_look / vertical_gate_active / vertical_wrist_norm；新增 configure_scene_layout() 和 NativeCameraService.latest_frame()；修改 _update_zones_locked() 支持固定圆圈区域和 lookGate；修改 _update_head_locked() 实现垂直视角门控（左腕进 lookGate → 右腕控制 Y，左腕离开 → Y 快速回零，水平视角始终来自头 yaw）；status 输出 scene_mode 和 vertical 状态。
- `server.py`：新增 SceneLayoutManager 实例和场景辅助函数；新增 GET /api/scene/status、/api/scene/reference.jpg；新增 POST /api/scene/capture、/api/scene/rematch、/api/scene/layout；扩展 execute_voice_action 支持 OUTPUT.START/STOP、HEAD.CENTER、SCENE.CAPTURE_REFERENCE/REMATCH；配置 INPUT_BRIDGE scene_snapshot_handler。
- `input_bridge.py`：新增 scene_snapshot 协议（request_scene_snapshot 向手机发请求、_handle_scene_snapshot 处理手机回传 JPEG、configure_scene_snapshot_handler）；handle_message 新增 scene_snapshot 类型分发。
- `voice_backend.py`：扩展系统命令白名单，支持 SCENE.CAPTURE_REFERENCE、SCENE.REMATCH、OUTPUT.START、OUTPUT.STOP、HEAD.CALIBRATE、HEAD.CENTER。
- `web/index.html` + `web/app.js`：新增记录场景/重新匹配按钮、lookGate 视角门区域显示、固定空间区域设置面板（参考场景图、7 圈拖拽编辑器、圆圈半径/右腕上下范围/死区调节、匹配指标显示）。
- `config/voice_mappings.json`：新增 5 条系统语音命令（截图→SCENE.CAPTURE_REFERENCE、重新匹配→SCENE.REMATCH、开始输出→OUTPUT.START、停止输出→OUTPUT.STOP、设置中心→HEAD.CENTER）。
- 保留本机配置：vosk_model_path.txt（models/vosk-model-small-cn-0.22）、model_root.txt（I:\MotionControl-Pose-Models\models）、现有 voice_mappings 自定义词条、摄像头后端缓存、个人校准数据均未覆盖。
- 测试结果：使用 `F:\MotionControl\MediaPipe\.venv`（含 OpenCV/MediaPipe）运行 `python -m pytest tests/ -q`：64 passed, 19 skipped, 2 failed。2 个失败均在 test_scene_layout.py，原因是测试引用旧版 ControlKernel._yaw_signal()（v0.9.3 头控已迁移到 HeadController），属于测试架构不兼容，非合并错误。详情已写入 `G:\我的云端硬盘\MotionControl\v0.8.3-seven-zones\LOCAL_AI_FEEDBACK.md`。
- 语法检查：control_kernel.py / scene_layout.py / input_bridge.py / voice_backend.py / server.py 全部通过 py_compile；web/app.js 通过 node --check。
- 未进行真人测试、摄像头实机测试、手机端联调或游戏输出测试。

## 2026-08-23：应用 v0.8.3 测试兼容补丁并修复垂直视角状态快照

- 应用 ChatGPT 提供的 `MotionControl-v0.9.3-scene-tests-HeadController-compat.patch`：在 `tests/test_scene_layout.py` 新增 `_prepare_v093_head_for_scene_test(kernel)` helper，替换两处旧版 `kernel.head[...] / kernel._yaw_signal(...)` 前置设置。该 helper 直接操作 `kernel.head_controller` 的校准状态（center_pending/calibrating/calibrated/center_yaw/center_pitch/noise_yaw/noise_pitch），再调用 `_recompute_deadzone()` 和 `_reset_filters()`，最后用 `controller.status()` 重置 `kernel.head`。未修改 `head_control.py` 或任何头控核心逻辑。
- 应用补丁后仍有 1 个测试失败：`test_fixed_gate_enables_right_wrist_vertical_but_head_only_drives_x` 断言 `state["head"]["output_y"] > 0` 但得到 0.0。根因：`_update_head_locked()` 中垂直视角门控覆盖了传给 `output.apply()` 的 y 值，但 `status_locked()` 中 `self.head = self.head_controller.status(now)` 重新覆盖了整个 head 字典，导致 `output_y` 被重置为 HeadController 的原始值（0.0）。
- 修复：在 `status_locked()` 中，当 `fixed_zones_enabled` 且 `vertical_look.enabled` 时，用 `vertical_wrist_norm`（gate 激活时）或 0.0（gate 未激活时）覆盖 `self.head["normalized_y"]` 和 `self.head["output_y"]`。同时在 `_update_head_locked()` 中也做了相同的状态快照同步（双保险）。此修复仅在 scene-adapt 集成层，未修改 HeadController 核心。
- 最终测试结果：`F:\MotionControl\MediaPipe\.venv\Scripts\python.exe -m pytest tests -q` → **66 passed, 19 skipped, 0 failed**（目标达成）。
- `tests/test_scene_layout.py` 单独运行：8 passed。
- 语法检查：control_kernel.py / scene_layout.py / input_bridge.py / voice_backend.py / server.py / tests/test_scene_layout.py 全部通过 py_compile；web/app.js 通过 node --check。
- 未修改 head_control.py、未降低任何测试断言、未删除任何测试。

## 2026-08-24：v0.9.5 头控纵向与用户界面收敛

- 头控最终输出只保留 yaw→X；HeadController 内部 pitch 仍可用于诊断，但 control kernel 不再把它传入输出 Y。公开状态的最终 `normalized_y`/`output_y` 也只反映右腕。
- 固定场景下左腕进入 `lookGate` 后捕获右腕 Y 中心锚点，右腕相对锚点经过 range/deadzone 归一化产生纵向；门控关闭、右腕缺失或身体来源清理时立即归零并清除锚点。
- PC 网页版本统一为 0.9.5：主按钮按“开始体感→开启游戏输出→停止游戏输出”推进；普通界面隐藏性能/模型/后端技术字段，保留高级诊断入口；新增六条常用语音大字区和从本地命令目录读取的完整 27 条命令面板。
- 手机端产品标识升级为 0.9.5 / Android versionCode 21；Sherpa KWS、scene_snapshot、地址记忆和断线自动重连链保持不变，技术状态从普通界面隐藏。
- 本轮不替换 MediaPipe、Sherpa、模型或输出后端；真实摄像头、真人动作、游戏联调和手机实机验收留待验收阶段。

## 2026-08-28：PC 端性能优化短测（MotionControl 1.00）

- 8765 端口当时由用户现有 Python 服务占用，状态为手机姿态源；未抢占摄像头、未重启该服务。使用正式 `F:\MotionControl\MediaPipe\.venv`、兼容 Full 模型 `I:\MotionControl-Pose-Models\models\mediapipe\pose_landmarker_full_compatible_075.task` 和 `F:\MotionControl\test_videos\baseline_001.mp4` 做同输入无输出短测。一次基线得到 488 个姿态帧、每帧 33 点，推理平均约 12.6 ms/P95 约 13.8 ms；JPEG 质量 78 的编码平均约 1.7 ms，说明预览编码是可独立削减的外围开销，不能据此宣称真实摄像头验收。
- 摄像头服务预览改为短时请求驱动、最高 8 FPS；没有预览请求时不编码 JPEG，请求仍返回当前最新 JPEG，采集/推理线程和关键点流未改动。新增预览 FPS、编码平均/P95、JPEG 大小和最近年龄诊断字段。
- `/api/input/status` 保留默认完整响应以兼容旧网页和手机桥接；网页轮询改用新增语义 `?brief=1`，仅省略重复的 `runtime` 快照。临时本地服务验证 brief 响应约 439 bytes、完整响应约 6964 bytes；旧调用仍返回完整字段。
- 临时端口验证 HTTP/1.1 持久连接下各 JSON/静态响应均有正确 `Content-Length`，404 后续请求和 WebSocket 升级均不挂起；未对正在运行的 8765 服务做热重启。网页 3 秒轮询频率与改动前一致（kernel 12、input/output/performance 各 4 次）。
- 仅通过 `py_compile`、`node --check` 和预览线程定向单测；输出保持关闭。未进行真人摄像头、手机实机或游戏输出验收。

## 2026-08-28：未完成身体 Zone 时缩小输出门控

- `web/app.js` 主操作保留首次启动时的场景捕获流程，但不再要求场景/身体 Zone 完成后才能开启总输出。
- 未完成 Zone 时，主状态明确提示区域尚未定位，同时保留头控、动作、语音和手机输入可用；Zone 捕获、调整和 Zone 触发条件未改动。
- 未修改 `head_control.py` 或头控/自动校准参数；仅更新对应的最小静态回归断言。
- 检查边界：`node --check web/app.js` 与 `tests/test_minimal_controls.py` 中的单个 Zone 门控回归；未重启 8765、未进行真人/硬件验收。

## 2026-08-28：头控上下视角门最小修复

- 复用两段真人视频、已有人工方向标注与缓存 Pose33 做离线对比，没有重新提取 MediaPipe，也没有触发键鼠、手柄或游戏输出。对比确认：把头部俯仰全局直接映射到 Y 虽可提高上下动作命中，但会让中立画面产生大量误动，因此未采用。
- 最终保留现有横向头控语义与 PnP（透视解算）yaw 方向；2D yaw proxy 只保留为诊断信息，不再重写或抑制 PnP 符号。横向输出与基线逐帧对比 3268 帧完全一致，最大差值为 0。
- 只有当上下视角来源选择“头部”且左腕进入 `lookGate`（视角门）时，系统才捕获临时俯仰中心，并把相对俯仰持续映射为纵向视角；离开视角门会立即清除锚点并把 Y 精确归零。普通未进入视角门时仍沿用原头控状态机，不新增全局漂移风险。
- Luna Max 定向复核：语法检查通过，43 项定向测试通过。两段缓存视频的强制视角门回放中，上下宏召回率 91.91%，正确方向 92.03%，反向 2.17%，零输出 5.80%；视角门未激活与退出后 Y 均为 0。
- 验证边界：上述结果属于缓存关键点和人工片段的离线视角门回放，不等同于真人实时摄像头或游戏内手感验收；未调整 deadzone、灵敏度、gamma 或鼠标速度。

## 2026-08-30：v153 Personal PnP 接入与 world_pose 边界

- 保留现有 `gesture_v153` 横向策略及 `classic` 兼容策略；通过 `/api/head/config` 的 `horizontal_algorithm` 显式选择，不改纵向 lookGate、腕部纵向控制或冻结的头控参数。
- `pose_frame_v2` 的 normalized image pose 与可选 `world_pose` 在 `ControlKernel` 中分流：图像点只做一次 `coordinates_mirrored` 纠正并进入身体/头控，world 点只用于 v153 个人模板校准和诊断；运行期 world_pose 断流不会替换或改变已激活的横向输出。
- 电脑本地 MediaPipe Pose Full 现在把 `pose_world_landmarks` 沿同一内核边界传入；完整 replay 工具也复用该链路。状态只增加 `world_pose_available`、个人模板质量/拒绝原因等紧凑字段，不在高频状态中复制完整 world 点。
- 个人模板在世界点不足、刚体抖动过大、三组左右脸点不一致、重投影或深度无效时安全回退通用 PnP，并保留拒绝原因供诊断。
- 定向验证：`tests/test_v153_integration.py` 5 passed；头控/手机桥/内核/手部锚点相关子集 71 passed；另完成 py_compile。未做真人、真实摄像头、游戏输出或全量测试。

## 2026-09-01：frozen22 横向测量源接入

- 新增显式 `frozen22` 横向算法选项，固定使用 R3 已审计 `real-ab-equalmean-20260830-v1` 11 点/22 维 signature；保持 `classic` 默认和 `gesture_v153` 原路径不变。
- 运行时只从中心校准收集 frozen22 的中性中心、通道噪声和可选 world-face 残差；固定 signature 不会被用户数据重拟合。frozen22 通过现有相对 ratchet（相对棘轮）状态机输出水平轴，缺点/校准无效时安全归零。
- 定向验证：归档 A/B 关键点重放 1833/2695 个有效帧，特征与参考最大绝对误差 `0`、raw yaw 最大绝对误差 `0`；frozen22 选择/校准/缺点归零 3 项通过；v153 与清洁头控子集 34 项通过。未做真人或游戏输出验收。
## 2026-09-03：vertical-hand-v160 接入隔离工作树

- v160 以独立版本模块适配，`vertical_hand_control.py` 仍是生产兼容导入路径；既有 `point`、`range_y`、`deadzone` 配置和 `VerticalHandController.update/reset/status` 接口保持不变。
- 既有 `lookGate` 仍是垂直手控的安全授权门；它只限制依赖右腕垂直动作的纵向意图，不会改变头控、语音、手持或其它输出来源。断流、换源和紧停继续由内核统一归零。
- v160 的 `body_scale_normalization`、`camera_scale_deadband`、`nominal_torso_y` 使用候选脚本的内部默认值，未新增用户控件或持久化参数，避免把实验阈值误当成产品设置。
- v160 的 `reset()` 会重建锚点 deque；内核必须同步刷新历史 `vertical_anchor_samples` 别名，否则状态页会显示旧的采样数，虽然输出本身仍会被清零。

## 2026-09-04：稳健 v188 产品试用选项

- 没有继续把 Frozen22 当作直接 Mouse-X 信号堆补丁；新增显式 `gesture_v188`，保留个人化 v153/PnP 和原相对棘轮，只把 v187 的时间门、C0 零 fallback 与 v188 俯仰主导保护放在最终横向输出外层。门控不会清空或重写内部转头状态。
- 使用已提交的 RGB-only 固定窗口和同一批 9,682 帧真人 Pose33/world 数据，对产品 `gesture_v153` 与 `gesture_v188` 做离线 A/B。静止活动率 71.24%→0%，pitch-only 61.73%→2.83%，clear-yaw 错误方向帧 40→17、正确方向 256→229；回正反向残留只下降约1.3%，历史 motion 积分下降约37.2%。
- 因仍有回正残留和保守响应，网页只增加“稳健 v188（试用）”，不替换默认、不宣称最终算法。选择后沿用现有安全语义并要求重新校准。
- 定向检查共 12 passed，并完成 Python/JavaScript 语法检查；未触碰主工作树、8765 服务、手机或游戏输出，真人手感仍需用户现场验证。

## 2026-09-04：统一游戏映射与上下视角互斥

- 当前游戏的输入统一分为 `Zone 圈 / 身体动作 / 语音` 三组，每一种输入均复用同一套键盘、鼠标按钮/滚轮和 Xbox 按键/扳机/左摇杆目标。游戏 Profile（预设）仍提供开箱即用默认值，用户只在不满意时展开修改。
- 删除“右腿向左交叉/左腿向右交叉”；新增可选“开合跳、侧步开合、提膝碰对侧肘”。提膝碰对侧肘仅依赖躯干、膝和对侧肘，不要求容易遮挡的手腕或脚踝可见。
- 新增“上下控制时暂停左右”选项。默认关闭，保留左右/上下可同时控制；开启后，左腕进入上下视角门时只把最终横向轴归零，离开门立即恢复原横向头控，不改横向算法内部状态。
- 语音页面直接显示每条口令在当前游戏实际执行的动作；固定系统命令明确标记为“系统固定”，未启用的备用口令明确标记未启用。显示目录和识别执行共用 `VoiceService.command_registry`，旧版自定义别名降为兼容入口，避免两套语义相互覆盖。
- 头控校准只做代码与定向回归审计，没有修改参数。当前流程准备 1 秒、最多 6 秒内收集至少 20 个有效样本，普通模型抖动不视为失败；噪声较大时保留鲁棒中心并自动扩大稳定区，重新校准失败继续使用上一次有效中心。
- 验证边界：Python/JavaScript 语法检查；动作、v160 上下手控、场景门、游戏 Profile、输出动作、语音和头控校准相关定向测试。未把这些自动检查冒充真人游戏手感验收。

## 2026-09-05 · 真人运动视角稳定与 Zone 键盘映射修复

- 用用户提供的 85.4 秒真人视频和正式 MediaPipe Full 离线回放确认：已校准后横向头控在 959/2559 帧产生非零输出，且多数不在现有动作标签内，不能只靠 `motion_active` 阻断。
- 在 `ControlKernel` 外围增加可关闭的“身体动作时稳定视角”：用肩/肘/腕/髋/膝/踝相对髋中心、按躯干长度归一化后的速度，100 ms EMA，实测保守阈值 2.50/1.625 与 100 ms 释放保持；不改 v188/Frozen22/v153 内部算法或参数。
- 映射编辑改为变更后自动保存并立即下发；`config/game_profile_selection.json` 作为用户运行数据忽略，不再污染 Git 工作树。
- Windows 键盘输出从虚拟键事件改为扫描码 `SendInput`，提高使用 Raw Input/DirectInput 游戏对 W 等按键的兼容性；Xbox 输出路径未改。
- 只执行相关语法检查及身体门控、输出、Profile、场景布局定向测试；真人游戏体验仍由用户实际运行确认。

## 2026-09-05：v197 回正防跳与 Body Guard C2.9 网页接入

- 以用户提供的 `MotionControl-v197-return-jump-guard-bodyguard-c2.9-20260905.zip` 为冻结输入；压缩包 SHA-256 为 `2B8E69A64E6319FB5D845EEA1F9EF7E795C20BED8EFBED3549596F1533376332`。包内 `head_control.py`、`control_kernel.py` 与本次接入后的核心逻辑逐行一致（仅工作树行尾格式不同）。
- 保留持久化/接口算法值 `gesture_v188`，只把其实现版本升级为 `relative-ratchet-v197-realdata-low-angle-rearm-quarantine`，避免旧配置失效；`classic` 仍为默认，v153 与 Frozen22 入口保持不变。网页显示名更新为“稳健 v197（试用）”，切换后仍沿用原有重新校准提示。
- Body Guard 升级到 C2.9：补充 0.15 秒短暂核心点低质量宽限，并把 `horizontal_paused_by_body_motion` 改为“本帧实际拦截 Mouse-X”；网页状态栏区分提前抑制、动作后抑制和持续防晃，不再仅凭内部 guard 活跃态宣称已拦截输出。
- 定向与相关回归共 `141 passed, 19 skipped`；另通过 `py_compile`、`node --check web/app.js` 和 `git diff --check`。压缩包附带的真人数据结果属于冻结 RGB/Pose 离线回放材料；本次没有重启 8765 服务，也没有完成实时摄像头、OS 鼠标链路或游戏内真人验收。

## 2026-09-06：v207 零误触安全层与同侧救援网页接入

- 以用户提供的 `MotionControl-v207-realdata-rescue-zero-return-bodyguard-c2.9-20260906.zip` 为冻结输入；压缩包 SHA-256 为 `A01AAC9417BAD082E838D69F80D6ABFE53A6A26F0D0DBC4D4A4C5B6DAB4C5236`，包内清单 `10/10` 文件哈希匹配。
- 包内 `head_control.py` 作为 v207 累计实现接入，与本次生产文件逐行一致（仅工作树行尾格式不同）；包内 `control_kernel.py` 与已有 Body Guard C2.9 完全一致，因此不重复修改内核。v207 增加 v202 输出层俯仰/回正安全闸及 v205 同侧误锁救援，并保留 v197 的低幅反向重开静音。
- 网页显示名更新为“稳健 v207（试用）”，持久化/API 值继续使用 `gesture_v188`，`classic` 默认、v153、Frozen22 和重新校准流程均保持不变。
- 相关回归共 `144 passed, 19 skipped`，并通过 Python 编译、`node --check web/app.js` 与 `git diff --check`。包内真人指标来自冻结 Pose Full 离线回放，原始回放输入不在压缩包内；本次未重启正在运行的 8765 服务，也未宣称实时摄像头、USB 手机、OS 鼠标或真实游戏验收。

## 2026-09-10：合并手区与侧抬脚/头顶跳跃区

- 运行时把左右手各自的两个旧圆区合并为一个连续触发区；保留旧四个标识作为只读兼容别名，实际只评估/派发新的 `leftHand`、`rightHand`。
- 左右脚区改为脚踝外侧且略高于站立脚的触发空间，用于侧向抬脚/跨步；头顶新增由鼻点进入上方空间触发的 `headJump` 区。默认映射为左手 X、右手 B、头顶跳跃 A、左右脚 LB/RB，均可在游戏设置中修改。
- 首次场景定位不再要求完整人体：只需头部定位点和双肩稳定可见；缺少髋/脚时按肩宽比例估算初始脚区，之后可在页面拖动微调。首次定位和真实动作触发仍需用户现场确认。
- 兼容性定向检查：场景布局与最小控制共 `36 passed`；另通过 Python 编译、`node --check web/app.js` 和 `git diff --check`。


## 2026-09-14：MotionControl 2.0 开始实施

- 已刷新远程记录并核对本地分支，基线为 b52967f；创建 codex/motioncontrol-2.0-20260914。
- 本次仅改电脑端界面、状态反馈与游戏配置保存，保留模型、算法、手机协议和实体手柄语义。
- 保留既有 tools/replay_pose_video.py 修改、未跟踪的标注资料、回放结果及其他文件，不纳入本次提交。
- 运行源码基线 901392 字节；其中前端 106315 字节；逐文件明细见 docs/V2_BASELINE.json。

### 2.0 第一批：游戏配置持久化

- 自定义映射改为按游戏保存；恢复默认只影响当前游戏。旧配置原字节备份后迁移，磁盘替换成功后才更新内存。
- 增加目标游戏核对并保留无标识旧调用；迁移损坏数据时保留原文件。相关配置测试已通过。

### 2.0 第二批：电脑端整理与交付

- 三个入口、顶部固定主操作、唯一的区域编辑器、原生弹窗和按需诊断已完成。去掉隐藏兼容按钮、重复样式和手动映射保存流程。
- 保存串行、跨游戏持久化、目标游戏冲突核对、失败草稿重试、人体与语音独立启动已接通。服务和页面标记 2.0，手机协议和算法保持兼容。
- 78 项配置／区域／输出检查和 3 项请求处理检查通过；离线浏览器流程通过，三种目标分辨率主操作与紧急停止均在首屏。
- 含启动入口及语音子进程的运行源码 906460 → 894082 字节；前端 106315 → 91383 字节。非空行 16440 → 16733，增量与展开样式及失败处理相关，不通过压缩排版凑指标。详见 docs/V2_SOURCE_METRICS.json 与 docs/V2_DELIVERY.md。
- 模型、真实摄像头、真实识别和游戏未运行，自动检查不作为真人体验验收。保留既有回放脚本与其他用户修改。

### 2.0 继续执行：冲突恢复与重试交互

- 其他页面改变游戏后，冲突草稿可通过明确的重选按钮恢复原游戏并保存。
- 去掉文本输入失焦时重复保存，避免保存状态改变使重试按钮移位、丢失点击。扩展浏览器检查已通过（8 个保存请求，最大并发 1）。
- 最终源码字节：前端 106315 → 92142；运行源码 906460 → 894841。完整统计以更新后的 docs/V2_SOURCE_METRICS.json 为准。

## 2026-09-14：跟随区域扩大手区并修复跳跃区不可触发

- 定位到用户一直运行在 `body_relative_provisional` 模式：`/api/scene/status` 返回 `configured:false`，`config/` 下只有 `scene_layout.example.json`，从未记录过参考场景。因此本轮只改跟随区域，fixed 圆形区域与 `scene_layout.py`、网页区域编辑器均未触碰。
- `headJump` 原先每帧锚在 `nose.y - 0.30*torso` 再经 `_smooth_rect` alpha=0.50 平滑，约 170 ms 即追上鼻子，而起跳滞空 400~600 ms，目标始终高于鼻子，实际无法进入。改为持久锚点：横向时间常数 0.35 s 正常跟随站位变化，纵向 1.5 s 且在肩髋同向垂直速度 ≥0.35 torso/s 时完全冻结，落地后自动恢复收敛；偏差超过 1.2 torso 直接吸附，避免锚点被冻结后滞留。
- 冻结信号在区域代码内独立计算，未复用 Body Guard 的 `coherent_vertical_speed`：`_update_zones_locked` 执行在 `_update_body_motion_guard_locked` 之前只能取到上一帧值，且 guard 可被用户在设置中关闭，挂靠会使跳跃区静默失效。
- 左右手区改为按边界构造，覆盖各自一侧的画面上方角落；下边界自髋部上移，实测与自然垂手手腕保持约 0.75 torso 间距，内沿与 `headJump` 不重叠。区域面积由 0.0317 增至 0.1752（5.53 倍），侧举手不再需要抬到头部高度。
- 补齐跟随区域几何的回归覆盖——此前仅有 `headJump` 标识存在性断言。新增 4 项：起跳可触发、自然站立不误触且侧举可触发、侧移跟随而纵向不动、落地后冻结释放。其中起跳与侧举两项在改动前确认失败，据此证明复现了原问题。
- 验证：`tests/` 共 `236 passed, 21 skipped`；另通过 `py_compile` 与 `git diff --check`。帧率无关性在 20/30/45/60 FPS 下实测一致（起跳均触发，侧移收敛 0.1000，落地复位误差 ≤0.0017）。
- 已知遗留（与本轮无关，改动前后均失败）：`tests/test_hand_anchor.py::test_kernel_zones_consume_anchor_when_wrist_is_missing` 仍断言手区合并前的旧按键 Y，而合并后 `leftHand` 派发 X。`388215a` 合并手区时更新了 `test_minimal_controls.py` 与 `test_scene_layout.py`，漏掉了该文件。
- 以上均为合成姿态自动检查，未运行模型、真实摄像头或游戏，不等同于真人体验验收。自然垂手不误触、小幅起跳触发头顶区、侧移后手区仍对得上，仍需用户现场确认。

## 2026-09-14：脚区放大与固定区域切换确认

- 真人试用反馈脚区偏小。量化确认比"偏小"更严重：侧抬脚需同时向外移 0.48 torso 且抬高 0.23 torso 才进入，三种模拟动作（侧抬 0.15 torso、侧迈 0.5 torso、侧抬+外移各 0.25 torso）全部无法触发。
- 参数扫描显示只延伸下沿无法解决：任何下沿取值下自然侧抬脚仍因 x 内沿受限而不触发，而下沿压到地面又会让极宽站姿误触。改为两轴分工——`floor_y` 跟随支撑脚，因此地面高度的脚**与站姿宽窄无关**地被 y 间隙排除；y 间隙独自承担防误触，x 只负责够得着，于是可安全加宽。
- 脚区取 `foot_w 0.58 → 1.00 * torso_px`、`foot_h 0.52 → 0.60 * torso_px`、中心偏移 `0.40 → 0.36`。面积增至 1.99 倍，内沿内移 0.063、下沿下移 0.032；侧抬脚所需外移量由 0.48 torso 降至 0.22 torso，站立脚与下沿保持 0.10 torso 间隙。
- 记录参考场景是单向操作（跟随区域将永久失效、头顶跳跃改固定判定），此前经"调整区域位置"和电脑摄像头首次开始两个入口静默触发。两者都经 `ensureInitialSceneLayout`，故在其中加一次显式确认弹窗；默认按钮为取消，Esc 不视为确认。
- 验证：`tests/` 共 `238 passed, 21 skipped`（唯一失败为既有的 `test_hand_anchor.py` 陈旧按键断言，与本轮无关）。新增脚区 4 站姿／3 动作用例、手脚区不重叠用例和确认弹窗用例。另通过 `py_compile`、`node --check web/app.js`、`git diff --check`。
- 弹窗在真实页面实测：取消与 Esc 均返回 false、确认返回 true，验证后场景仍为 `not_configured`，未改变运行状态。既有离线浏览器检查的 mock 为 `configured:true`，会在弹窗前 early return，因此覆盖不到该弹窗。
- 脚区新参数为合成姿态推导，未经真人实测；侧抬脚是否跟手、以及自然站立与走动是否仍不误触，需用户现场确认。

## 2026-09-15：手机固定接入地址与无线 adb 设备识别

- 手机每次都要重填地址，原因有两个且叠加：热点网段跟随手机自身上游网络变化（一次会话内实测 10.246.192.18 → 10.57.177.18 → 10.245.40.245，整个网段都变），且本仓库只含 PC 端，Android 端为独立项目，PC 侧单独加 mDNS/UDP 发现无法生效，自动发现必须两端同时支持。全仓确认无任何发现或地址持久化机制，`broadcast_*` 均为已连接 peer 的业务消息广播。
- 改用 `adb reverse tcp:8765 tcp:8765`，手机端地址固定为 `ws://127.0.0.1:8765/ws/input`，与 Wi-Fi 地址无关。无线调试下同样可用，无需 USB 线，也不必让电脑反过来开热点牺牲上网。
- 实测：手机侧 `curl http://127.0.0.1:8765/` 返回 200；`/ws/input` 握手返回 101 并立即收到真实 `control_config_v1`（当前游戏与六区映射），确认整条链路可用而非仅端口连通。
- 修复 `START.ps1` 漏建隧道：无线 adb 的序列号是 mDNS 名称，可能自带空格（实测 `adb-AU7KVB4509002217-xcLa6O (2)._adb-tls-connect._tcp`），原检测正则 `^[^\s]+\s+device` 不匹配，且 `-split '\s+'` 取序列号会在空格处截断。改按 `adb devices` 实际使用的制表符分隔，USB 与无线同时适用。
- 该分支验证覆盖 USB 序列号、含空格的无线序列号、表头行、`offline` 与 `unauthorized`——后两者不建隧道，未授权设备不会获得通道。新增对应回归断言。
- 验证：`tests/` 共 `240 passed, 21 skipped`（唯一失败为既有的 `test_hand_anchor.py` 陈旧按键断言，与本轮无关）；PowerShell 解析检查与 `git diff --check` 通过。
- `adb reverse` 不跨 adb 服务重启、手机重启或无线调试断开保留，需重新建立；`START.ps1` 每次启动会自动重建。本轮未做真人体感与游戏输出验收。

## 2026-09-15：区域按键标注、语音回归修复与取景问题

- 接续上一轮五项修复中未完成的部分。核对结果：踏步与脚区（7fca6ff）、语音按住／松开（5ecd452）已提交；物理手柄合流为运行时未开启，现已确认生效（`xinput_merge_enabled` 为真、手柄已连接）；区域按键标注未完成。
- 区域标注缺的不是内容而是可读性：`renderKernelZones` 早已按真实映射写入按键名，但 `.zone strong` 未设字号，继承 14px。改为用容器查询单位跟随区域短边缩放并加 clamp，各区域尺寸相差数倍也不溢出；游戏悬浮窗的画布字号同样由固定的整窗比例改为跟随区域短边。
- 标签内容统一为纯按键名。此前手区显示 `X`，脚区却显示 profile 里的描述性 `左脚区 · LB`，长串会被自适应字号压小，正好与"大字标注"相悖。身体部位仍保留在 `<small>` 中。
- 修复 5ecd452 引入的四项测试回归（该提交未跑完验证即合入）。三项是 `normalize_action` 现在显式记录 `behavior` 导致的精确字典断言过期；一项是 `test_voice_profile_binding_is_edge_triggered` 断言显式 `hold` 会被改写为 `tap`，而解除该改写正是本次语音按住的目的。核对确认语音默认仍为 `tap`，`release` 仅限语音映射，且断开、改配置、切游戏、停输出均会清理锁存，故改为断言默认值，显式形式由 `tests/test_voice_hold.py` 覆盖。
- 补提交 Codex 未提交的 `config/voice_mappings.json`：新增"保持右肩键"（RB hold）与"松开右肩键"（RB release）。
- 实时取景诊断：手机为 480×864 竖屏，脚踝 y 持续在 1.47~1.50、置信度恒为 0，即双脚在画面外，内核按 0.4 阈值正确拒绝，脚区从未渲染。脚区几何调整在当前机位下不可能生效，需先让摄像头拍到脚。另记录一处退化情形：玩家贴近画面边缘时该侧手区会被压缩至零宽，未做最小宽度兜底，因为在无空间处强行造区域会带来误触。
- 验证：`tests/` 共 `253 passed, 21 skipped`（唯一失败为既有的 `test_hand_anchor.py` 陈旧按键断言）；`node --check web/app.js` 与 `git diff --check` 通过；区域标签与字号在真实页面实测。真人手感与游戏输出未验收。

### 补充：区域形状与静态资源缓存

- 跟随模式的区域在内核中按矩形判定，页面却无条件以 `border-radius: 50%` 画成椭圆；`renderKernelZones` 切换的 `circle-shape` 类在样式表里没有任何对应规则。内嵌椭圆只占矩形面积的 π/4，约 21% 的真实触发区落在图形之外且集中于四角，而手区正是贴着画面角落的。改为默认矩形、`circle-shape` 才用椭圆，与悬浮窗画布一贯的区分方式一致。
- `web/` 下的静态资源由 `SimpleHTTPRequestHandler` 默认处理，只发 `Last-Modified`，浏览器据启发式缓存会在文件改动后继续使用旧的 `app.js`／`app.css`，表现为改动"没生效"。`_serve_file` 的 `no-store` 只覆盖模型文件与场景参考图。改为在 `end_headers` 统一补 `Cache-Control: no-store`，已有该头的响应不重复发送（实测 API 路由仍只出现一次）。

## 2026-09-15：起跳不再中断行走

- 核实"软件不支持同时识别"不成立：踏步是动作、头顶跳跃是区域，互斥规则只约束开合跳与双手过头，两者输出分别为左摇杆与按键。实测腾空瞬间二者同时出现在 holds 中。
- 真正的限制是时序：起跳时双脚同时离地，观察不到左右交替，`active_until` 停止续期，行走固定在最后一次交替后 0.70 秒失效。实测修复前无论滞空多久都在 0.67 秒中断，而真实起跳滞空约 0.4~0.8 秒，因此半空中会掉前进。
- 起跳属于节奏中断而非停止意图，故在跳跃区按下期间维持已在运行的行走。仅维持、不启动：站立起跳不会产生前进；停下不跳仍在 0.70 秒失效；长时间悬停因跳跃区锚点回升而最终停止，不会无限前进。区域在动作之前评估，读到的是当前帧。
- 定向验证：滞空 0.8 与 1.0 秒由 0.67 秒中断改为全程保持；站立起跳不产生行走；新增对应回归用例。`tests/` 共 `254 passed, 21 skipped`（唯一失败为既有的 `test_hand_anchor.py` 陈旧按键断言）。
- 记录两项与语音相关的事实核查，均无需改动代码：其一，语音识别没有固定词表，`VoskCommandRecognizer` 接受任意非空短语且 `unsupported` 恒为空，grammar 按单字 token 构建，识别效果取决于声学模型而非白名单；其二，按游戏的 20 项语音映射本就可选任意按键与"持续按住／松开"，实测其类型与方式下拉均已包含。当前真正的限制是这 20 项的口令文本固定，自定义口令则是全局生效。

## 2026-09-15：自定义口令改用按键选择器

- 自定义口令的输出目标原为纯文本框，需手输 `RB`、`HEAD.CENTER` 之类的字面量；按游戏的映射列表早已使用 `fillTargetControl` 生成的下拉。改为复用同一控件，两处行为一致。
- 系统命令另配专用下拉。派发端只接受 `OUTPUT.START`／`OUTPUT.STOP`／`HEAD.CENTER`／`HEAD.CALIBRATE`（含 `HEAD_CALIBRATION_START`）／`SCENE.CAPTURE_REFERENCE`／`SCENE.REMATCH`，其余一律返回"不支持的系统语音命令"，此前手输错字只能在游戏中表现为静默无反应。
- 键盘类型保持自由输入，组合键需要；Xbox 键沿用 `fillTargetControl` 自带的"组合键…"入口。语音行可能先于动作目录渲染，而该函数对 gamepad 自带内置候选表、仅 keyboard 的自由文本依赖目录，故兜底只针对目录缺失时的键盘。
- 读取端改按 `.binding-target` 取值：该类始终落在当前生效的控件上，组合键模式下也只有可见的那个持有它。
- 实测四类取值：Xbox 键与系统命令均为下拉（15 项／6 项）且回填正确，键盘仍为文本框；21 行全部解析成功、无残缺行。`tests/` 共 `254 passed, 21 skipped`（唯一失败为既有的 `test_hand_anchor.py` 陈旧按键断言）。未做真人语音识别验收。

## 2026-09-15：备用语音口令改为可编辑

- 手机麦克风下自定义口令无法识别：识别发生在手机端，而 PC 没有任何向手机下发口令短语的通道（手机上行仅 `voice_text` 与 `voice_command(command_id)`），手机词表只有随应用分发的 39 条目录，因此自定义短语手机不会吐出、PC 也就无从匹配。自定义口令仅适用于电脑麦克风。
- 目录中 `game.profile_slot_01`~`12`（体感功能一～十二号）正是留给用户指派的备用口令，手机认得，后端也支持绑定，但 `profileTriggers()` 将其无条件排除出触发器列表，网页上没有任何入口可以赋予动作；而语音帮助只在其"已启用"后才列出，形成死循环。
- 改为纳入列表并单列"语音 · 备用口令"分组（默认折叠，不打乱原有 20 项）。分组仅按新增的 `slot` 标记区分显示，`group` 仍为 `voice`——该名称同时决定行为下拉是否提供"持续按住／松开"，并用于寻址已保存的绑定，改动它会写错位置。
- 实测：新分组 12 项，slot_01／02 正确回填既有的 START／BACK，slot_03 的类型下拉 6 项齐全、行为下拉含 tap/hold/release。`tests/` 共 `254 passed, 21 skipped`（唯一失败为既有的 `test_hand_anchor.py` 陈旧按键断言），新增对应回归断言。未做真人语音识别验收。

## 2026-09-15：语音词表改为由电脑下发

- 手机端此前把 grammar 硬编码在 `NativeAudioPlugin.java` 的 `COMMAND_GRAMMAR` 常量里，与电脑各自维护一份并已经漂移：该常量含"体感确定""体感往上"等同义词而电脑目录没有，电脑的"体感背包／技能／换弹／跳跃／互动／使用物品／打开菜单"以及 12 条 `game.profile_slot` 该常量又没有。因此在网页上新增的口令只有电脑麦克风听得见，手机麦克风永远识别不出，与填写是否正确无关。手机源码仓库为 `F:\switch`（gitdir `I:\switch_git`），其 `2026-09-06 14:50` 的提交与手机上 APK 的安装时间一致。
- 电脑侧把短语表抽成 `VoiceService.grammar_phrases()`，识别器与下发共用同一份，并随 `control_config_v1` 发给手机。复用既有消息而非新增类型：手机本就在接收、断线会缓存、配置变更时电脑会自动重播。
- 手机侧 `NativeAudio.start()` 接受可选 `phrases`，据此构建受限语法；单字空格分隔并追加 `[unk]`，与电脑 `_grammar_entry` 的产物一致。缺少该字段时仍回退到原常量，供首次启动和尚未收到配置的情况使用。收到的短语表与当前识别器不一致且正在监听时重建识别器——语法在构建识别器时固定，仅靠重播配置不会生效。
- 构建与安装：`npm run check`、`npm run android:sync`、`gradlew assembleDebug` 均通过，覆盖安装成功（`lastUpdateTime 2026-09-15 13:39:52`）。实测重启后的服务在 `control_config_v1` 中下发 78 条短语，含"体感保持左肩键""体感松开左肩键"及"体感功能一～十二号"。
- 验证：`tests/` 共 `257 passed, 21 skipped`（唯一失败为既有的 `test_hand_anchor.py` 陈旧按键断言），新增短语表完整性与下发字段断言；另通过 `py_compile`、`tsc --noEmit` 与 `git diff --check`。真人语音识别尚未验收——手机需重连并重新开启语音开关后才会用新词表构建识别器。

## 2026-09-15：组合键支持按键与摇杆混合

- 手柄按键与左摇杆在配置模型里属于 `gamepad` 与 `gamepad_axis` 两种类型，组合键只在按键集合内校验，因此 `LB+LS_UP` 被拒（"不支持的 Xbox 按键：LS_UP"），无法表达"按住肩键同时推摇杆"。
- 输出层本就把两者分开维护（`_button_sources` 与 `_left_stick_sources` 各自刷新），同时输出一直是支持的；限制只在配置校验与派发。故按类型分流而非新增动作结构：`gamepad` 组合键中的按键进按键通道、方向进摇杆通道，一个触发器即可同时驱动两路。
- 新增 `_gamepad_parts()` 统一拆分，`set_holds`、`execute_action` 的持续按住与 `tap_gamepad` 的点按三处共用，点按会同时脉冲两半而非只按按键。摇杆那半沿用既有合流规则：物理手柄合流期间，仅在用户开启"体感驱动左摇杆"时才移动摇杆。
- 单独的方向仍归 `gamepad_axis` 类型，组合键里写单个方向会被明确拒绝并提示改选类型，避免同一语义出现两种写法。
- 实测：持续按住 `LB+LS_UP` 得到按键 `('LB',)` 与摇杆 `(0.0, 1.0)`；再叠加 `A+LS_LEFT` 后按键为 `['A','LB']`、摇杆合成 `(-1.0, 1.0)`，与多个独立摇杆源的求和方式一致；释放后两路归零；语音的按住与松开同样覆盖两半。
- 验证：`tests/` 共 `260 passed, 21 skipped`（唯一失败为既有的 `test_hand_anchor.py` 陈旧按键断言），新增混合组合键、语音按住松开与单方向拒绝三项用例；另通过 `py_compile`、`node --check` 与 `git diff --check`。未做真人游戏验收。

## 2026-09-15：组合键改为勾选，姿势可选持续按住

- 组合键原为自由文本框，而合法名称本就是固定集合，手输的错字只能在保存被拒时才暴露。改为逐项勾选，选项含 14 个手柄按键与 4 个左摇杆方向，选中项同步到隐藏字段供既有读取路径使用，无需改动保存逻辑。
- 复选框不会产生 input 事件，其 change 是唯一信号；而此前为修复重试按钮位移加的守卫会拦掉全部 input 元素的 change，导致勾选不触发自动保存。守卫收窄为排除复选框。
- 解除姿势只能"进入时触发一次"的限制。`normalize_bindings` 与 `_merge_bindings` 在默认值已是 `tap` 的基础上又强制改写了一次，使"姿势持续期间按住"无法表达，而识别器对姿势的释放与动作并无二致。现改为仅保留默认值，持续按住可显式选择；滚轮的强制改写保留——按住会无限滚动。网页端同步去掉 `tapOnly` 并让姿势的默认显示与服务端一致。
- 实测：双手交叉勾选 LB 与 LS_UP、方式选持续按住后自动保存成功，落盘为 `{"type":"gamepad","target":["LB","LS_UP"],"behavior":"hold"}`。过程中发现服务需重启才会加载新的校验代码，旧进程仍会拒绝 `LS_UP`。
- 验证：`tests/` 共 `261 passed, 21 skipped`（唯一失败为既有的 `test_hand_anchor.py` 陈旧按键断言）。更新了两项断言旧约束的既有用例：滚轮强制 tap 保留、姿势改为断言"默认 tap 且可显式 hold"。另通过 `py_compile`、`node --check` 与 `git diff --check`。真人游戏手感未验收。

## 2026-09-15：混合组合键补齐内核路径并让按键领先摇杆

- 上一轮只改了 `set_holds` 与 `execute_action`，漏了 `set_action_holds`——区域与姿势正是走它，其中仍在调用只认按键的 `_gamepad_targets`，因此双手交叉绑定 `LB+LS_UP` 后每帧抛出"不支持的 Xbox 按键：LS_UP"。这是该绑定始终无效的直接原因。
- 部分游戏要求先按住修饰键、随后才接受方向，同一份手柄报告里同时到达会被忽略（《神秘海域4》爬绳需要先 LB 再向上）。实测监控确认输出本身正确：语音锁存期间按键 LB 与左摇杆源同时存在并持续 25 秒，游戏无反应。故让组合键中的按键领先摇杆 0.08 秒。
- 领先时长不能从"当前已施加的内容"推断：体感触发每帧重建整组输出，摇杆会被清掉后重新排期，定时器永远等不到触发。改为按源记录起始时刻并以时间差判定；定时器仅用于语音锁存这类无人重入的一次性调用补一次施加。触发在领先期内结束时会取消定时器并清除记录，摇杆不会事后才推出去。
- 默认值调整：启动时若恰好连接一个物理手柄则自动开启合流（数量不为一或未连接时保持关闭，避免歧义），体感源默认改为手机摄像头（`LocalControlRuntime`、`InputBridge` 与网页三处一致）。此前曾以修改 `START.ps1` 的方式实现后者并被回滚，本次改的是真实默认值而非启动脚本。
- 注意：开启合流会把输出模式提升为 `gamepad`，头控随之由鼠标改为右摇杆，这是合流"只有一份虚拟手柄报告"的既有语义。
- 验证：`tests/` 共 `263 passed, 21 skipped`（唯一失败为既有的 `test_hand_anchor.py` 陈旧按键断言）。新增领先时序与"领先期内释放不得补推"两项用例；既有两项混合组合键用例改为显式将领先置零，避免与时序用例重复覆盖。另通过 `py_compile`、`node --check` 与 `git diff --check`。真人游戏内是否因此可以爬绳尚未验收。

## 2026-09-15：姿势的持续按住在内核侧被改写

- 语音触发 `LB+LS_UP` 已可在游戏中爬绳，姿势触发却无效。原因是内核派发处第三次强制改写：`_dispatch_controls_locked` 对任何 `pose.` 前缀的绑定无条件设为 `tap`，使配置里保存的 `hold` 永远走不到持续输出分支，只发一次脉冲。此前已解除的两处在 `game_profiles.py`，内核这处遗漏。
- 改为把 `tap` 作为姿势的默认值而非改写：未指定时仍是进入触发一次，显式 `hold` 则进入持续输出并随姿势结束释放，与动作一致。识别器对姿势与动作的释放本就没有区别。
- 更新既有用例 `test_cross_pose_dispatches_once_on_entry_not_every_frame`：原用例显式绑定 `hold` 并断言被改写为 `tap`，改为省略行为以验证默认值，其"每次进入只派发一次、离开后重新武装"的断言全部保留。新增内核层的姿势持续按住用例。
- 运行期发现两项与本次改动相关的配置问题并已处理：合流默认开启但"体感驱动左摇杆"默认关闭时，混合组合键的摇杆半会被按既有合流规则丢弃，只剩按键；`/api/game-profiles/overrides` 是整体替换而非合并，分批写入会清除未列出的既有覆盖。
- 验证：`tests/` 共 `264 passed, 21 skipped`（唯一失败为既有的 `test_hand_anchor.py` 陈旧按键断言）；另通过 `py_compile` 与 `git diff --check`。姿势触发爬绳尚未真人验收。

## 2026-09-15：输出模式默认改为手柄

- 默认开启合流后仍会被关掉：切到鼠标视角时 `set_config` 按既有语义关闭合流并清理物理手柄（合流只有一份虚拟手柄报告，鼠标模式无法承载）。而网页的视角输出默认仍是鼠标，页面首次与服务交互就把 `mode=mouse` 推了过去，刚建立的合流当场失效，模式也退回鼠标。用户配置并未丢失，三条覆盖始终在服务端。
- 三处默认值改为手柄：`OutputManager.mode`、网页 `output.mode` 与选项顺序。任一处不一致都会在每次重启后重演上述冲突，故新增断言同时检查这三处。
- 合流默认开启时一并开启"体感驱动左摇杆"，否则混合组合键的摇杆半会按既有合流规则被丢弃，只剩按键。
- 总输出开关保持默认关闭：一旦默认开启，服务启动瞬间体感便会向当前焦点窗口发送按键，此时尚未校准和站位。
- 重启后未做任何手动设置，实测为手柄模式、合流生效、体感可驱动左摇杆、体感源手机摄像头且已连接。`tests/` 共 `265 passed, 21 skipped`（唯一失败为既有的 `test_hand_anchor.py` 陈旧按键断言）。

## 2026-09-20：生产网站开放玩家邀请码

- QQ 群统一改为 `755359297`，覆盖云端反馈页、电脑端页脚、网盘说明和说明生成脚本，避免后续重新打包又写回旧群号。
- 已登录玩家新增“邀请朋友”页面。账号注册满 24 小时后可生成邀请码，之后每 7 天最多生成 1 个；邀请码 7 天过期且只能使用一次，明文只在生成成功时显示。
- 冷却时间存入用户表，服务重启和频率计数清理都不会绕过。生成时使用数据库条件更新作为并发闸门，同一账号同时请求只能成功一个；另有账号每小时 3 次、来源地址每小时 10 次的接口频率限制。
- 数据库迁移只给 `users` 新增可空的 `last_invite_created_at` 字段，不改已有账号、邀请码或配置数据。验证：云端 `38 passed, 7 skipped`，网站类型检查与生产构建通过。

### 推广限制调整

- 原拟“注册满 24 小时、每 7 天 1 个”会妨碍推广，部署前取消。最终规则为注册成功后立即可生成、每个账号滚动 24 小时最多 3 个，连续生成至少间隔 10 秒；邀请码仍为 7 天有效且只能使用一次。

## 2026-09-20：启动器避免重复实例

- 一次重复启动在第二个进程绑定 `0.0.0.0:8765` 时触发 `WinError 10013`。排除系统保留端口、防火墙规则和持续占用后，单独绑定 `127.0.0.1:8765`、`0.0.0.0:8765` 及正式服务均成功，确认不是永久端口故障。
- `START.ps1` 在建立手机反向通道后先检查 `127.0.0.1:8766` 的现有控制页；已有健康实例时只打开页面并退出，不再创建第二个服务。新启动路径仍使用原来的 `python -X utf8 server.py`，不改变解释器选择。
- `.gitattributes` 固定 Windows 的 `.bat`／`.ps1` 为 CRLF，避免自动补丁把批处理改成 LF 后被 `cmd.exe` 拆成残缺命令。实际重复执行 `START.bat` 后提示“已在运行”，服务进程号保持 `29240`，手机连接与控制页均正常。


## 2026-09-20 本地双手分轴实验

- 从 a001630 建立 codex/experiment-split-hand-axes，本地实验，不推送、不部署。
- 默认右手水平、左手垂直，按手复用既有握拳控制器，独立起点；支持交换或同手全向。
- 旧单手字段迁入分轴配置，保留阈值和灵敏度。切换设置、人体失联时清除握拳起点和输出。
- 主版本使用新增功能版本，网页独立热更新版本维持原值。
- 实际发现手机仅按单手裁图，因此手机仓库建立同名实验分支，按电脑下发的去重手列表依次识别；保留旧 hand 字段兼容旧电脑。网页实验版本从电脑版本源读取。
- 验证：隔离环境完整测试 752 passed、21 skipped、6 xfailed（8 项依赖弃用提示）；手机网页构建成功；浏览器模拟状态下已检查默认分配、交换左右手和保存请求。未进行真人摄像头或游戏验收。
- 本地手机网页 dist 已签名；未推送、未连接 aliyun、未部署、未替换正在运行的服务。初次完整测试失败来自测试环境缺少 httpx、sqlalchemy、greenlet，已在隔离环境补齐，未修改现有运行环境。

## 2026-09-20 优先接入用户提供的头控回正修复

- 按用户最新指示暂停旧测试清理，优先替换 motioncontrol/head_control.py；替换源 D:\Download\head_control_return_fix.py。
- 压缩包 head-feel-kit.tar.gz 内的基线源码与替换前源码按文本完全一致。新文件原样复制，双方 SHA256 均为 1BC5DC94373B2D152C2A7F9FB13D6A99434A7827D05070278412DFE6C6675372。
- 新增左右两侧的小幅回摆与真实回正测试。小回摆序列在旧版触发 RETURNING，新版持续输出；真实回正序列两版均在第三帧（约 0.1 秒）归零，到中心保持静默。测试最初要求第二帧归零，但与压缩包原版实际行为不符，已按两版对比确认的基线检查第三帧。
- 保持现有实验版本；不重启服务、不部署。历史录像包缺少精确归一化所需参数，不声称完成该录像的精确回放或真人手感验收。
- 完整回归：756 passed、21 skipped、6 xfailed，8 项依赖弃用提示；原有跳过与预期失败标记未改动。

## 2026-09-20 回正修复真人分段观察

- 收到用户“开始”后完成静止、右转、左转、小回摆四段状态采样；未录视频。记录和分析在 output/head_feel_2026-09-20/return_fix_live_report.md。
- 静止段未见鼠标漂移；重新校准后两侧转头均出现输出，但小回摆段起始连续约 16.9 秒处于回正锁定，另有约 1.6 秒姿态估计无效。尚不能认定通过真人验收。
- 方向按指令与鼠标坐标疑似相反，等待用户反馈。未修改方向、算法或脚本；没有同条件新旧对照，不能报告改善百分比。
## 2026-09-25：未完成提膝碰肘不再误判踏步

- 在独立副本 \`F:\MotionControl-App-pose-library-next\` 继续 \`feature/pose-library\` 的未提交工作，原工作树未修改。
- 复现并修复同时绑定“提膝碰对侧肘”和“原地踏步”时的缺口：膝盖已抬起、对侧肘已接近但还没碰到，脚踝到峰值后不再触发踏步；真正碰到对侧肘仍优先识别为提膝碰肘。
- 用接近阈值记录一次未完成碰肘候选，只影响已绑定提膝碰肘的这一趟抬腿；肘部距离较远的普通踏步保留原行为。
- 新增未完成与完成碰肘的回归用例。
- 验证：\`python -m pytest -q\` 为 1079 passed、20 skipped；\`py_compile\`、\`node --check web/app.js\`、\`git diff --check\` 通过。
- 仍未进行真实摄像头、真人动作或游戏内验收。

## 2026-09-25：量身准备倒计时与脚圈中性位置

- 只在独立副本 `F:\MotionControl-App-pose-library-next` 修改；原 `F:\MotionControl-App-pose-library` 和运行中的 `F:\MC-main` 未改动。
- 量身开始增加 3 秒准备阶段：点击后先回到镜头前，倒计时结束才采集站定基准；教学文案改为“动作圈不合适”，按钮明确显示 3 秒后开始。
- 根据真人姿态录制 `C:\Users\Lenovo\AppData\Local\MotionControl\recordings\pose-20260925-020232.jsonl` 重放，修复脚区中性位置在抬脚过渡帧被追随的问题；握拳占用左手时左手区域仍按既有规则暂停。
- 验证：量身、脚区和最小控制测试 75 项通过；排除当前环境缺少依赖的云端与场景测试后，其余 983 项通过、14 项跳过。全量收集仍受 `sqlalchemy`、`argon2`、`cv2` 和本机 `pywin32` 环境差异影响。

## 2026-09-25：按键框直接录键、身体动作收进动作库、脚区与手区防误触、踏步按脚下缘

- 在 `F:\MotionControl-App` 从 `feature/zone-fit-keep-calibration`（c7a4c56，即 MC-main）新开 `codex/key-capture-motion-defaults`；两个提交，按用户要求快进合并进 MC-main 的 `feature/zone-fit-keep-calibration`；未推送。
- 键盘键位框（映射表、通用口令、键盘宏步骤）改成点一下再按键：按什么就换成什么，按住组合就是组合键，全部松开才保存；不支持的键在框下提示，浏览器占用的组合（CTRL+W 等）录不了。
- 所有游戏默认绑定原地踏步、小腿向后抬起：有手柄输出的游戏推左摇杆上/下，一个手柄输出都没有的 69 个键鼠游戏按 W/S（规则在 `profile_schema.with_default_bindings`）。映射表的身体动作只列绑了键的和本次从动作库加进来的，其余在动作库/自定义动作卡片上点「加到映射」。
- 量身和运行时的脚：地面线、离地高度都按脚踝/脚跟/脚尖里最低的点（`zone_fit.foot_bottom_y` 等），脚区因此放低；脚区的"确实往外抬脚"改用同一套量法，站立基准在两脚着地且静止时跟随，不再只记第一帧。
- 手区：进框至少 0.04、单手待 0.07 秒、两只手同时在框里待 0.3 秒才按，松开速度不变。
- 踏步：抬没抬脚改为两脚下缘直接比、扣掉站立时的高低差，不再相对各自的胯（骨盆一歪站着的脚就被算成一步）；门槛 0.07/0.04/0.8 秒未改。
- 验证：1071 passed、2 skipped。用 10 段真人录制（recordings/pose-20260925-*）回放对比：侧踢时的两次误判踏步消失；左脚区按住从 0.4 秒到 0.7 秒；踏步中途的一次断开消失；举双手扫过手区误按 24 次→4 次；左手快速挥手 9 下→7 下。未做真人或游戏内验收。
- 已停掉 MC-main 的服务进程（未关它开的 Chrome），用本分支 START.ps1 启动。

## 2026-09-25：口令按模型词表拆词，认不出的字当场提示；手机照电脑拆好的 grammar 听

- 起因：问「体感爬绳」能不能用。能——四个字都在 vosk-model-small-cn-0.22 的词表里。但查下来常用 3755 个汉字里有 670 个查不到单字（「堡」「础」「啡」），字母、数字、标点一个都没有；写进口令的话 Vosk 只是悄悄丢掉，那句说多少遍都听不到，界面上什么都不说（`unsupported` 一直是空列表，从来没查过）。
- 引擎不换。2026-08-28 的对比里 Vosk 受限词表是唯一过线的候选（意图 92.2%、紧停召回 96.7%、误触 1.4%），Sherpa KWS 4.4%，Vosk 开放识别 33.3%；FunASR、Sherpa 通用 ASR、whisper 没测。
- `voice_backend.grammar_tokens`：能按单字就按单字（词表齐全的口令拆出来和以前一模一样），某个字单独查不到才拼成词表里的整词（「城堡」「咖啡」「舞蹈」这类因此能用），拼不上的字记进 `unheard`，那句口令不进 grammar。查词用 `Model.vosk_model_find_word`，老版本 vosk 没有这个方法就当都在。口令先过 `compact_text`，带标点的口令以前会把「，」当成一个词，现在折掉。
- 提示：`POST /api/voice/check` 给输入框用；本游戏口令、通用口令（含急停）、唤醒词三处输入框底下写「「笛」语音认不出，这句说了也听不到，换个说法」。换游戏、装别人配置带进来的，在语音页顶上一行汇总（`status.unheard`）。
- 手机：控制配置多一个 `voice_grammar`（电脑实际交给模型的那份，已拆好词）。F:\MC-switch 的 main.ts 把它和原来的 `voice_phrases` 一起交给原生层，NativeAudioPlugin 有 grammar 就照原样用、没有就退回逐字拆。旧手机只认 `voice_phrases`，那份没变；新网页热更到旧 APK 上，旧 APK 只读 phrases，照旧能用。
- 验证：1149 passed、3 skipped；真实模型下「体感爬绳」→「体 感 爬 绳」、「体感城堡」→「体 感 城堡」、「体感吹笛」报「笛」。手机 tsc、21 个 vitest 通过；release APK（2.2.0，MotionControl 证书）已覆盖安装到 ELI-AN00，网页包已重签。未做真人说话验收。

## 2026-09-25：恢复踏步左右交替门控，避免单腿挪动误触

- 在独立工作树 `F:\MotionControl-App-march-fix`（分支 `codex/march-fix`）修改，`F:\MC-main` 和正在运行的服务未改动。
- 恢复昨天之前的踏步节奏：单腿完成一次抬脚只记录最近一侧；相反脚在 0.10～1.50 秒内完成后才开始或续上前进。脚下缘、站姿基准、脚跟脚尖和提膝碰肘分类保留当前版本。
- 回归测试覆盖单腿重复有效抬脚不触发，以及左右交替后正常触发；README 已同步行为说明。
- 验证：`python -m pytest -q tests/test_march_foot_separation.py tests/test_control_kernel.py tests/test_minimal_controls.py tests/test_default_motion_bindings.py tests/test_body_motion_guard_defaults.py tests/test_body_motion_head_guard.py tests/test_motion_conflicts.py tests/test_pose_rules.py` 为 149 passed；`compileall`、`git diff --check` 通过。
- 尚未重启当前服务，也未做真人摄像头或游戏内验收。

## 2026-09-25：本游戏映射的“停住语音按住”支持多选

- 在独立工作树 `F:\MotionControl-App-voice-release-multiselect` 修改；原 `F:\MC-main` 尚未合并、运行中的服务未重启。
- 映射表里的 `voice_release` 目标改成原生多选框：可以选多条当前设为“持续按住”的本游戏口令；单条旧配置仍保持字符串格式，多条保存为编号列表，重绘和口令状态刷新不会丢选择。
- 共享配置校验接受口令编号列表并去重；内核一次触发逐条释放，缺失的一条不阻止其他有效口令释放；描述和界面标签支持多条显示。
- 验证：针对性测试 30 通过、84 项配置/宏/语音回归通过；全量 `python -m pytest -q` 为 1206 通过、23 跳过；`node --check web/app.js` 和 `git diff --check` 通过。界面自动检查因当前工作树缺少 `playwright` 依赖未运行。
- 尚未合并到 `F:\MC-main`，尚未重启服务，尚未做浏览器真人操作验收。

## 2026-09-25：停住语音按住改为无 Ctrl 的下拉多选

- 在 `F:\MotionControl-App-voice-release-ui` 修改；在已合并的多口令逻辑之上，替换截图中的原生多选列表。
- 可见控件恢复普通下拉框外观；展开后每条口令直接点击切换，已选口令再次点击取消；点击菜单外关闭。隐藏的多选 `select` 继续承载保存和刷新状态。
- 验证：`node --check web/app.js`、`git diff --check` 通过；停住语音回归测试 30 通过、1 项跳过。尚未合并到 `F:\MC-main`，尚未重启服务。

## 2026-09-26：整合手机远程音频与电脑系统语音识别

- 从 `main` 建立 `codex/integrate-latest-audio`，保留主线的语音停住、多目标释放、系统功能和区域功能，再合入手机音频来源选择、手机原始音频传输和 Windows 系统语音识别；整合提交 `6046e41`。
- 修复 Windows 识别器缺少口令检查兼容接口的问题，避免设置页轮询时报错；修复识别器初始化失败时仍残留对象的问题。
- 从 `F:\switch` 分支 `codex/experiment-split-hand-axes` 构建正式签名手机包，版本从 2.2.0 递增到 2.2.1（版本号 20201），覆盖安装到已连接的 ELI-AN00，保留应用数据；手机提交 `e9d139b`。
- 实际验证：手机姿态帧持续到电脑，手机远程音频切换后电脑收到约 2.3 MB 音频且音频状态持续有效；切回电脑音频后电脑 Windows 识别器保持可用，手机姿态仍持续。电脑接口和语音短语检查返回 200；未做真人说话结果验收。

## 2026-09-30：桌面端界面改版（白天月白、夜间石墨）

- 在独立工作树 `F:\MotionControl-App-ui-redesign`、分支 `claude/ui-redesign` 修改，从 `main`（`fbfe377`）拉出；只动 `web/` 和几条看页面文字的测试，服务端没改。
- 两套配色：月白（白天）、石墨（夜间），默认跟随系统，右上角「？」里可手动切换，记在浏览器里。
- 页签从四个减到三个：开始 / 本游戏 / 设置。「动作测试」并进开始页：触发时画面底部大字显示「原地踏步 → W」，画面下面一排动作认出来就亮（没绑键的写「没绑」），右边列最近触发，点一条跳去改键。
- 开始页：画面左上角只显示识别帧数；视角偏移改成右上角的小刻度条；删掉「身体区域：…」「动作：…」两行和键位小方块（画面里的框自己会亮）。右边准备卡片按状态变：差哪步点名哪步，全好写「可以开始了」，控制中换成计时。听到的语音口令改成几秒后消失的底部提示。
- 本游戏页：分「区域 / 身体动作 / 口令」三个页签；每行只露名字、键位（输出类型和键拼成一个控件）、按住/点一下；触发部位、动作扫过时也按、条件换键收进行尾「更多」，改过默认值的在名字旁挂小标签。动作库在「身体动作」下面、能收起，自己录的动作也放在里面。换游戏合成一个可搜索列表，点哪个换哪个；云端配置、改名、删除、管理员启动收进页头按钮和「⋯」菜单。本游戏口令没用的空位先不列，点「添加口令」再出来。
- 设置页改成左侧分类：设备、视角、身体识别、语音、键盘宏、实验与诊断。
- 合并或删除的重复功能：两个「新手教学」按钮留一个（放进「？」菜单）；视角「更多设置」里的握拳左右/上下下拉、「启用头部控制」「启用握拳视角控制」勾选框删掉（上面的「左右 / 上下」下拉就是同一件事）；「左右头控算法」只在选「左右转头」时出现，只列三种转头算法；手动拖的握拳/松开阈值删掉（「量身 → 只量握拳」量的就是这两个值）；映射表上方的「最近触发」删掉（开始页有）；搜索框 + 下拉 + 「使用这个游戏」合成一个列表；框让路的说明只留在那个框的「更多」里。旧版上下视角（绿框/抬低头）只有还开着的人才看得到，里面有一键「改用左手握拳」。头部姿态算法、采集方式、骨骼录制、触发录制、运行诊断归到「实验与诊断」。
- 提示按需出现：服务连着不显示状态；麦克风、鼠标、虚拟手柄正常就不说话，出问题才在那一组底下说；手机那一组只在选了手机或手机连着时出现；保存成功「已保存」两秒后自己消失；页顶提示改成几秒后消失的浮层。
- 新手教学跟着改了指向和说法；要指的按钮在别的设置分类、别的页签里时会先切过去。
- 验证：`node --check`（app.js、tutorial.js）通过；全量 `python -m pytest -q tests` 为 1357 通过、3 跳过（另有一条改版前就失败的 `test_only_the_rising_edge_is_recorded`，与界面无关，未处理）。另起一个服务实例（端口 18775/18776、复制的用户目录）逐页检查了两套配色和手机宽度无横向溢出，用伪造的内核状态看过有人、触发、控制中的画面。
- 未做：真人站在摄像头前的实际使用验收；悬浮窗（画中画）没改样式；尚未合并到 `main`。

## 2026-10-03：量身、固定圈与轻度动态头控稳定区衔接

- 在 F:\MC-main 的 codex/custom-pose-any-20261003 分支继续实现，保留前一提交 d34a9b1 的同一卡片多姿势规则。未切换当前服务，未合并、未推送。
- 新手基础教学复用现有量身课；已有个人量身和正在使用的握拳测量结果直接沿用。设置中的完整量身、只量握拳仍是同一流程的快捷入口，局部重测不移动区域。
- 固定区域量身完成后直接更新本次量到的区域，继续保持固定；用站定基准生成几何，避免用最后一帧跳起或伸脚的坐标定圈。站定时脚未入镜时，可使用该脚测量阶段的中性参考补上。未量到、自选点或连线组合的区域，以及旧上下视角框保留。
- 自选触发点/连线组合不套用默认手脚测量；教学根据内核真实触发组说明单点、任意组与组内同时入圈。自选区域仍需按实际选择试圈和调整，尚未实现任意人体点组合的专用自动量身算法。
- 恢复默认也更新当前固定区域，并保留固定模式、握拳测量和自选触发点区域；无人体参考时明确提示先连接并入镜，不悄悄报告成功。
- 量身完成结果保留到用户点完成，展示跳过项、保留的自选区域以及握拳读数未能应用的情况。
- 身体防晃改为统一按身体运动量临时略扩头控最终门槛，最多 20%，按秒平滑恢复。保持下蹲/自定义姿势不延长限制，明显转向及反向仍可输出，不再需要头回正解锁。身体来源失效/中断会清除临时增量；个人死区与校准文件不写入增量，原头控和踏步算法保留。
- 修复切换踏步算法后，已录误触分析未自动重算的问题；头控设置显示已结合个人校准的实际侧倾门槛。
- 注意：内核自身不保存游戏映射，重启验证需要像正式服务一样重新载入游戏绑定。固定框几何会规范到小数点后四位，比较参考计算时需要考虑该精度。
- 已完成隔离用户数据的后端与浏览器检查；尚未进行真人摄像头、鼠标实际位移或游戏内手感验收。
- 最终验证：227 项相关后端、模拟姿态和真实浏览器自动检查通过；网页脚本语法与差异空白检查通过。浏览器未截图，未启用真实游戏输出。

## 2026-10-04：统一语音口令规则与连接入口定位

- 在 F:\MC-main 的 codex/voice-direct-user-commands-20261004 分支实现；通用口令入口保留在设置 → 语音。通用与本游戏口令统一点按、持续按住、松开；系统功能仍执行一次，并直接显示文字，不展示禁用的下拉框。
- 新安装的可选唤醒词默认留空，填写后应用于通用口令；新增“内置系统口令也使用此唤醒词”，默认不勾选，内置沿用体感。勾选且留空时内置直接说；本游戏口令始终免唤醒。已有个人前缀保留，支持清空保存并跨启动复用。
- 旧的自定义急停迁移为单独保存用户填写的部分，使用通用前缀；旧的非默认系统前缀保留原行为。版本标记避免重复迁移，两个个人语音字段继续不随游戏分享。
- 查看口令使用后台的实际完整说法与当前有效输出，显示固定内置、已映射的本游戏和通用口令；打开时重新读取，未映射/无输出目标不展示。内置急停就近解释为停止输出并松开按键。
- 界面参考 Apple 输入框规范和 Google Material 输入框的就近说明、明确标签原则；空前缀隐藏占位，长前缀换行。参考：https://developer.apple.com/design/human-interface-guidelines/text-fields；https://github.com/material-components/material-web/blob/main/docs/components/text-field.md。
- 开始页去连接明确选择设备栏目：手机来源展开二维码、滚动定位并转移键盘焦点；电脑来源定位电脑连接按钮。直接定位，无额外弹层、动画或新的权限处理。
- 检查中发现：正式服务首次使用会迁移随附的 config/voice_mappings.json，需要一起清空其默认前缀；游戏口令的行为在电脑分发层解析，测试不能仅检查识别器提交的编号；连接定位无需增加画面循环。
- 验证：相关 166 项检查完成。综合运行 165 通过、1 项发现旧画面检查不允许新增 requestAnimationFrame；改为同步一次性定位后，重新跑该项和 4 项连接浏览器检查全部通过。空/短/长前缀、两种窗口宽度、保存、作用范围、实际输出行为及口令清单均已检查；脚本语法与差异空白检查通过。无截图、无真人录音、无真实游戏输出。
- 当前旧服务尚未重启；新后端规则需重启后进行真人说话验收。未合并、未推送。

## 2026-10-04：手机网络鼠标接收

- 从电脑端已提交主分支 42d35af 创建独立工作树 F:\MC-desktop-mouse，分支 codex/mobile-network-mouse-20261004；当前运行目录 F:\MC-main 不作修改。
- 手机鼠标复用现有 /ws/input 双向连接，发送 mouse_frame、role=mouse、设备编号、递增序号、采集时间、整数横纵位移与按键状态。按键状态为 0～3：1 是左键，2 是右键，3 是同时按住。鼠标只有一个桌面光标，无需玩家槽位。
- 位移使用既有 MouseOutput；左右键进入既有输出来源合流，遵守电脑全局开始/暂停开关。手持设备在线状态包含网络鼠标，不依赖人体识别。
- 断开连接立即释放该手机的鼠标按键；300 毫秒没有新数据也释放按键，但保持连接，可以继续恢复输入。新连接接管后旧连接不能抢回，包含旧连接已因断流保护清空来源的情况。
- 位移、按键、序号和时间戳先验证；重复序号及过时数据不执行。数字验证补充了超大整数处理，避免无效时间戳使采集线程异常退出。错误信息使用中文。
- 验证：22 项新增网络鼠标检查与既有手机输入、连接会话、输出动作检查共 85 项通过；差异空白检查通过。检查使用替代鼠标输出，未启动服务器、未产生电脑实际鼠标动作、未进行游戏验收。
- 电脑后台需重启才会识别新鼠标协议；旧版手机摄像头和手柄协议保持兼容。尚未合并、未推送；手机端安装与实际连接由主任务继续完成。

## 2026-10-04：统一两类语音的完整输出与唤醒词

- 用户明确要求：通用口令和本游戏口令仅区分生效范围，两处均保留完整输出功能；本游戏输出类型与键位改为独立控件，采用通用口令样式。保留现有布局和普通下拉，不减少功能，不改身体动作或头控参数。
- 两处共用输出类型、目标控件、行为控件和读取规则，通用口令补齐鼠标按键、滚轮、扳机、左摇杆、组合键、键盘宏、多选松开口令；点按、持续按住、松开统一。系统功能目标在两类语音中一致。
- 通用口令的保存与云端规范化复用游戏输出校验，保留组合目标列表与按键领先摇杆时间。实际执行转交原有输出层，避免只增加界面选项却无法保存或执行。
- 松开口令可同时选择通用与本游戏口令。通用口令引用由口令内容生成稳定编号，调整列表顺序、增删其他口令或修改唤醒词不会误指向别的输出；删除被引用的口令后不执行。循环宏的实际行为、口令清单及松开目标统一按宏库处理。
- 紧急停止属于系统口令，与所有内置系统口令统一使用用户填写的唤醒词；留空直接说，填写后才加前缀。移除系统口令的独立开关与固定“体感”要求，旧开关字段仅为兼容保留并自动迁移。原有用户唤醒词和口令保留。本游戏口令继续直接说。
- 内置紧急停止改为正常文字对比度，说明显示实际完整说法及停止所有输出、松开按键的效果；“内置”表示程序自带、不能修改或删除，不表示禁用。
- 验证：158 项相关检查全部通过，包含保存与重新加载、云端规范化、完整输出分发、旧配置迁移、口令冲突、循环宏停止、两种窗口宽度及空/短/长唤醒词；两份界面脚本语法、差异空白检查通过。未截图、未录音、未做真人或真实游戏验收。
- 按用户最新要求，先通过关闭接口停止 F:\MC-main 旧后台（40920），确认两个服务端口均关闭；最后复核发现同目录后台另被启动（44392），再次关闭。完成后合并本地 main，不启动新版，不推送云端。保留此前手机网络鼠标提交 7bb74e6。

## 2026-10-07：微软 Kinect 摄像头和踏步深度增强

- 基于电脑端 main 的 17e8dfc，在独立工作目录 F:\MC-kinect-camera、分支 codex/kinect-depth-camera-20261007 实现。F:\MotionControl-App 旧实验和 F:\MC-depth-compare 对照分支仅只读检查，原有改动保留。
- 沿用“设备 → 摄像头”的来源、摄像头下拉和连接按钮；扫描可列出微软 Kinect。只有支持深度的设备显示普通样式“使用深度”开关，首次默认开启，按设备序列号记住选择；普通摄像头使用原有状态文案，实际收到深度时补充“深度已启用”。
- 使用当前已恢复的微软原厂驱动和接口。采集程序在用户缓存目录自动准备，通过本地二进制管道传递640×360未镜像彩色画面、同帧可靠三维骨骼和地面方向；不保存照片、录像或原始深度文件。程序退出、读取停流和用户停止均释放设备。
- 摄像头选择仍兼容原来的数字序号；Kinect 使用稳定设备编号，处理原厂接口打开前序列号暂时为空的情况。首次连接确认真实编号后保存，不用临时默认编号覆盖另一个设备的深度选择。开启或关闭深度会重新建立当前采集，人体输出在切换期间释放。
- 彩色画面沿用现有完整人体识别模型和预览。深度骨骼必须与画面中的肩、髋位置匹配，推断关节、人物不匹配、重叠人物和过期帧不作为踏步依据；旋转时同时变换匹配坐标。
- 旧版与新版踏步均可以使用三维脚踝相对高度，按躯干长度归一，并用地面方向补偿相机倾斜；保留既有两脚交替、后续低门槛、其他动作排除和停止规则。无深度时输入保持原有二维规则。孤立缺失帧短暂保留已有高度，不产生新的运动；持续缺失则回到二维并重新确认交替节奏，避免把旧的第一步带到另一测量路径而凭空多走。初始深度基准只在脚高差较小时建立，避免把已经抬着的一只脚当作站立基准。
- 新采集源文件进入发行文件清单，普通电脑不需要安装 Kinect 组件；选择 Kinect 时需要原厂运行库和适配器。
- 实机检查：30.01秒读取651帧彩色和651帧有效深度，原厂报告652帧深度、652帧人体流；该轮没有跟踪到人体，不能据此宣称真人踏步通过。完整识别管线分别开启、关闭深度：开启181帧采集/180帧识别，关闭139/139，近期供帧与识别均约15帧/秒，无运行错误；停止释放分别约0.359秒、0.189秒。只保存统计，记录在 output/kinect-camera。
- 定向检查80项通过，覆盖两版踏步真实内核调用、二维回退、前后移动与倾斜补偿、单腿不启动、低幅度续步、缺失深度、人物匹配、按设备保存与重新加载、浏览器开关和原有相机/踏步/发行清单。脚本语法和差异空白检查通过；没有截图，没有真人动作录制，没有真实游戏验收。
- 本次不启动电脑端服务器或界面、不合并 main。只提交当前独立分支。下一步是用户真人踏步验证；先前真人语音及真实游戏输出体验的验收仍不能由这些测试替代。

## 2026-10-07：Kinect 二代画面与骨骼同步左右反向
- 按用户确认，只反转 Kinect 二代的视频与骨骼显示方向。主画面的图片、骨骼、身体区域共用显示方向，区域文字保持正向；悬浮窗、区域移动与改大小同时适配。
- 识别内核和原厂深度继续使用同一份坐标与身体左右名称，因此不会因显示翻转破坏深度匹配或修改头控输出方向。普通摄像头与手机沿用原来的显示方向，切回 Kinect 也能正确恢复。
- 两项浏览器检查通过，覆盖设备切换、视频/骨骼/区域样式、悬浮窗骨骼与框的位置、区域向右移动和改大小；脚本语法与差异空白检查通过。没有截图或录制，不以浏览器检查代替真人游戏操作验收。
- 改动位于 F:\MC-main 的 main，更新页面即可生效，保持现有 Kinect 连接，不重启后台。

## 2026-10-07：按反馈恢复触发圈原有方向
- 用户指出上一笔 f2e0cfd 把原来正确的圈也左右翻转了。将 web/app.css 和 web/js/play.js 精确恢复到 cae74a2 的区域显示、悬浮窗和编辑规则，不改用户保存的圈、量身结果或输出映射。
- Kinect 左右反向移到旋转后的采集帧，在人体识别前完成；识别与视频预览共同使用同一帧，普通摄像头不翻转。深度匹配也使用该帧的旋转/镜像方向，通过肩髋对应确认原厂与图像骨骼的左右名称，再将脚部可靠关节对应起来，继续拒绝多人物歧义。
- 67项针对性检查通过，覆盖采集实际像素翻转、全部旋转方向、深度脚部匹配、原有圈与悬浮窗显示位置、编辑方向、普通摄像头及生命周期。语法与差异检查通过，没有截图或录像；真人方向与游戏体验仍以实际使用为准。
- 需重启当前 F:\MC-main 后台以加载采集侧修正，再恢复原有 Kinect 识别；本次不推送云端。

## 2026-10-08：选择相机即识别，区分暂停控制与停止识别
- 按用户要求保持设备页布局，来源和摄像头选择立即接入并识别，扫描结果在刷新配置后仍保留。识别中、控制中均可切换：先松开旧输出，等待旧相机与识别线程释放，再建立新相机；成功后按切换前的控制状态继续，停止按钮优先于迟到的切换响应。
- 主按钮统一为“开始控制／暂停控制”。暂停只关闭输出，识别继续；首次需要接入设备时，同一次点击可接入后开始控制。右侧和设备页按钮统一为“停止识别”，F9同步停止身体和语音识别并立即释放输出。内置系统语音口令原有功能不改。
- 相机操作的页面等待延长到90秒，Kinect第一帧最多等待45秒；显示准备状态，停止操作可中断Kinect等待。延长的是启动时间，不增加稳定运行时的识别延迟。处理客户端取消响应时的正常断连，避免重复发送错误响应。
- 实机重连曾偶发设备编号异常；区分原厂接口暂未提供编号与实际换了另一台设备，暂时无编号的帧不采用，实际不同编号仍提示重新扫描。原有采集侧Kinect镜像、触发圈位置、量身结果、头控与握拳配置均保留。
- 针对性检查107项通过，涵盖完整相机生命周期、自动接入、控制中切换、暂停不断识别、F9中断优先、延迟首帧、暂缺设备编号、真正换设备、现有配置与方向规则。界面脚本语法与差异检查通过，无截图或录像。
- 实机：Kinect到普通相机0再恢复Kinect；每段2秒新增61～62识别帧，均约30帧/秒，无相机运行错误。停止后0新增采集帧，身体入口关闭、语音断开、输出关闭。最终恢复kinect2:009098263847，深度启用，电脑音频，输出保持暂停。统计见output/kinect-camera/20261008-automatic-selection-check.json。
- 握拳问题只读检查已看到左手“moving”及非零上下识别输出，但总输出开关关闭；当前上下来源是左手握拳，上下头控未启用。已向用户发出真人测试准备提示，真人鼠标移动及上下头控体验尚待操作确认，不能以相机出帧或浏览器检查代替。

- 补充真人握拳验证：用户明确回复“开始”，倒数后临时开启鼠标输出10秒。94次状态采样中70次握拳生效，上下识别量范围-0.7～0.7；真实光标纵坐标186～972，36次纵坐标改变，94次采样输出均开启，无输出错误。结束已恢复暂停，没有保存原始动作数据、截图或录像。
- 用户随后取消上下头控真人测试；没有修改头控来源，仍为横向侧倾头控、上下左手握拳。保留Kinect识别与深度、电脑音频；本次修改直接提交本地主分支，不额外推送云端。

## 2026-10-08：通用摄像头枚举、真实名称与黑画面诊断
- 用户要求检查整个扫描方法，不能只删一个重复入口。当前已保存选择为数字1；程序沿用保存值，扫描不会自动选第一项。没有历史选择事件记录，不能断言用户在某次操作中为何保存为1。
- Windows原生视频设备枚举与直接采集的顺序对应：0为内置摄像头，1为Kinect V2 Video Sensor普通视频接口，2为直播伴侣虚拟摄像头。参考OpenCV实现：https://github.com/opencv/opencv/blob/4.x/modules/videoio/src/cap_dshow.cpp 。旧扫描只判断一帧尺寸和数据长度，纯黑但结构正常的帧也通过；旧状态只判断采集线程运行，因此会提示站到镜头前并展示处理黑帧的帧率。
- 增加Windows原生名称枚举辅助程序，只读取设备目录，不开启摄像头；缓存编译产物放在用户缓存，发行清单携带源文件。枚举正常时只检查真实列出的设备，枚举失败或非Windows仍可退回原有数字探测。按照系统软件设备标识判断虚拟摄像头，不凭名称猜测。
- 每台未使用设备最多读取3次，观察空帧、异常尺寸、持续全黑及初始化后恢复；静止的有效图像照常通过。2秒为读帧之间检查的软时限，驱动单次读帧的阻塞时间仍由原接口决定。被占用、没帧和黑画面都保留已枚举的设备并标明状态，不因镜头遮挡删掉真实设备。当前已使用的相机不重复打开。
- 同一Kinect原厂通道已存在时不再列出普通视频重复入口。黑帧状态从实时采集传到设备页与开始页：显示画面全黑，不再给相机打正常勾，也不再用1帧/秒和站到镜头前掩盖无画面。低层诊断仍保留真实采集/推理统计。直接采集设备有真实名称；未确认的兼容采集数字顺序不强行套用名称。
- 针对性检查102项通过：95项相机扫描/来源保存/线程生命周期/接口切换/Kinect/发行文件，7项浏览器检查，覆盖静止图像、纯黑、起初黑而后恢复、虚拟源正常供图、设备占用、无帧、原生枚举为空与失败、正在使用的相机不重开、扫描保留选择、黑画面提示与供图恢复。脚本语法和差异空白检查通过。
- 实机复现普通视频接口：1920×1080每个像素均为0，采集及处理均1.00帧/秒，无相机运行异常；新版正确报告画面全黑。恢复原厂kinect2:009098263847后，实际预览像素均值124.685、标准差78.312，采集及识别均29.96帧/秒，深度启用，无运行错误。扫描正确显示内置摄像头、当前无画面的直播伴侣虚拟摄像头和一个微软Kinect入口。数值统计见output/kinect-camera/20261008-122646-camera-enumeration-check.json。
- 正常退出旧F:\MC-main后台并启动修正版，最终恢复原厂Kinect，输出保持暂停。没有保存相机照片、录像或原始动作，未修改握拳/头控/区域/量身设置；不进行用户已取消的上下头控真人测试。本次仅提交本地主分支，不处理远端其他更新。
