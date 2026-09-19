"""重新 stage 一遍，不该把已经签好的名弄丢。

签名不是源文件，所以打扫时它一直是被顺手删掉的那一类——包括什么都没改的那种
空跑。于是「stage → 签名 → 因为任何原因再 stage 一次 → 打包」会发出一个手机
拒绝安装的包，而且中间没有一步会报错：只有等到有人拿真手机去更新才看得出来。

今天就撞上了：发布包签完名之后，push.sh 里的 build_app_bundle 又 restage 了一次，
签名当场没了。那次侥幸——zip 是在那之前打的。

规则是两句话：内容没变就留着签名，内容变了就删掉。对着已经挪动过的字节的签名
比没有签名更糟，它会在手机上验签失败，而不是在它失效的那一刻失效。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.stage_release import stage_phone_web  # noqa: E402


@pytest.fixture()
def built(tmp_path):
    """一份构建好的手机网页产物。"""
    source = tmp_path / "dist"
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text("<html>一</html>", encoding="utf-8")
    (source / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return source


def signature(target: Path) -> Path:
    return target / "phone_web" / ".signature"


def test_staging_twice_keeps_the_signature(built, tmp_path):
    """第二次 stage 什么都没改，签名依然成立。"""
    target = tmp_path / "release"
    stage_phone_web(built, target)
    signature(target).write_text("假装这是签名", encoding="utf-8")

    stage_phone_web(built, target)
    assert signature(target).is_file(), "空跑一次就把签名弄丢了"


def test_changed_content_drops_the_signature(built, tmp_path):
    """签的是上一份字节。内容变了还留着，它会在手机上炸，而不是在这里。"""
    target = tmp_path / "release"
    stage_phone_web(built, target)
    signature(target).write_text("假装这是签名", encoding="utf-8")

    (built / "index.html").write_text("<html>二</html>", encoding="utf-8")
    stage_phone_web(built, target)
    assert not signature(target).exists(), "内容变了，旧签名必须跟着作废"


def test_a_removed_source_file_also_drops_it(built, tmp_path):
    """少一个文件同样改变了这份包。"""
    target = tmp_path / "release"
    stage_phone_web(built, target)
    signature(target).write_text("假装这是签名", encoding="utf-8")

    (built / "assets" / "app.js").unlink()
    stage_phone_web(built, target)
    assert not (target / "phone_web" / "assets" / "app.js").exists()
    assert not signature(target).exists()


def test_other_leftovers_are_still_swept(built, tmp_path):
    """只有签名享受这个待遇，别的陈年文件照删。"""
    target = tmp_path / "release"
    stage_phone_web(built, target)
    stale = target / "phone_web" / "上一版留下的.js"
    stale.write_text("旧的", encoding="utf-8")

    stage_phone_web(built, target)
    assert not stale.exists()
