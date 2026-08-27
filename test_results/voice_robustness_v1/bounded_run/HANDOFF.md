# MotionControl 中文语音离线对比交接

产物：`F:\MotionControl-App\test_results\voice_robustness_v1\bounded_run`
正式仓库快照：`ed426809cb99877b36f4e2fa065014ca43d18a8a`；本试验未修改生产源码，`test_results/voice_robustness_v1` 之外的原有脏改动状态：`dirty（运行前已有/保留）`。

综合相对第一名：`vosk_constrained/light_preprocess`；低资源（仅模型体积）：`sherpa_phrase_kws_current`；强噪声相对第一名：`vosk_constrained/light_preprocess`，但未达到紧停保守线。

产品筛选结论：相对第一名通过本轮保守筛选线，但仍需真人验收。

实测候选只有当前 Sherpa 全短语 KWS、Vosk 受限词表、Vosk 开放识别；Whisper/通用 sherpa-onnx ASR 未因缺少本地可核验模型而参加排名。TTS+合成噪声只是压力代理，不能冒充手机麦克风/真人验收。

建议下一步：在输出禁用下录制真人正常/小声/真实房间噪声，回放同一 manifest，再决定是否只扩 KWS 同义词或另开轻量流式 ASR 试验。

完整数据：`RESULT.json`、`RESULT.csv`、`REPORT.md`；复现：`run_experiment.ps1`。
