# MotionControl 中文离线语音对比交接

产物目录：`F:\MotionControl-App\test_results\voice_robustness_v1`

结论：本轮合成语料压力代理的综合相对第一名为 `bounded_run/vosk_constrained/light_preprocess`（92.2% 意图、96.7% 紧停召回、1.4% 误触、44.8 ms P95）；它通过本轮保守线，但不等于真人或手机验收。强噪声 SNR 5/0 dB 没有候选达到紧停 95% 门槛。低资源候选是 Sherpa（模型约 38.9 MiB），质量不足，且具体 KWS 权重授权未明确。

复核文件：

- `bounded_run/RESULT.json`、`bounded_run/RESULT.csv`、`bounded_run/manifest.json`（轻量预处理）
- `bounded_run_raw/RESULT.json`、`bounded_run_raw/RESULT.csv`、`bounded_run_raw/manifest.json`（raw 对照）
- `REPORT.md`、`CANDIDATE_AUDIT.md`
- 关键文件 SHA256：`SHA256SUMS.txt`

运行依赖只装在 `F:\VoiceRobustnessV1-trial-312\site-packages313`；未改生产 Python/源码，未启用游戏输出。TTS+合成噪声是压力代理，不能冒充真人效果。
