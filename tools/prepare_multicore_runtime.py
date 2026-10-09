"""准备本程序独立的多核运行环境，不替换系统或其他项目的依赖。"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from motioncontrol.owned_process import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, help="Python 3.12 的解释器路径")
    args = parser.parse_args()
    base = Path(args.python).resolve(strict=True)
    version = run([str(base), "-c", "import sys;print('.'.join(map(str,sys.version_info[:2])))"],
                  check=True, capture_output=True, text=True).stdout.strip()
    if version != "3.12":
        raise SystemExit("多核运行环境需要 Python 3.12；没有修改现有环境")
    target = ROOT / ".venv-multicore"
    python = target / "Scripts" / "python.exe"
    uv = shutil.which("uv")
    if not python.is_file():
        run(([uv, "venv", "--python", str(base), str(target)] if uv else
             [str(base), "-m", "venv", str(target)]), check=True)
    else:
        actual = run([str(python), "-c", "import sys;print(sys.version_info[:2])"],
                     check=True, capture_output=True, text=True).stdout.strip()
        if actual != "(3, 12)":
            raise SystemExit("已有多核运行环境版本不符，没有修改它")
    installer = ([uv, "pip", "install", "--link-mode=copy", "--python", str(python)] if uv else
                 [str(python), "-m", "pip", "install"])
    run(installer + ["--no-deps", "mediapipe==0.10.21"], check=True)
    run(installer + ["-r", str(ROOT / "requirements-multicore-runtime.txt")], check=True)
    # 导入整个任务接口，确保模型加载及其他功能不会因依赖缺失而启动失败。
    run([str(python), "-c", "import sys;sys.modules['tensorflow']=None;"
         "import mediapipe,cv2,numpy,cryptography,sounddevice,vosk,qrcode,psutil;"
         "assert hasattr(mediapipe.tasks.vision.PoseLandmarker,'get_graph_config');"
         "print('多核运行环境已准备好')"], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
