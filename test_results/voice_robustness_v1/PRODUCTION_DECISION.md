# MotionControl 1.00 语音落地决定

日期：2026-08-28

## 生产默认

- PC：`vosk-model-small-cn-0.22` + 字符级受限 grammar。
- grammar 来源：当前 `wake_word`、`emergency_stop_phrases`、实际 `mappings` 及每条 `synonyms`；包含“体感开始校准”。
- parser 保持严格唤醒词和映射匹配，并用 `compact_text()` 去除 Vosk 中文 token 间空格。
- Android：官方 Vosk `Recognizer(model, sampleRate, grammar)` + `SpeechService`；只发送最终 `voice_text`。不使用自写 `AudioRecord + acceptWaveForm`。
- Sherpa KWS 与 FunASR 不进入 1.00 活跃产品路径。

## DSP 边界

离线第一名的 `light_preprocess` 由整句裁静音、80 Hz 一阶高通和整句响度归一化共同构成。实时麦克风不能整句裁静音，Android `SpeechService` 也不暴露 PCM；现有评估没有证明拆出流式高通/自动增益后仍有等价收益。因此 1.00 不加入实时 DSP，避免静音被放大或改变紧急停止/误触表现。

## 最小生产链回归

使用评估语料中的同一原始小子集，输出回调为 no-op（不产生键鼠/手柄输出）：

- 正例 4/4 命中：体感开始校准、体感紧急停止、体感向左、体感加速。
- 负例 3/3 未触发：体感紧急制止、无唤醒词“开始校准”、普通句“今天天气不错”。
- 运行状态：`vosk_constrained_grammar`，共 53 条 grammar 项。

该回归只验证生产 grammar/parser 没有破坏同一离线小子集；不替代真人、手机麦克风、强噪声或长时间运行验收。
