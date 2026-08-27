# 中文语音极端条件离线对比 v1 报告

生成时间：2026-08-27T17:36:24.434921+00:00；语料样本：12 个 Edge TTS fallback WAV，评测 case：159；候选：3 个成功运行。

## 结论

相对第一名为 `vosk_constrained/raw`。本轮没有达到保守的产品筛选门槛，结论为“当前无产品可接受胜者”。

- 综合第一名：`vosk_constrained/raw`（安全优先排序：误触率 → 紧急停止召回 → P95 处理耗时 → 正例意图准确率）。
- 低资源第一名：`sherpa_phrase_kws_current`（本轮实测模型目录最小；它的准确率、误触和紧停数据仍以表格为准，不因体积小自动胜出）。
- 强噪声相对第一名：`vosk_constrained/raw`（SNR 5/0 dB 子集）；但紧停召回仅 83.3%，未通过 95% 保守线，因此没有强噪声产品胜者。

筛选门槛是本次实验的保守比较线，不是已完成的产品验收：误触率 ≤5%、紧急停止召回 ≥95%、P95 处理耗时 ≤500 ms。真人小声、手机麦克风、真实房间噪声和持续在线端点仍未验收。

## 综合排名

| 方案 | 预处理 | 样本 | 意图准确率 | 紧停召回 | 误触率 | 拒识率 | 平均CPU ms/条 | P95 ms | 评分 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| vosk_constrained | raw | 159 | 90.0% | 93.3% | 0.0% | 100.0% | 37.0 | 47.6 | 95.5 |
| vosk_open | raw | 159 | 32.2% | 13.3% | 0.0% | 100.0% | 856.8 | 1429.7 | 50.4 |
| sherpa_phrase_kws_current | raw | 159 | 4.4% | 6.7% | 0.0% | 100.0% | 91.9 | 36.3 | 52.5 |

评分仅用于排序，计算为 `0.40×(1-误触率) + 0.30×紧停召回 + 0.20×正例意图准确率 + 0.10×(1-min(P95/1000,1))`。误触和紧停权重高于普通整句字面准确率。

## 极端噪声子集（SNR 5/0 dB）

| 方案 | 预处理 | 样本 | 意图准确率 | 紧停召回 | 误触率 | 拒识率 | P95 ms | 评分 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| vosk_constrained | raw | 60 | 86.1% | 83.3% | 0.0% | 100.0% | 48.0 | 91.7 |
| sherpa_phrase_kws_current | raw | 60 | 2.8% | 8.3% | 0.0% | 100.0% | 35.9 | 52.7 |
| vosk_open | raw | 60 | 19.4% | 0.0% | 0.0% | 100.0% | 1552.4 | 43.9 |

## 资源指标

模型目录大小是当前本地权重目录的字节数；RSS 增量包含解释器/依赖的共享开销，只作为同进程近似，不是手机峰值内存。加载时间不计入逐条 P95，但单独报告。

| 方案 | 模型目录字节 | 加载 ms | RSS 增量（近似） |
|---|---:|---:|---:|
| sherpa_phrase_kws_current | 40,744,741 | 730.3 | 47493120 |
| vosk_constrained | 68,292,271 | 599.9 | 121966592 |
| vosk_open | 68,292,271 | 301.4 | 112455680 |

## 方案筛选与落地性

| 候选 | 本轮 | Windows | Android | 中文 | 许可证/模型注意 | 集成取舍 |
|---|---:|---|---|---|---|---|
| 当前 Sherpa 全短语 KWS | 是 | Python wheel + 本地 ONNX | 官方项目支持 | 当前中文 Zipformer/KWS 模型 | 引擎 Apache-2.0（[仓库](https://github.com/k2-fsa/sherpa-onnx)）；本地模型目录未见独立 LICENSE，需单独确认权利 | 与当前正式代码最接近，低延迟、拒识边界清楚；同义词需扩关键词并重建词表 |
| Vosk 受限词表 | 是 | 官方 Python/Windows wheel | 官方 API 支持 | `vosk-model-small-cn-0.22` | 引擎 Apache-2.0（[仓库](https://github.com/alphacep/vosk-api)）；模型条款以本地模型 README/来源为准 | 依赖少、词表可控；长中文短语和噪声鲁棒性需实测 |
| Vosk 开放识别 | 是 | 同上 | 同上 | 同上 | 同上 | 覆盖开放文本，但严格 parser 仍决定是否触发，误识别风险更高 |
| sherpa-onnx 通用 ASR/Zipformer | 否（无第二套现成中文 ASR 模型） | 官方项目支持 | 官方项目支持 | 官方列出中文模型 | 引擎 Apache-2.0；具体权重需核对 | 可能提升同义词/开放句覆盖，但需新模型、VAD/端点和 parser，实时成本未测 |
| whisper.cpp tiny/base | 否（本机无可用二进制+中文模型） | 官方 README 列出 Windows | 官方 README 列出 Android | 多语种可用，中文质量/延迟需本机测 | 项目仓库含 MIT 许可信息；模型许可另核对（[仓库](https://github.com/ggerganov/whisper.cpp)） | 通用 ASR 方案，但非专用 KWS；端点延迟和模型体积不适合在本轮凭文档夺冠 |

本轮真实跑过同一套音频的只有 Sherpa 当前 KWS、Vosk 受限词表、Vosk 开放识别；通用 sherpa-onnx ASR 和 whisper.cpp 没有可核验的本机模型/二进制，因此只列为资料候选，不参加“最好”结论。

## 语料与退化

- 正例覆盖：唤醒词+开始校准、紧急停止、上下左右、开始/停止输出、加速/攻击/闪避/跳跃/换弹/技能/地图/截图/设置中心；另含 `向上/向左/向右/自动校准` 同义短语。
- 负例覆盖：唤醒词单独出现、紧停/校准近似句、同音/近音短语、缺少唤醒词、疑问句和普通非命令句；另有白噪声、粉红噪声、风扇型噪声纯负例。
- 清洁响度：-6、-18、-30 dBFS；噪声：20、10、5、0 dB SNR。噪声 case 只对标记为 stress 的小子集运行，避免无意义的组合膨胀。
- 轻预处理：静音裁剪 + 80 Hz 一阶高通 + -18 dBFS 归一化；没有重型降噪。

## 主要失效点

- `sherpa_phrase_kws_current` 在 `clean_-6dBFS` 出现 6 个意图错误或负例误触发；逐条证据见 `RESULT.csv`。
- `sherpa_phrase_kws_current` 在 `clean_-18dBFS` 出现 6 个意图错误或负例误触发；逐条证据见 `RESULT.csv`。
- `sherpa_phrase_kws_current` 在 `noise_white_snr20dB` 出现 6 个意图错误或负例误触发；逐条证据见 `RESULT.csv`。
- `sherpa_phrase_kws_current` 在 `noise_white_snr10dB` 出现 6 个意图错误或负例误触发；逐条证据见 `RESULT.csv`。
- `sherpa_phrase_kws_current` 在 `noise_white_snr5dB` 出现 6 个意图错误或负例误触发；逐条证据见 `RESULT.csv`。
- `sherpa_phrase_kws_current` 在 `noise_white_snr0dB` 出现 6 个意图错误或负例误触发；逐条证据见 `RESULT.csv`。
- `sherpa_phrase_kws_current` 在 `noise_pink_snr10dB` 出现 6 个意图错误或负例误触发；逐条证据见 `RESULT.csv`。
- `sherpa_phrase_kws_current` 在 `noise_pink_snr0dB` 出现 6 个意图错误或负例误触发；逐条证据见 `RESULT.csv`。
- `sherpa_phrase_kws_current` 在 `noise_fan_snr20dB` 出现 6 个意图错误或负例误触发；逐条证据见 `RESULT.csv`。
- `sherpa_phrase_kws_current` 在 `noise_fan_snr5dB` 出现 6 个意图错误或负例误触发；逐条证据见 `RESULT.csv`。
- `sherpa_phrase_kws_current` 在 `noise_fan_snr0dB` 出现 6 个意图错误或负例误触发；逐条证据见 `RESULT.csv`。
- `vosk_open` 在 `noise_white_snr10dB` 出现 6 个意图错误或负例误触发；逐条证据见 `RESULT.csv`。

当前 Sherpa KWS 的固有边界是全短语关键词：正式 `voice_action_map.json` 没有把所有 `voice_mappings.json` 同义词都做成关键词，所以 `体感向上/向左/向右/体感自动校准` 是有意加入的兼容压力项；这不等同于模型听不懂，而是词表/映射覆盖不足。

## 最小下一步

1. 不把本报告的相对第一名直接接入正式软件；先录制同一短语的真人正常音量、小声和手机麦克风样本，保留输出禁用。
2. 若真人数据确认误触可控，再只扩展当前 KWS 的必要同义词并重复这套回放；同时确认模型权重的独立许可文件。
3. 若真实房间噪声下仍需要开放句覆盖，再单独引入一个轻量中文流式 ASR/VAD 试验，不与 KWS 叠加到正式链路，除非延迟和误触证据足够。

## 证据边界

这是 Windows SAPI TTS + 确定性合成噪声的离线压力测试。它不是真人声学测试，不是手机本地麦克风实测，不是 Android 性能实测，也没有连接游戏输出。

详细复现入口、当前快照、包版本、模型哈希和逐条预测在同目录的 `manifest.json`、`RESULT.json`、`RESULT.csv`。
