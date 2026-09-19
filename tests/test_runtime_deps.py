"""跑桌面端的这台机器，该装的东西真的装了没有。

和 test_shared_boundary.py 分开，是因为那份还要在 Linux CI 上跑，而那个 job 故意
只装 pytest——共享包必须在裸标准库上活着，这是它存在的意义。把「本机装了
cryptography 没有」写在那里，等于让一个跟边界无关的理由把每一次 push 都判红：
从 9 月 18 日起连续六次 CI 失败，全是这一条，而真正的边界检查一直是绿的。一个
天天报红的 CI，和没有 CI 是一样的。

这里只放"这个环境能不能真的跑起来"这一类，Linux 上跑不到，也不该跑。
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def test_cryptography_is_actually_installed():
    """缺了它，设备配对会静默降级成"不可用"，而不是报错。

    别的依赖缺了都在用到的那一刻当场报错，用户看得见：没有 OpenCV，摄像头就
    打不开并且说出来。device_pairing 把 ImportError 接住了，于是在开了
    require_paired_devices 的机器上，打包时漏掉这一个看起来会像是"每台手机都
    因为协议原因被拒"。
    """
    from motioncontrol import device_pairing

    assert device_pairing.crypto_available(), (
        "这个环境里没有 cryptography，设备配对会静默降级。"
        "跑一下：python tools/check_runtime_deps.py"
    )


def test_it_is_the_same_dependency_the_release_declares():
    """装着的和发布包声明要装的，得是同一件事。"""
    declared = (REPO / "requirements-runtime.txt").read_text(encoding="utf-8")
    assert "cryptography" in declared
