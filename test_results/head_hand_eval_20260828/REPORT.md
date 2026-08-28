# 真人视频头控方案 + 手部遮挡多点方案评估

评估日期：2026-08-28
范围：只读、离线、Shadow/No-op；没有调用键盘、鼠标、手柄或游戏输出，也没有改动生产源码、配置、阈值和用户档案。

## 结论先行

- 两段真实视频都能由 MediaPipe Pose Full 提供稳定的脸部关键点流。A 为 1562/1562 帧有效；B 为 1700/1706 帧有效（6 个空 Pose 帧）。PnP 和 ratio 是在同一段 33 点流上逐帧分别运行，不能把本结果解释为两个模型并行跑。
- 这两段片没有逐帧“向左/向右/抬头/低头”真值标签，不能宣布哪个算法绝对准确。PnP 在本数据上纵向输出更有幅度、ratio 的中性抖动和输出步进更小；建议保留 PnP 为当前默认，并增加校准质量提示/候选并排复核，不要仅凭这两段视频全局切换到 ratio。
- 单腕点确实存在明显连续缺失，尤其 A 的右腕：有效率 69.97%，最长连续缺失 218 帧（约 7.27 s）；同期肩肘腕组有效率 96.29%，组最长缺失 24 帧（约 0.80 s）。因此“置信度加权的肩-肘-腕语义组 + 迟滞 + 缺点降级”值得做，但它只能稳健地表达“手臂/抬手状态”，不能替代握拳/捏合分类。
- 仓库已有 `hand_landmarker.task`，抽样检查确实观察到 21 点手部结果，但只按约 1 秒抽样，不能当作连续手势准确率。要做握拳/捏合，下一步必须保留连续 21 点、手性和置信度，并用真人标注片段评估。

## 输入视频核对

| 文件 | 真实媒体信息 | 画面内容（按带时间戳接触表复核） | 校准/动作适用性 |
|---|---|---|---|
| `bf5ad4437d893c1bbb493ff90ab5d368.mp4` | HEVC，720×1280，30 fps，52.0667 s，1562 帧，1,650,583 B | 近景上半身；0–52 s 可见正面为主、轻微左右转头和上下点头/抬头变化；多次手靠脸、握拳/张手/指向。腿部不在画面内。 | 适合头部与手臂近景，不适合下蹲/跳跃；23.0–29.0 s 选为较早的低抖动正面校准候选。 |
| `c7fbc865832d892ec4aaa362fda5800d.mp4` | HEVC，720×1280，30 fps，56.8667 s，1706 帧，1,667,249 B | 全身站立；可见正面、轻微左右转头及抬头/低头，反复举拳/抬臂；接触表没有明确可标注的下蹲或跳跃段。 | 比 A 更适合观察全身遮挡，但仍没有动作时间标签；33.5–39.5 s 选为低抖动校准候选。 |

视觉证据：

- [A 时间戳接触表](video_review/bf5ad4437d893c1bbb493ff90ab5d368_timestamp_contact.jpg)
- [B 时间戳接触表](video_review/c7fbc865832d892ec4aaa362fda5800d_timestamp_contact.jpg)
- [A 校准代表帧（约 23.5 s）](video_review/video_a_calibration_23_5s.jpg)
- [B 校准代表帧（约 33.5 s）](video_review/video_b_calibration_33_5s.jpg)

## 方法与可重复性

1. 使用现有正式环境 `F:\MotionControl\MediaPipe\.venv\Scripts\python.exe`（OpenCV 5.0.0、MediaPipe 1.0.0、NumPy 2.4.6）和兼容 Full 模型 `F:\MotionControl-build-075\isolated-075\models\mediapipe\pose_landmarker_full.task`。
2. 每段视频只运行一次 Pose Landmarker（VIDEO 模式、原始视频时间戳、`num_poses=1`），保存真实 33 点 PoseFrame；PnP 与 ratio 都消费这份完全相同的缓存流。
3. 每段视频取一个共同的 6 s 校准窗：在最低抖动分数的 15% 范围内取最早窗口，避免把所有动作挤到片尾；两种算法不使用不同校准数据。
4. 直接调用现有 `HeadPoseEstimator`/`HeadController`，不启动服务器，不装载输出后端；`HeadController(None)` 只在内存中产生诊断值。`response_lag_proxy` 是“归一化信号越过阈值到控制器产生非零输出”的帧时间代理，不是人的反应延迟、传输延迟或游戏延迟。
5. 另用仓库已有 `F:\switch\mobile\public\models\hand_landmarker.task` 做约 1 Hz 的 21 点存在性抽样；该抽样不混入连续腕点/肩肘腕指标。

重放入口（同一环境、无输出）：

```powershell
& 'F:\MotionControl\MediaPipe\.venv\Scripts\python.exe' `
  'F:\MotionControl-App\tools\replay_pose_video.py' `
  'D:\xwechat_files\wxid_wczned99ujlf22_545c\msg\video\2026-08\bf5ad4437d893c1bbb493ff90ab5d368.mp4' `
  --model 'F:\MotionControl-build-075\isolated-075\models\mediapipe\pose_landmarker_full.task' `
  --vertical-source head --calibration-start 0 `
  --output 'F:\MotionControl-App\test_results\head_hand_eval_20260828\replay_a_pnp.json'
```

完整 A/B 评估脚本：[`evaluate_videos.py`](evaluate_videos.py)。

## 头控 A/B 结果

下表的 `有效率` 是该候选估计器得到有效头姿的帧比例；`非零 X/Y` 是校准完成后的控制器输出非零帧比例；“步进 P95”是相邻输出帧的最大轴变化 P95；“返回残差”是在信号从明显偏转回到 ±0.05 后的归一化残差 P95。本数据没有方向真值，因此没有 TP/FP/准确率。

| 视频 / 候选 | 有效率 | 校准窗 | 校准 yaw σ / pitch σ | 非零 X / Y | 步进 P95 | 延迟代理 P50/P95（X；Y） | 无效最长 |
|---|---:|---:|---:|---:|---:|---:|---:|
| A / PnP | 100.00% | 23.0–29.0 s | 2.763° / 0.926° | 3.5% / 14.8% | 0.862 | 200 / 1280 ms；66.7 / 140 ms | 0 帧 |
| A / ratio | 100.00% | 同上 | 0.0602 / 0.0098 | 3.4% / 8.8% | 0.390 | 500 / 1603 ms；233 / 760 ms | 0 帧 |
| B / PnP | 99.53%（1698/1706） | 33.5–39.5 s | 5.352° / 1.868° | 12.6% / 7.3% | 4.345 | 200 / 1723 ms；117 / 878 ms | 7 帧（约233 ms） |
| B / ratio | 99.59%（1699/1706） | 同上 | 0.0921 / 0.0169 | 9.4% / 2.1% | 1.523 | 167 / 1300 ms；950 / 1490 ms | 7 帧（约233 ms） |

补充诊断：四组的“返回到中心”代理残差 P95 均为 0（由当前死区/释放逻辑定义，不能据此证明真实用户回中无误）；符号一致性在观测到的非零样本中为 100%，但 A 的 X 方向样本仅 5–7 个、Y 方向 7–13 个，证据很弱；符号翻转后仍保持旧输出符号的过冲代理计数均为 0。完整逐帧数值在 [head_ab_summary.json](final/head_ab_summary.json) 和 [head_ab_per_frame.csv](final/head_ab_per_frame.csv)。

### 头控建议

- PnP 的纵向动态范围明显大于 ratio（A 最大 |Y|：16.397 对 5.308；B：28.000 对 4.559），更像当前“需要可见抬头/低头幅度”的默认候选；代价是校准散布、输出步进和疑似代理延迟更大。
- ratio 的中性散布/输出步进更小，且不依赖 OpenCV，但本片段的纵向信号常被压在死区附近，不能直接当成更稳或更准。
- 推荐：保留 PnP 默认；把“校准质量（σ、有效帧数、死区）+ ratio 并排 Shadow 对照”做成用户可见诊断。要决定替换默认算法，还需带明确左/右/上/下时间标记的真人片段，测方向分离、漏触和误触。

## 手部遮挡：单腕点 vs 肩-肘-腕组

组有效定义为三点中至少 2 点的 score ≥ 0.40；臂角有效要求三点全有效。它是“手臂语义状态”代理，不是握拳/捏合分类。

| 视频 / 侧别 | 单腕有效率 | 肩肘腕组有效率 | 腕缺失但肩肘仍有效 | 腕最长缺失 | 组最长缺失 | 臂角三点有效率 |
|---|---:|---:|---:|---:|---:|---:|
| A / 左 | 98.08% | 100.00% | 1.92% | 19 帧（0.63 s） | 0 帧 | 98.08% |
| A / 右 | 69.97% | 96.29% | 26.31% | 218 帧（7.27 s） | 24 帧（0.80 s） | 69.97% |
| B / 左 | 99.12% | 99.65% | 0.53% | 9 帧（0.30 s） | 6 帧（0.20 s） | 99.12% |
| B / 右 | 98.53% | 99.18% | 0.64% | 16 帧（0.53 s） | 14 帧（0.47 s） | 98.53% |

补充 21 点抽样（每段约 1 秒一帧，**不是连续控制结果**）：

- A：53 个样本，24 个样本检测到至少一只手（45.28%），7 个样本检测到两只手（13.21%），最长无手连续 8 个抽样点（约 8 s）。
- B：57 个样本，49 个样本检测到至少一只手（85.96%），4 个样本检测到两只手（7.02%），最长无手连续 7 个抽样点（约 7 s）。抽样 JSON 中每个检测包含真实 21 点坐标；手性标签会受镜像影响。

证据文件：

- [单腕/肩肘腕逐帧 CSV](final/hand_wrist_group_per_frame.csv)
- [21 点抽样汇总](final/hand_sample/hand_sample_summary.json)
- [21 点抽样 A JSON](final/hand_sample/bf5ad4437d893c1bbb493ff90ab5d368.hand_samples.json)
- [21 点抽样 B JSON](final/hand_sample/c7fbc865832d892ec4aaa362fda5800d.hand_samples.json)
- [腕点/组连续性图](plots/arm_wrist_group_coverage.png)

### 手部最小实现边界

1. 第一层只将肩-肘-腕做置信度加权/鲁棒聚合，用于“手臂抬起、横移、遮挡时保持语义”的门控；腕点高置信度时保留原始精细位置，腕点掉点时降级到组状态并暂停高风险触发。
2. 所有状态加进入/退出迟滞、最短保持时间、重复触发冷却；缺点超过上限时清零/暂停，而不是沿用旧坐标。
3. 握拳/捏合必须接入连续 Hand Landmarker 21 点（掌根、拇指、食指/小指等）和手性/置信度；使用指尖-掌根、指尖间距离及相对尺度，而不是用肩肘腕猜拳势。
4. 多点也有反例：肩耸、肘部独立摆动、交叉手臂或身体旋转会让“组中心/臂角”偏离真正手势；因此组只做稳健门控，最终拳/捏合仍需手部几何和短时一致性。

当前生产代码静态上仍以单腕点定义动作区和 `lookGate`（`control_kernel.py`），本评估没有改它；建议后续以独立适配器接入上述降级策略。

## 证据与保护边界

- PC 工作树：`F:\MotionControl-App`；外置 Git 元数据：`I:\MotionControl-App\_git`；评估开始时工作树已有并发修改，当前 HEAD 记录为 `ed426809cb99877b36f4e2fa065014ca43d18a8a`，未执行 reset/checkout/clean，也未覆盖任何生产文件。
- 模型 SHA256：`5134A3AAD27A58B93DA0088D431F366DA362B44E3CCFBE3462B3827A839011B1`（9,398,198 B）。
- 21 点抽样模型 `F:\switch\mobile\public\models\hand_landmarker.task` SHA256：`FBC2A30080C3C557093B5DDFC334698132EB341044CCEE322CCF8BCF3607CDE1`（7,819,105 B）。
- 视频 SHA256：A=`05B4841016D76172A11D825A9B72BA432DA7F5C31AD09C599D579A7C6C76C688`；B=`72116392B04CBE459014EAEDF493CE7075CEE68ED276C5CA7D2730B78BF93D7E`。
- 现有 `tools/replay_pose_video.py` 两次 no-op 重放均完成：A 1562 samples、invalid pose 0；B 1706 samples、invalid pose 6。自定义 A/B 结果在 `final/`。
- 未运行全量 pytest、未构建、未接入游戏、未宣称真人动作准确率；没有下蹲/跳跃真值，也没有可用于真实端到端延迟的传输/输出事件。
- 关键产物与输入/模型哈希见 [`SHA256SUMS.txt`](SHA256SUMS.txt)。

本目录就是独立产物根目录：[`test_results/head_hand_eval_20260828`](.)。
