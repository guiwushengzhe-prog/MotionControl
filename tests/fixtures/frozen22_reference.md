# 冻结参考的回归样本

`frozen22_reference.json` 来自 2026-09-01 的 R3 A/B 实际录制归档
`r3_minimal_ab_20260901/expanded`。只保留脸部点坐标，没有图像、视频或声音。

- A、B 各取 16 个等间隔有效帧，并补充转向读数的最大、最小帧，共各 18 帧。
- 特征、尺度和转向预期由归档中的独立冻结程序
  `MotionControl_head_control_v186_stageA_frozen22_evidence.py` 计算。
  生成器不导入产品算法，预期值不能用当前产品代码更新。
- 原录制、校准文件和参考程序的 SHA-256（文件内容指纹）都记在样本里。
- 常规测试直接对这些预期值校验。设置 `MOTIONCONTROL_FROZEN22_ROOT`
  后，同一测试还对 A/B 完整记录逐帧比较独立参考程序；指定归档缺文件会失败。

确需从已审核归档重新提取时：

```powershell
python tools/make_frozen22_test_fixture.py "归档的 expanded 目录" tests/fixtures/frozen22_reference.json
```
