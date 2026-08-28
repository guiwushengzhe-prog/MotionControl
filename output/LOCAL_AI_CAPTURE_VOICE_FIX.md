# MotionControl 本地合并任务：截图门槛 + 语音两段式唤醒修复

目标项目：`F:\MotionControl-App` 当前 v0.9.3 + seven-zones 合并版本。

本目录中的 `MotionControl-v0.9.3-capture-voice-hotfix.patch` 是基于 ChatGPT 的 v0.8.3 scene/voice 文件生成的差异参考。不要粗暴覆盖本地文件；按差异人工合并到当前 v0.9.3。

## 只改两处核心外围文件

1. `scene_layout.py`
   - 去掉首次“截图/记录场景”对 `left_ear/right_ear/left_ankle/right_ankle` 单帧置信度同时过阈值的硬失败。
   - 保留头、肩、髋、双腕和人物尺寸的基础有效性检查。
   - 首次生成 7 Zone 时：耳朵优先直接 ear，低置信度时用同侧 eye + 肩宽估算；脚优先 ankle/heel/foot_index 中最可靠者，仍不可用时用髋+躯干尺度估算。
   - 如果使用了估算，截图仍成功，但在 `last_result.placement_fallback` 和 message 中提示用户回来检查圈位置。
   - 不改变 Zone 数量、映射和 fixed-zone 运行逻辑。

2. `voice_backend.py`
   - 增加 3.5 秒 `WAKE_COMMAND_WINDOW_SECONDS`。
   - 当 Vosk 单独识别出“体感”时，不判失败，而是打开 3.5 秒命令窗口。
   - 窗口内下一条最终识别如“截图”“重新匹配”“开始输出”等，按已有 mapping 执行。
   - 原来的“体感截图”一次性识别仍然支持。
   - 没有唤醒且不在窗口内的命令仍拒绝，避免普通讲话误触。
   - status 增加 `wake_window_active` 和 `wake_window_remaining_ms` 便于调试。

## 不要修改

- `head_control.py`
- seven-zones 的 7 个圈定义/按键映射
- 场景重新匹配算法
- 当前 Vosk 模型路径和本机词库
- 当前模型根路径修复

## 建议补两类回归测试

- 双耳/脚点坐标仍在画面内但 score 很低时，`capture_reference()` 仍成功并产生 7 Zone。
- `_match_and_execute('体感', enforce_wake=True)` 后 3.5 秒内再输入 `截图`，应执行 `SCENE.CAPTURE_REFERENCE`；直接输入 `截图` 仍应因缺少唤醒而拒绝。

ChatGPT 基线针对性+全套测试结果：`42 passed, 0 failed`（这是源码基线测试数量，不等于本地 v0.9.3 的总数）。

本地合并后请跑当前完整测试。之前本机基准是 `66 passed, 19 skipped, 0 failed`；合并本补丁后不能出现新增失败。新增测试数量可使 passed 增加。

如果出现与 v0.9.3 结构冲突，不要改 `head_control.py` 或重写 scene/voice 架构；把 traceback 和差异追加到当前 Google Drive 热修目录的 `LOCAL_AI_FEEDBACK.md`。
