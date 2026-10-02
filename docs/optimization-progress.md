# 2.3.0 优化进度与维护入口

按 2026-10-02《MotionControl 2.3.0 源码审查与优化方案》的编号追踪。保持现有界面视觉、Full 姿态模型和双手识别；PC 与 Android 源码版本仍为 **2.3.0**。本轮继续完善源码，尚未正式发布新的完整便携包或正式签名 APK。

“已落地”指代码与相应自动化回归已完成。本轮四项追加优化、真实模型浏览器对照、完整本地回归和入口/维护文档已完成；提交与 CI 状态以配套 PR 为准。实机、跨版本兼容与长时间稳定性专项按用户安排后置，不能据源码回归宣称原方案全部验收完成。

## 对照原方案

| 原方案 | 当前状态 | 已有实现与剩余边界 |
|---|---|---|
| 0：性能与设备基线 | 部分完成 | 已有合成控制帧对照与分阶段计时；同设备冷/热机、真实采集/识别/显示 FPS、资源长期趋势后置。 |
| 1.1 配置完整提交 | 已落地 | [配置事务](../motioncontrol/config_transaction.py)、[视角两轴](../motioncontrol/view_control.py)与服务端协调入口统一保存及恢复；身体/语音绑定和手机快照同步，前端操作版本保护。进程强制终止、多进程与真实磁盘故障专项后置。 |
| 1.2 来源与主设备 | 已落地 | [接入桥](../motioncontrol/input_bridge.py)分别约束显式启停、身体来源、会话所有权；首台手机拥有控制权，断线/过期后交接，同设备重连拒绝旧连接。真实双手机和网络切换后置。 |
| 1.3 会话生命周期 | 已落地 | PC 摄像头、手机相机/模型/socket 与原生语音取消使用代次；旧任务只清理自己的资源。摄像头拔插、睡眠恢复及厂商后台策略后置。 |
| 1.4 急停与命令保序 | 已落地 | [有界系统执行器](../motioncontrol/command_executor.py)可失效旧任务；急停先释放输出，最终提交检查命令及语音会话。普通键盘/手柄语音仍即时执行；游戏口令映射到系统功能也进入执行器。真实输出释放延迟后置。 |
| 2.1 实时路径与快照 | 已落地 | 帧处理可跳过全量状态，[录制](../motioncontrol/pose_recorder.py)交出独立快照后台原子保存。本轮状态读取跳过不需要的动作示范帧、快速复制配置，预计算语音配置冲突，精简 API JSON 编码；返回字段与可变隔离保留，合成基准见下文。 |
| 2.2 手机推理与交互 | 已落地 | 支持 Worker、OffscreenCanvas、createImageBitmap 的设备默认启用 Worker，保持 Full 与同帧双手、单槽最新帧及主线程回退；手模型后台加载，身体帧先返回。生产 Worker 与真实 Full/Hand/WASM 的 Chromium CPU 对照通过；实机 WebView/GPU/热机后置。原生视觉属于后续候选。 |
| 2.3 时效与背压 | 已落地 | 姿态/传感器拒绝重复、倒退与可信时间下的过期样本；手机有背压，PC 音频独立有界队列，过期或缺段显式重置。未同步时钟保留明确兼容路径；真实弱网后置。 |
| 2.4 可见性与变化更新 | 已落地 | 隐藏页停止画布/列表重绘，轮询退避、初始化重试、乱序响应保护；本轮只更新变化字段，保留事件、语音卡片和菜单节点/焦点，相同骨架不重复清空画布。真实 Chromium 定向回归已完成。 |
| 3.1 时间消抖与平滑 | 已落地 | 控制内核按可信样本时间处理，保持 30 FPS 参考手感；无采样时间的旧输入保留兼容行为。真人 15/30/60 FPS 误触、漏检与手感对照后置。 |
| 3.2 实际能力与适配 | 基础已落地；专项后置 | 原生报告传感器注册、有效位、年龄及接口能力，样本失鲜归零；ARM64、蓝牙权限、16 KiB 原生页和旧 WebView 矩阵待专项验收。设备自动分档是候选实验。 |
| 3.3 显示、触摸与弹层 | 自动化已落地 | DPR 有上限的画布、窄屏、触摸命中、软键盘与弹层焦点已有浏览器回归；真实高 DPI/触屏组合后置。 |
| 4.1 原子配置保存 | 已落地 | [语音配置](../motioncontrol/voice_backend.py)验证及存盘完成后才发布内存；多文件恢复记录、第二文件失败回滚有行为回归。保存中杀进程与长期故障专项后置。 |
| 4.2 发现与重连 | 已落地 | 手机地址缓存保留失败元数据；握手/回应有期限、心跳和连接代次，旧 socket 不覆盖新会话。真实 Wi-Fi/USB/换热点矩阵后置。 |
| 4.3 更新兼容与恢复 | 基础已落地；专项后置 | 签名/hash、原生 API/协议门禁、必要初始化后确认健康；桌面流式下载、复用完整文件、有限重试/总预算及取消。不同 PC/APK/网页/模型组合的完整兼容矩阵尚未完成。 |
| 4.4 云端等待与离线 | 已落地 | 云列表批量查询；[公开元数据缓存](../motioncontrol/cloud_metadata_cache.py)已接入云面板和官方动作库，60 秒 TTL、离线持久化、后台合并刷新、有界条目/工作线程。安装仍直接下载验签，不通过展示缓存授权。 |
| 4.5 发布来源与维护 | 已落地 | 手机 dist 和 PC 装配记录确切源码、dirty 状态及摘要，拒绝过期产物。当前入口、双仓配套、历史目录和回退说明已补齐；未执行正式发包。 |

## 两仓库怎样配套

| 仓库 | 当前入口 | 职责 |
|---|---|---|
| [MotionControl](https://github.com/guiwushengzhe-prog/MotionControl) | `START.bat` / `server.py`、`motioncontrol/`、`web/` | PC 采集、唯一控制内核、输入接入、映射、输出与设置；`cloud/` 为配套云服务。 |
| [MotionControl-Android](https://github.com/guiwushengzhe-prog/MotionControl-Android) | `mobile/`、`mobile/src/main.ts`、`mobile/android/` | 手机网页与 Capacitor 原生壳，本地姿态/手部推理、语音、传感器及蓝牙。进度文档为 `mobile/docs/optimization-progress.md`。 |

体感经手机本地识别发送 `pose_features_v1`，电脑不再次推理手机视频；语音主要由手机原生 Vosk `SpeechService` 识别后发送 `voice_text`，PC 使用同一词表/映射。旧输入格式继续走兼容边界。Android 仓库的 `motionbridge/`、`desktop/` 及其旧启动/打包脚本是历史实现，不能拿它们验证当前 PC 服务。

源码开发：在 PC 仓库安装 `requirements-runtime.txt`，准备配置指定的模型，再运行 `python server.py` 或 Windows `START.bat`。管理界面是 `http://127.0.0.1:8766`；手机接入为 `ws://<电脑地址>:8765/ws/input`。逻辑测试使用独立的 `MOTIONCONTROL_USER_DIR`，避免改开发者配置。Android 从 `mobile/` 执行 `npm ci`、`npm run check`、`npm test`、`npm run android:sync`；原生构建要求 JDK 21、Android SDK 36。详见两仓库 README 和[已有协调说明](coordination-optimization.md)。

## 更新与回退

已安装用户沿用现有正式包的启动入口：PC 双击便携包的 `启动.bat`，手机打开 APK。本轮的源码与 Debug APK 是开发/验证材料，不能直接当成新正式发布包。

PC 更新下载至 `app_next`，完整校验后留完成标记；下次通过包外的 `runtime/launcher.py` 启动，才将它替换为 `app/`。启动前保留 `app_previous` 和 `app.booting`，两面服务启动成功后确认健康。若未确认，下一次启动恢复上一份；确认成功会删除上一份备份。因此自动回退保护的是失败启动，不能承诺任意历史版本都可一键降级。开发仓库直接运行 `server.py` 不等于走完整便携更新链。

Android 网页包经签名、hash 与能力校验后下载至 `web_next`，下次启动才启用；未完成健康确认的网页在再启动时被移除，回到 **APK 内置网页**。这不是恢复上一份热更网页，也不会回退 APK 原生代码。涉及原生插件的更新必须安装配套 APK；旧 APK 根据能力使用已有兼容路径，完整版本组合专项后置。

用户配置保存在 `%LOCALAPPDATA%\MotionControl`，与程序目录分开。人工回到已知可用正式包时，先停止服务并备份用户目录，保留当前目录，再重新解压该正式包；涉及配置格式变化时由维护者确认兼容，不能靠删除 `.issued`、完成标记或健康标记强行装旧更新。Android APK 更换受签名和版本码约束，不把清除应用数据作为常规回退步骤。

## 发布装配与记录

每次正式候选都从明确的两个提交重建，不能仅用“都是 2.3.0”判断配套。本轮配套变更见 [PC PR #10](https://github.com/guiwushengzhe-prog/MotionControl/pull/10) 与 [Android PR #1](https://github.com/guiwushengzhe-prog/MotionControl-Android/pull/1)。完整提交和 CI 以两个 PR 的最新提交/检查及总交付记录为准，文档不将自身提交号写为固定发布来源。

先在 Android `mobile/` 运行生产构建，再在 PC 仓库显式装配到已有完整便携目录：

```text
python tools/stage_release.py --target <便携包根目录> --phone-web <Android仓库>/mobile/dist
python tools/stage_release.py --target <便携包根目录> --phone-web <Android仓库>/mobile/dist --check
```

`stage_release` 要求目标已有 `python/`，不负责从零供应完整 Python、模型和驱动。`dist/build-provenance.json` 与装配来源记录应保留；`dirty` 必须如实披露。开发装配、测试目录 `--check` 和 ZIP 对齐都不能单独证明完整便携包或原生设备兼容已验收。

发布说明必须记录：两仓库完整 commit、APK 版本名/版本码、网页版本、原生 API/协议要求、模型来源/hash、产物 SHA256、签名状态、实际测试命令与结果，以及后置项。正式签名使用既有私钥流程；本轮不改变版本、签发新正式包或上传发布。

## 验证边界

既有回归覆盖真实函数/入口的写盘失败、旧会话、急停、音频积压、浏览器布局及来源装配；命令和说明见[协调优化记录](coordination-optimization.md)。本轮四项新增优化各自记录基准输入和测试结果，不与上一轮测试数相加。

| 本轮实现 | 定向验证与已测结果 | 测量范围 |
|---|---|---|
| PC 状态快照 | 相关组合 127 项通过，最后定向回归 41 项通过。6 个官方动作、9 条绑定、固定时钟各 1,000 次：`status` 中位数 0.553 → 仅元数据优化 0.400 → 完整优化 0.317 ms；P95 0.711 → 0.625 → 0.516 ms。`status`、`effective_bindings`、`general_settings_payload`、`runtime_zones` 各自的对照 SHA256 一致。 | 仅合成 Python 状态读取成本，无摄像头、推理、OS 输出或游戏 FPS。 |
| 语音状态与 API 编码 | 相关组合 106 项通过、2 项条件跳过。22 条命令配置的 `VoiceService.status` 中位数 0.16679 → 0.001652 ms；API 样本 16,302 → 14,946 字节，实际 HTTP 连续两次请求验证中文与 `Content-Length`。 | 语音配置冲突预计算的合成 CPU 成本，以及选定响应样本；不是音频识别速度或所有 API 的统一收益。 |
| 前端变化更新 | 49 项组合回归通过，其中新增 7 项行为回归。20 次相同输入、仅时间递增：开始页 MutationRecords 1,112 → 13、childList 493 → 11、画布清空 20 → 0；游戏页 561 → 1 / 240 → 0；语音页 263 → 0 / 152 → 0。事件年龄变化仍更新，按钮与菜单保持焦点。 | 真实 Chromium、模拟 API、静态动作；两列为 MutationRecords / childList。不推论实机 CPU、FPS 或完整链路延迟。 |
| 云元数据缓存 | 72 项相关回归通过，包含新增 12 项后端及 8 项浏览器用例；覆盖冷请求合并、过期后台刷新、离线重启、查询隔离、有限退出与无效签名不被缓存授权。 | 受控真实 HTTP 与浏览器入口；未测生产云弱网时延。 |
| Android Worker | TypeScript 检查、64 项 Vitest、3 项 controller 测试、生产构建及来源记录通过；真实 Chromium、生产 Worker、Full/Hand/WASM 共 30 帧 CPU 对照通过。每帧 33 身体点和左右各 21 手点，图像/世界/手部关键点逐项最大差为 0；1,093,120 个采样 RGBA 分量差为 0，无页面错误。 | 双方使用同图、相同时间戳、640 输入档与 CPU 预热；生产自适应档为 384/448/512，不能把这一测量当成各档实测。主线程阻塞改善见下文；尚无 Android 实机 WebView/GPU、温升或真人识别质量结论。 |

Worker 同图 CPU 对照中，主线程同步工作总 P95（含 bitmap 发起）179.7 → 14.1 ms；10 ms 心跳间隔 P95 184.3 → 11.0 ms、最大值 335.2 → 24.0 ms。模型总计算均值 164.27 → 163.55 ms，帧完成总时间均值 169.45 → 173.34 ms，包含线程传输开销。因此测得的是主线程交互阻塞减少，不能宣称模型更快或 FPS 提升。Android `mobile/scripts/benchmark-vision-worker.mjs` 可复测；固定图、headless Chromium 与 CPU 的结果不替代真人动作和手机实测。

另用生产尺寸 **512 × 342** 完成三帧正确性烟测：每帧均有 33 身体点和左右各 21 手点，与主线程归一化/世界/手部结果及原始图像缩放像素的最大差均为 0，无页面错误。该烟测与 640 输入档的 30 帧性能对照分别记录，不代表其他尺寸、GPU 或设备均已覆盖。

完整本地 `tests` 与 `cloud/tests` 回归：**1,546 项通过、38 项条件跳过、7 条警告，126.65 秒**，包含 30 项真实 Chromium 行为用例（既有 15 项、增量渲染新增 7 项、云缓存新增 8 项）。复测命令：

```bash
MOTIONCONTROL_USER_DIR=/tmp/motioncontrol-tests python -m pytest tests cloud/tests -q
```

这些定向测试组与全量回归有重叠，不能相加；跳过项不计为通过，Windows 设备专项未因此完成。CI 状态见上述配套 PR。PC 快照可复测：`python tools/benchmark_kernel_status.py --iterations 1000 --output status-current.json`，基准源码也可由 `--kernel-file` 显式指定。行为入口见 [快照回归](../tests/test_kernel_status_snapshots.py)、[语音状态回归](../tests/test_voice_status_snapshot.py)、[增量渲染回归](../tests/test_web_incremental_render.py)和[缓存端点回归](../tests/test_cloud_cached_endpoints.py)。

云缓存按电脑配置的 endpoint、数据种类和查询参数隔离，最多 32 条、4 MiB 持久化及 3 个后台工作线程；有缓存先返回，过期合并后台刷新，首次无缓存最多等待 8 秒，失败重试间隔 15 秒。只保存公开展示字段和成功更新时间，不保存凭据、配置正文或签名；安装状态从本地实时读取。可见面板在刷新进行时跟进，离线保留最后成功结果与实际更新时间，安装/移除继续走原校验流程。

后置范围包括 Windows 驱动及真实游戏输出、Android 权限/厂商后台与 16 KiB 原生页、双手机及真实弱网、真人动作质量、20–30 分钟热机、2/8 小时稳定性、进程被终止和资源长期增长。当前没有这些专项的完成结论。可选原生视觉、设备自动分档、进一步推送替代轮询属于候选，需收益与行为对照后另行决定。
