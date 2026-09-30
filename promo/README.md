# 宣传与教学视频

这里是视频的渲染管线：**模拟关键点 → 画面 → 视频**。不拍摄、不录人，改一句文案重新渲染即可。

## 现在有什么

三种风格的 15 秒 demo，用同一套动作、同一套文案，只有画法和配乐不同，用来选定整个系列的风格：

| 风格 | 感觉 |
|---|---|
| `neon` 霓虹竞技 | 游戏预告片：深色底、发光骨架、故障字、电子乐 |
| `clean` 极简产品 | 发布会：白底、粗线小人、大字、木琴 |
| `doodle` 手绘漫画 | 综艺花字：纸面、抖动线条、贴纸、拟声字、尤克里里 |

## 渲染

需要 Node 22、Playwright（带 Chromium）和 ffmpeg。

```text
cd promo
npm install                                   # 只装字体
node render/render.mjs                        # 三种都录，输出到 promo/out/
node render/render.mjs --style neon           # 只录一种
node render/render.mjs --stills 1.5,6.2,8.4   # 只截静帧，调画面时用
```

找 ffmpeg 的顺序：`FFMPEG` 环境变量 → `PATH` 上的 `ffmpeg` → Python 包 `imageio-ffmpeg` 自带的那个。

浏览器里直接预览：在 `promo/` 下起任意静态服务器，打开 `render/demo.html?style=clean&play=1`。

## 文件

| 文件 | 作用 |
|---|---|
| `sim/rig.js` | 三维小人：关节角 → 33 个世界坐标 → 针孔相机投影成 MediaPipe 格式的关键点。转头时远侧耳朵会被挡住、可见度下降，和真模型一样。换一台相机就是另一个视角，以后做双目深度演示也用它。 |
| `render/storyboard.js` | 分镜：时间线、文案、动作脚本、游戏画面状态、音效事件。画面和声音都读这一份，所以脚落地和鼓点天然对齐。 |
| `render/stage.js` | 三种风格的画法。`renderAt(t)` 是纯函数，任意一帧可以单独重渲。 |
| `render/audio.mjs` | 配乐和音效全部现场合成（鼓、贝斯、Karplus-Strong 拨弦、混响），没有素材文件，没有版权问题。114 BPM 正好是一步 0.526 秒。 |
| `render/render.mjs` | 无头 Chromium 逐帧截图喂给 ffmpeg，混上音轨出 mp4。逐帧而不是实时录屏：慢机器只是录得慢，成片一样。 |

## 真实度

- 33 个点的编号、连线、坐标约定（不镜像、左肩 x 更大、z 以髋中点为 0）和 MediaPipe Pose 一致；显示时和软件界面一样做镜像。
- 原地踏步要左右脚各完成一步之后游戏画面才开始前进，停步后停下，和产品里的判定一致。
- 头控是速度型的：偏过死区才转，偏得越多转得越快。

## 下一步

风格和框架定下来之后：

1. 主宣传片（60 秒横屏）和 3 条竖屏短片按选定风格做。
2. 教学视频录真软件：软件已确认能在 Linux 上跑起来，再写一个"假手机"，按 `pose_features_v1`（`mc33-v3`）把 `sim/rig.js` 的关键点发到 `/ws/input`，界面上的反应都是真实的。
