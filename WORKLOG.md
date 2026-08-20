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
