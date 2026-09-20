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
    # app/ 里面，不是同级：电脑端自更新换的就是整个 app/，放在外面的东西永远
    # 不会被换，网页包的修复也就到不了任何人手里。
    return target / "app" / "phone_web" / ".signature"


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
    assert not (signature(target).parent / "assets" / "app.js").exists()
    assert not signature(target).exists()


def test_other_leftovers_are_still_swept(built, tmp_path):
    """只有签名享受这个待遇，别的陈年文件照删。"""
    target = tmp_path / "release"
    stage_phone_web(built, target)
    stale = signature(target).parent / "上一版留下的.js"
    stale.write_text("旧的", encoding="utf-8")

    stage_phone_web(built, target)
    assert not stale.exists()


def test_it_lands_inside_app_so_the_pc_update_carries_it(built, tmp_path):
    """放在 app/ 外面的东西，电脑端自更新一辈子不会换掉。

    自更新换的是整个 app/（app_update.promote 里 os.replace(staging, app_dir)），
    而更新包打的就是 app/ 的内容。phone_web 只要落在 app/ 同级，网页包的修复就
    到不了任何人手里，只能等下一次重装整个发布包——而「网页包能热更」这句承诺
    当初（3a04a03）许的就是让电脑端顺路把它带上。

    这件事出过一次，没有任何地方报错：两个功能隔两天做，后一个的范围把前一个的
    东西漏在了外面，部署输出、测试、界面全都正常。
    """
    target = tmp_path / "release"
    stage_phone_web(built, target)
    assert (target / "app" / "phone_web" / "index.html").is_file(), (
        "phone_web 没落在 app/ 里")
    assert not (target / "phone_web").exists(), (
        "phone_web 落在 app/ 同级了，电脑端自更新带不上它")


def test_the_bundle_builder_checks_the_signature_after_restaging():
    """顺序：restage 会在内容变了时丢掉签名，所以验签必须排在它之后、打包之前。

    排错了就会把一份没签名的网页包发给每一台电脑，再由电脑发给手机——手机全部
    安静拒绝，而电脑端这边一切正常，没有一处会说话。
    """
    text = (ROOT / "tools" / "build_app_bundle.py").read_text(encoding="utf-8")
    restage = text.index("restage(target)")
    check = text.index("check_phone_web_signature(target")
    packing = text.index("build(target / \"app\")")
    assert restage < check < packing, (
        "验签没夹在 restage 和打包之间：签名可能已经被 restage 丢掉，或者检查得太晚")


def test_a_signature_in_the_source_does_not_wipe_the_staged_one(built, tmp_path):
    """源目录自己也可能被签过。那份签的是它自己的清单，不是发布包的。

    开发时电脑端直接从 switch/mobile/dist 供包给手机，那份就得签。把它当普通
    源文件拷过来的话，它和目标那份的签发时间不同，每次都判成“内容变了”，
    于是“没变就留着签名”那条逻辑彻底失效——每一次 restage 都把刚签好的删掉，
    而删完不报错，只有真手机去更新时才拒绝。2026-09-20 撞上过。
    """
    target = tmp_path / "release"
    stage_phone_web(built, target)
    signature(target).write_text("发布包自己的签名", encoding="utf-8")
    (built / ".signature").write_text("源目录的签名，签的是别的清单", encoding="utf-8")

    stage_phone_web(built, target)
    assert signature(target).is_file(), "源目录的签名把发布包的签名冲掉了"
    assert signature(target).read_text(encoding="utf-8") == "发布包自己的签名"


def test_the_app_sweep_leaves_phone_web_alone(tmp_path, monkeypatch):
    """app/ 的打扫不能碰 app/phone_web——那是 stage_phone_web 的地盘。

    打扫是按 import 图算的：app/ 里不在图里的文件一律当陈年文件删掉。
    phone_web 挪进 app/ 之后整个落在这个范围里，于是每次 stage 都是“先删光、
    再重建”，“内容没变就留着签名”永远不成立。后果不报错：打出的包没签名，
    而只有真手机去更新时才拒绝。2026-09-20 撞上过：签一次、stage 一次、
    签名就没了，来回两次才找到。
    """
    from tools.stage_release import plan

    app = tmp_path / "app"
    (app / "phone_web" / "assets").mkdir(parents=True)
    (app / "phone_web" / "index.html").write_text("x", encoding="utf-8")
    (app / "phone_web" / ".signature").write_text("sig", encoding="utf-8")
    (app / "phone_web" / "assets" / "a.js").write_text("y", encoding="utf-8")
    (app / "陈年文件.py").write_text("# 不在 import 图里", encoding="utf-8")

    _, stale = plan(tmp_path)
    names = {p.name for p in stale}
    assert "陈年文件.py" in names, "真正的陈年文件应该照样被清"
    assert not any("phone_web" in p.parts for p in stale), (
        "app/ 的打扫把 phone_web 里的文件当成陈年文件了")
