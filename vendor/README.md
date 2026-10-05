# vendor：随程序更新的纯 Python 第三方包

程序自动更新只换 `app/`，不换随包的 `python/`。于是新功能一旦依赖一个新的第三方包，
老安装更新完就缺它——2.3.x 加扫码连接时的 `qrcode` 就是这样：只换程序的更新装上去，
服务会因为 `import qrcode` 起不来，被当成坏包自动退回。

放在这里的包会跟着 `app/` 一起发出去（`tools/stage_release.py` 的 `COPY_TREES`），
`server.py` 启动时把这个目录**追加**到 `sys.path` 末尾：随包的 Python 里已经装了的
照样优先用，只有缺的时候才用这里这一份。

只放满足下面三条的包：

1. 纯 Python，没有编译过的扩展（`.pyd`/`.so`）——那些跟着 Python 版本走，必须装进 `python/`；
2. 许可证允许随程序再分发，并把许可证原文放在这里；
3. 运行时真的会用到（`requirements-runtime.txt` 里也要写上，开发环境照常 pip 安装）。

| 包 | 版本 | 许可证 | 来源 |
|---|---|---|---|
| qrcode | 8.2 | BSD（`LICENSE-qrcode.txt`，内含所移植的 pyqrnative 的 MIT 声明） | https://pypi.org/project/qrcode/8.2/ ，去掉了 `tests/` |

升级：`pip install --target <临时目录> --no-deps qrcode==<新版本>`，把包目录换进来，
更新上表和许可证文件。
