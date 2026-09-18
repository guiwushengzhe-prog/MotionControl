# 参与开发

欢迎。这个项目一个人做不完，尤其是游戏配置那部分——每个游戏的按键映射都要有人真的玩一遍才知道好不好用。

提问、报 bug、提配置、改代码都可以。不用先问"我能不能做某某"，直接开 issue 或者提 PR。

## 这个项目由三部分组成

| 部分 | 在哪 | 是什么 |
|---|---|---|
| 电脑端 | 本仓库根目录 | Python 服务 + 本地网页界面，摄像头识别、映射、虚拟手柄输出 |
| 手机端 | [MotionControl-Android](https://github.com/guiwushengzhe-prog/MotionControl-Android) | Capacitor + Android，手机当摄像头用 |
| 云端 | `cloud/` | 配置的上传、版本和分享，跑在 motioncontrol.guiwu-aware.icu |

手机端是独立仓库。只改电脑端或云端的话不用管它。

## 跑起来

需要 Windows（虚拟手柄驱动只有 Windows 版）、Python 3.13、一个摄像头。

发布包内置的和开发用的都是 3.13，更低的版本没试过。

```bash
pip install -r requirements-runtime.txt
```

然后双击 `START.bat`，浏览器打开 http://127.0.0.1:8766

手柄输出需要先装 ViGEmBus 驱动，[官方地址](https://github.com/nefarius/ViGEmBus/releases)。不装的话摄像头识别和鼠标控制照样能用，只是手柄按键没反应。

没用过这个软件的话，先按 [新手指南](docs/新手指南.md) 跑一遍，比直接读代码快。

云端部分怎么跑见 [cloud/README.md](cloud/README.md)，它不需要 Windows。

## 跑测试

```bash
python -m pytest -q
```

611 个，大约半分钟。提 PR 之前请确认它们全过。

有些测试需要摄像头或者虚拟手柄驱动，缺了会自动跳过（显示 skipped），这是正常的。

## 提 PR 之前

1. `python -m pytest -q` 全过
2. 一个 PR 只做一件事。同时改三个不相干的地方，会让审核变得很慢，也不好回滚
3. 说清楚为什么要改，不只是改了什么。"修复 bug" 这种描述看不出你解决的是哪个问题

代码风格没有强制要求，跟着你改动的那个文件周围的写法就行。注释用中文英文都可以。

## 三条必须遵守的规则

这三条不是风格偏好，违反了会真的出问题。

**一、配置校验规则只能有一份，在 `motioncontrol_shared/` 里**

电脑端和云端都调用它。如果两边各写一份，迟早会出现"电脑端存得进去、云端拒绝"这种情况，而且很难查。

**二、`cloud/` 不能 import 电脑端的模块**

云端跑在 Linux 上，而 `output_backend.py` 第一行就是 `import ctypes.wintypes`，在 Linux 上直接抛异常。所以任何越界的 import 会在 CI 上以真实的导入失败暴露出来，不是靠自觉。

**三、这三个 version 是三件不同的东西，不要合并**

| 名字 | 含义 |
|---|---|
| `/api/v1` | HTTP 接口版本 |
| `schema_version` | 配置文件本身的结构版本 |
| `revision_no` | 某个配置被改了第几次 |

合并任意两个，以后就没法单独改其中一个。

## 贡献游戏配置

内置约 200 个游戏配置，其中绝大多数是根据公开的手柄配置自动生成的，**没有人实际玩过**，界面上标着「实验配置」。

如果你实际玩过某个游戏并调好了映射，这是最有价值的贡献之一。两种方式：

- 在 motioncontrol.guiwu-aware.icu 上传你的配置（需要邀请码）
- 或者直接改 `game_profiles/` 里对应的 JSON 文件，提 PR

## 目录速览

```
server.py                 入口。启动脚本跑的就是它
motioncontrol/            程序本体
  control_kernel.py         摄像头采集、姿态识别、区域判定的主循环
  head_control.py           头部控制视角
  hand_mouse_control.py     手控鼠标
  input_bridge.py           手机连接、配对、姿态数据接收
  output_backend.py         键盘、鼠标、虚拟手柄输出（仅 Windows）
  voice_backend.py          语音识别
  game_profiles.py          游戏配置库
motioncontrol_shared/     电脑端和云端共用的配置规则
web/                      电脑端网页界面
cloud/                    云端服务
tools/                    打包、构建配置库、签名等工具
tests/                    测试
```

入口留在根目录、其余收进 `motioncontrol/`，是为了让人一眼看出从哪读起。

## 授权

本项目采用 AGPL-3.0。提交代码即表示你同意你的贡献以同样的授权发布。

如果你不希望自己的改动被这个授权约束，请不要提交。
