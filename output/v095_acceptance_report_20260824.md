# MotionControl v0.9.5 acceptance snapshot

日期：2026-08-24

## 代码与版本

- PC：`b358113`，分支 `head-control-v2-signal-fix-20260821`
- Mobile：`7221629`，父仓库 `F:\switch` 的 `main`
- PC/API/Web：`0.9.5`
- Android：`versionName 0.9.5`、`versionCode 21`
- 协议：继续使用 `pose_frame_v2`；没有替换 MediaPipe、Sherpa KWS、模型或输出后端。

## 自动化结果

- PC：`77 passed, 19 skipped`（`F:\MotionControl\MediaPipe\.venv`）
- Python 编译：`control_kernel.py`、`server.py`、定向测试和 `head_signal_capture.py` 通过
- Web：`node --check web/app.js` 通过
- Mobile：`npm run check`、`npm run build`、`npm run android:sync` 通过
- Android：`gradlew.bat assembleDebug` 成功，APK：
  `F:\switch\mobile\android\app\build\outputs\apk\debug\app-debug.apk`
  SHA256：`B85A7A9EDC6088364D93E796899DC3D48CC1FE47367FE4D297E2759D121FFD3A`

## API/网页快照

源码服务短时启动后核对：

- `/api/models`：`version=0.9.5`，Full 33 点模型可用
- `/api/voice/status`：`available=true`、`model_ready=true`、`recognizer_mode=single_stage_phrase_kws`、`supported_count=27`、`last_error=null`
- `/api/voice/commands`：`count=27`
- `/api/kernel/status`：`version=0.9.5`、无 active body source、输出关闭
- 浏览器页面标题为 `MotionControl 游戏控制 v0.9.5`
- 普通页面可见：开始体感、五项状态、站好并校准、视角回正、六条常用语音；展开“全部 27 条命令”可读到 27 条用户短语。
- 控制台唯一错误为开发浏览器自动请求 `/favicon.ico` 的 404，不影响页面或 API。

## 控制规则核对

- 头部 yaw 仍进入水平 X；HeadController 的 pitch 不进入最终输出 Y，公开 `head.output_y` 也不再暴露 pitch。
- 纵向只在固定 `lookGate` 有效时使用右腕；进入门控时记录右腕 Y 锚点，按 range/deadzone 计算，门控第一帧离开、右腕缺失或来源清理时归零。
- 头控校准保持中心采集，不再要求抬头/低头阶段；默认输出仍关闭。

## 尚未替代真人验收的项目

- 真人左/右 yaw、lookGate 与右腕上下动作方向/舒适度
- 真实手机 0.9.5 安装、Sherpa KWS、scene_snapshot、自动重连联调
- 游戏内鼠标/Xbox 输出和长时间稳定性
