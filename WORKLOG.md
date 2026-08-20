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
