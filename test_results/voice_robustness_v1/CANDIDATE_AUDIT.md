# 中文离线语音候选审计（2026-08-28）

范围：只在独立临时依赖目录 `F:\VoiceRobustnessV1-trial-312\site-packages313` 中运行；不改 `F:\MotionControl-App` 生产源码，不连接游戏输出。运行时输入是本地 WAV，候选均离线。

## 已实测候选

| 候选 | 本地版本/模型 | 模型目录大小 | SHA256 | Windows/Android | 许可证与处置 |
|---|---|---:|---|---|---|
| Vosk 受限词表、Vosk 开放识别 | `vosk 0.3.45`；`vosk-model-small-cn-0.22` | 68,292,271 B（约 65.1 MiB） | `db67e7419f1ca224989e773d411f0dc85ac7c6cce7aedd6c92293fa6e2a1528e` | 官方 API 覆盖 Windows/Python 与 Android；官方模型页将中文 small 模型列为 Android/RPi 轻量模型 | API 仓库 Apache-2.0；模型页列 Apache 2.0。模型目录自带 README，仍应随部署包保留来源与通知。 |
| 当前 Sherpa 全短语 KWS | `sherpa-onnx 1.13.5`；`sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20` | 40,744,741 B（约 38.9 MiB） | `e81b6ca82c82bd905b1d235d2680fa0cc7c5a3c90cbcd013878584bf7581c409` | 官方项目提供 Windows 预编译/ Python 路径和 Android 构建说明 | 引擎 Apache-2.0；本地权重目录没有独立许可证。上游仍有针对该具体 zh-en KWS 权重再分发授权的未决澄清记录，因此本次只作内部实测，不打包、不宣称可商业再分发。 |

## 资料候选（未计入排名）

- `sherpa-onnx` 通用中文 ASR/Zipformer：官方目录列出 Windows/Android 可用路线，但本机没有第二套已选中文 ASR 权重、端点配置和可复核许可证，未下载、未测。
- `whisper.cpp` tiny/base：官方仓库为 MIT，Android 示例建议 tiny/base；本机没有可复核的中文模型和 Windows 二进制，未下载、未测。不能用资料推断实时延迟或准确率。
- FunASR/Paraformer：本机没有在本试验目录内可复核的轻量权重/运行链，未测；不把其他环境的缓存当成候选。

## 语料来源边界

Windows SAPI 能枚举中文声音，但在当前非交互主机实际只写出 46 字节空 WAV；因此使用两种 Microsoft Edge 中文声音生成测试语料，随后转为 16 kHz、单声道、PCM16。生成音频只用于压力代理，运行时仍是离线识别；没有把网络 TTS 当成产品能力，也没有真人或手机麦克风证据。

## 官方资料

- Vosk API（Apache-2.0、Windows/Android 等绑定）：<https://github.com/alphacep/vosk-api>
- Vosk 中文模型列表与模型大小/许可证：<https://alphacephei.com/vosk/models>
- Sherpa-ONNX 项目与平台支持：<https://github.com/k2-fsa/sherpa-onnx>
- Sherpa-ONNX Android/Windows 构建说明：<https://github.com/k2-fsa/sherpa/blob/master/docs/source/onnx/android/build-sherpa-onnx.rst>
- Sherpa 具体 KWS 权重授权仍需确认：<https://github.com/k2-fsa/sherpa-onnx/issues/3760>、<https://github.com/k2-fsa/sherpa-onnx/issues/3852>
- whisper.cpp MIT 与 Android 示例：<https://github.com/ggml-org/whisper.cpp>、<https://github.com/ggml-org/whisper.cpp/blob/master/examples/whisper.android/README.md>
