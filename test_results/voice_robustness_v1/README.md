# 中文语音极端条件离线对比 v1

本目录只存放离线压力试验的脚本、少量合成语音素材、结果和报告。它不连接 `server.py`，不导入 `VoiceService`，不调用 `OutputManager`，不会触发键盘、手柄或游戏输出。

## 试验对象

实际比较同一批音频上的三种可落地路径：

1. `sherpa_phrase_kws_current`：正式代码当前使用的 Sherpa-ONNX 单阶段全短语 KWS；读取正式仓库已有模型和关键词文件，不写入它们。
2. `vosk_constrained`：现有中文 Vosk small 模型 + 固定短语语法（受限词表）。
3. `vosk_open`：同一 Vosk 模型的开放识别，再用当前映射做严格命令解析。

另测 `light_preprocess`：静音裁剪、80 Hz 一阶高通和响度归一化；不含重型降噪。`raw` 与 `light_preprocess` 对每个候选使用完全相同的输入音频。

Whisper/whisper.cpp、通用 sherpa-onnx ASR、FunASR/Paraformer 等列为资料候选或未实测候选；没有把新模型下载进本试验，也不把它们写入“最好”排名。

## 复现

首次运行使用仓库外隔离环境（不会修改正式 Python 环境）：

```powershell
$trial = 'F:\VoiceRobustnessV1-trial'
& 'C:\Python313\python.exe' -m venv $trial
& "$trial\Scripts\python.exe" -m pip install --disable-pip-version-check --no-cache-dir 'vosk==0.3.45' 'sherpa-onnx==1.13.5'
& "$trial\Scripts\python.exe" -c "import numpy; print(numpy.__version__)"
```

然后在 `F:\MotionControl-App` 执行：

```powershell
& 'F:\MotionControl-App\test_results\voice_robustness_v1\run_experiment.ps1'
```

脚本优先尝试 Windows SAPI；本机 SAPI 只产生 46 字节空 WAV，因此本轮使用两种 Microsoft Edge 中文声音生成压力代理语料，并在 `tts_manifest.json` 中保留实际来源。运行时识别仍是离线的；试验固定使用 16 kHz、单声道、16-bit PCM 输入。

## 产物

- `manifest.json`：语料、退化条件、候选、模型/包版本和哈希。
- `RESULT.json` / `RESULT.csv`：逐条预测和聚合指标。
- `REPORT.md`：排名、极端条件失效点、资源指标、候选筛选和下一步建议。
- `corpus/`：Edge TTS fallback 生成的短语 WAV；噪声模板由脚本按固定种子在内存中合成，不批量落盘。
- `generate_tts.ps1`、`run_experiment.ps1`、`run_experiment.py`：可重复生成/运行脚本。

## 解释边界

Windows SAPI/Edge 合成语音和白噪声/粉红噪声/风扇型噪声只是压力代理，不是手机麦克风、真人小声或真实房间噪声的验收。报告中的“第一名”只表示在这套固定代理样本上的相对结果；如果误触或紧停召回不满足产品阈值，报告会明确写“当前无产品可接受胜者”。
