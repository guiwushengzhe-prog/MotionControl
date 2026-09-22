"""更新包那个目录必须先清空再解，不能只靠 tar --overwrite。

服务端的清单是**按目录里的实际内容**现算 digest 的，而签名是打包时按那一份签的。
`--overwrite` 只替换同名文件、从不删除已经不在包里的，于是上一次构建的残留会留在
目录里，让服务端算出的 digest 和签名里那个对不上——电脑端于是拒绝整份更新包，
而且是静默的：没有一台机器会说自己更新失败了，只是永远停在旧版本。

2026-09-22 真发生过：两个旧的 js 残留（index-CHpgfHBk.js、web-CwoJBkv_.js）让
线上自更新通道整个停摆，是事后比对线上清单和本地产物才看出来的。

另一半同样要紧：不带新包的部署**不能**清。那种部署要保住服务器上现有的那份，
清了就等于把还在服务的更新包删掉，换来一个空目录。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "cloud" / "deploy" / "push.sh").read_text(encoding="utf-8")


def test_the_bundle_directory_is_cleared_before_unpacking():
    clear = SCRIPT.find('rm -rf "$APP_DIR/cloud/app_bundle"')
    unpack = SCRIPT.find("tar xzf /tmp/motioncontrol-cloud.tar.gz")
    assert clear != -1, "远端没有清空更新包目录，残留会让签名对不上"
    assert unpack != -1, "找不到解包那一步"
    assert clear < unpack, "清空排在解包之后了，等于把刚解出来的包删掉"


def test_clearing_only_happens_when_a_new_bundle_travels():
    """不带新包的部署要保住服务器上现有的那份。"""
    clear = SCRIPT.find('rm -rf "$APP_DIR/cloud/app_bundle"')
    guard = SCRIPT.rfind('if [ "${BUNDLE_FRESH:-0}" = "1" ]', 0, clear)
    assert guard != -1, "清空那一步没有被 BUNDLE_FRESH 守着，会误删还在服务的包"


def test_the_flag_is_set_where_the_bundle_is_actually_built():
    """标志必须挂在真的重打了包的那条分支上，不能挂在外面。"""
    built = SCRIPT.find("cp -r build/app_bundle cloud/app_bundle")
    flag = SCRIPT.find("BUNDLE_FRESH=1", built)
    # 匹配 echo 语句本身，不是那句话——同一句话在上面的注释里也出现过一次，
    # 只找文字会命中注释，把顺序判断整个带偏。
    skipped = SCRIPT.find('echo "    没有发布包')
    assert built != -1 and flag != -1, "重打包之后没有置位 BUNDLE_FRESH"
    assert flag < skipped, "置位跑到了「没有发布包」那条分支之后"
    assert "BUNDLE_FRESH=0" in SCRIPT, "没有默认值，未定义时的行为说不清"


def test_the_flag_reaches_the_remote_shell():
    """远端是另一个 shell，本地变量不会自己过去。"""
    call = re.search(r'ssh "\$HOST" "([^"]*)bash -s"', SCRIPT)
    assert call, "找不到远端调用"
    assert "BUNDLE_FRESH=" in call.group(1), "标志没有传给远端，远端永远读到空值"
