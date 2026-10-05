"""vendor/ 里的纯 Python 包跟着 app/ 更新：随包 Python 里缺它时也能用上。

程序更新不换 python/。加扫码连接时新依赖了 qrcode，只换程序的更新装到老安装上，
服务一启动就 ImportError、被当成坏包退回——这里守住"缺包也能更新"这件事。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_vendored_qrcode_draws_the_code_without_any_site_packages():
    # -S：不加载 site-packages，模拟随包 Python 里没装 qrcode 的老安装。
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"sys.path.append({str(ROOT / 'vendor')!r})\n"
        "from motioncontrol.connection_code import connection_code, qrcode\n"
        "assert qrcode is not None and 'vendor' in qrcode.__file__, qrcode\n"
        "data = connection_code('0123456789ab', 'pc', [], 8765)\n"
        "assert data['svg'] and '<svg' in data['svg']\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-S", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr


def test_server_appends_vendor_after_installed_packages_and_the_release_ships_it():
    from tools.stage_release import COPY_TREES

    source = (ROOT / "server.py").read_text(encoding="utf-8")
    assert 'sys.path.append(str(_APP_DIR / "vendor"))' in source, "必须追加在末尾，已装的包优先"
    assert source.index('sys.path.append(str(_APP_DIR / "vendor"))') < source.index("from motioncontrol.connection_code")
    assert "vendor" in COPY_TREES


def test_every_vendored_package_is_pure_python_and_licensed():
    vendor = ROOT / "vendor"
    readme = (vendor / "README.md").read_text(encoding="utf-8")
    for package in (path for path in vendor.iterdir() if path.is_dir() and path.name != "__pycache__"):
        compiled = [p.name for p in package.rglob("*") if p.suffix in {".pyd", ".so", ".dll"}]
        assert not compiled, f"{package.name} 带编译扩展，不能放 vendor/：{compiled}"
        assert (vendor / f"LICENSE-{package.name}.txt").is_file(), f"{package.name} 缺许可证原文"
        assert f"| {package.name} |" in readme, f"vendor/README.md 的表里没有 {package.name}"
