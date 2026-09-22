"""版本号，全项目只有这一处。

以前它散在十一个地方：server.py、语音目录的 product_version、两条写死 "2.0.0"
的测试断言、三个打包脚本里的路径、部署脚本、README 里的 APK 文件名，还有产物
自己的名字。发 2.0.1 的时候漏掉任何一处，就会出现界面写一个数、文件名写另一个
数的状态——而反馈表单恰好让用户填"你用的版本"，填的和跑的对不上，那条反馈就查
不下去了。

放在这里而不是仓库根的 VERSION 文件，是因为 server.py import 它，于是它自动跟着
发布包走（stage_release 是按 import 图收文件的），不需要再往打包清单里加一条。

约定就是语义化版本：
    2.0.x   补丁——修 bug、改文案，不加功能
    2.x.0   加了功能，但老配置照常能用
    x.0.0   老配置要迁移，或者玩法变了

改完跑一遍：
    python -m pytest tests/test_version_is_single_sourced.py
它会把所有又把数字写回去的地方指出来。
"""

from __future__ import annotations

VERSION = "2.1.1"

__all__ = ["VERSION"]
