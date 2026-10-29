"""为自动测试准备官方中文语音模型，已缓存完整模型时直接复用。"""
from __future__ import annotations

import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

MODEL = 'vosk-model-small-cn-0.22'
URL = f'https://alphacephei.com/vosk/models/{MODEL}.zip'
REQUIRED = ('am/final.mdl', 'conf/model.conf', 'graph/HCLr.fst', 'graph/Gr.fst')


def prepare(folder: Path) -> Path:
    target = folder / MODEL
    if not all((target / name).is_file() for name in REQUIRED):
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as staging:
            archive = Path(staging) / 'model.zip'
            with urllib.request.urlopen(URL, timeout=120) as response, archive.open('wb') as output:
                shutil.copyfileobj(response, output)
            with zipfile.ZipFile(archive) as source:
                source.extractall(folder)
    if not all((target / name).is_file() for name in REQUIRED):
        raise RuntimeError('中文语音模型未完整准备，请检查下载与缓存')
    return target.resolve()


if __name__ == '__main__':
    print(prepare(Path(sys.argv[1])))
