"""从明确指定的运行环境、模型和手机网页构建独立 Windows 安装包。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from motioncontrol.version import VERSION
from tools.release_paths import PC_DIR, RELEASE_DIR
from tools import stage_release


def run(*arguments: str | Path) -> None:
    subprocess.run([str(arg) for arg in arguments], cwd=ROOT, check=True)


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def vc_runtime(directory: Path) -> list[dict]:
    required = ('concrt140.dll', 'msvcp140.dll', 'msvcp140_1.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')
    optional = ('msvcp140_2.dll', 'msvcp140_atomic_wait.dll', 'msvcp140_codecvt_ids.dll', 'vcruntime140_threads.dll')
    names = [*required, *(name for name in optional if (directory / name).is_file())]
    for name in names:
        data = (directory / name).read_bytes()
        pe = struct.unpack_from('<I', data, 0x3c)[0]
        if data[:2] != b'MZ' or struct.unpack_from('<H', data, pe + 4)[0] != 0x8664:
            raise ValueError(f'需要64位微软运行文件：{name}')
    environment = {**os.environ, 'MOTIONCONTROL_VC_RUNTIME_DIR': str(directory.resolve()),
                   'MOTIONCONTROL_VC_RUNTIME_FILES': json.dumps(names)}
    command = """$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$records=@(foreach($name in (ConvertFrom-Json $env:MOTIONCONTROL_VC_RUNTIME_FILES)) {
  $file=Join-Path $env:MOTIONCONTROL_VC_RUNTIME_DIR $name
  $signature=Get-AuthenticodeSignature -FilePath $file
  if($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') { throw "微软运行文件签名无效：$name" }
  [pscustomobject]@{name=$name;version=(Get-Item -LiteralPath $file).VersionInfo.FileVersion;sha256=(Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()}
})
ConvertTo-Json -InputObject $records -Compress
"""
    modern = shutil.which('pwsh')
    shell = Path(modern) if modern else Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    if not modern:
        environment['PSModulePath'] = str(shell.parent / 'Modules')
    return json.loads(subprocess.check_output([str(shell), '-NoProfile', '-Command', command],
                                              env=environment, text=True, encoding='utf-8'))


def stage(args: argparse.Namespace) -> Path:
    target = args.target.resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError(f'请使用新的空构建目录：{target}')
    if sha256(args.python_archive) != args.python_sha256.lower():
        raise ValueError('运行环境压缩包与指定的官方摘要不同')
    packages = args.python_env / 'Lib/site-packages'
    environment = {**os.environ, 'PYTHONNOUSERSITE': '1'}
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    probe = subprocess.check_output(
        [str(args.python_env / 'Scripts/python.exe'), '-c',
         'import json,sys,struct;print(json.dumps([sys.version_info.major,sys.version_info.minor,struct.calcsize("P")]))'],
        text=True, env=environment)
    major, minor, pointer = json.loads(probe)
    if pointer != 8 or not packages.is_dir():
        raise ValueError('需要64位 Windows 运行环境及其依赖库')
    # 虚拟环境的注入脚本不进入安装包；其他路径文件也不能指向开发电脑。
    for path in packages.glob('*.pth'):
        if path.name == '_virtualenv.pth':
            continue
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.startswith(('#', 'import ')) and Path(line).is_absolute():
                raise ValueError(f'依赖库含有开发电脑的绝对路径：{path.name}')
    phone_web = stage_release.resolve_phone_web(str(args.phone_web))
    stage_release.read_phone_web_provenance(phone_web)
    runtime_records = vc_runtime(args.vc_runtime_dir)
    models = {
        'mediapipe/pose_landmarker_full.task': args.model_root / 'mediapipe/pose_landmarker_full.task',
        'mediapipe/pose_landmarker_full_compatible_075.task': args.model_root / 'mediapipe/pose_landmarker_full_compatible_075.task',
        'mediapipe/pose_landmarker_heavy.task': args.model_root / 'mediapipe/pose_landmarker_heavy.task',
        'mediapipe/gesture_recognizer.task': args.gesture_model,
    }
    if args.head_model:
        models['hfhpe/' + args.head_model.name] = args.head_model
    for source in [*models.values(), args.vigem_client, args.phone_signing_key]:
        if not source.is_file():
            raise ValueError(f'缺少构建输入：{source}')
    if not (args.vosk_model / 'am/final.mdl').is_file():
        raise ValueError('语音模型目录不完整')
    python = target / 'python'
    python.mkdir(parents=True)
    with zipfile.ZipFile(args.python_archive) as archive:
        if f'python{major}{minor}.dll' not in archive.namelist():
            raise ValueError('嵌入式解释器与依赖库的 Python 版本不一致')
        archive.extractall(python)
    # Python 自带的两份运行文件不足以供 ONNX 和 MediaPipe 使用，配套完整版本随包部署。
    for record in runtime_records:
        copy_file(args.vc_runtime_dir / record['name'], python / record['name'])
    (python / f'python{major}{minor}._pth').write_text(
        f'python{major}{minor}.zip\n.\nLib/site-packages\n../app\nimport site\n', encoding='utf-8')
    print('正在复制运行环境与识别模型…', flush=True)
    shutil.copytree(packages, python / 'Lib/site-packages', ignore=shutil.ignore_patterns(
        '__pycache__', '*.pyc', '*.pyo', '_virtualenv.py', '_virtualenv.pth'))
    for relative, source in models.items():
        copy_file(source, target / 'models' / relative)
    speech_name = args.vosk_model.name
    shutil.copytree(args.vosk_model, target / 'models' / speech_name)
    copy_file(args.vigem_client, target / 'native/ViGEmClient.dll')
    config = target / 'app/config'
    config.mkdir(parents=True)
    for name, value in {
        'model_root.txt': '../models', 'vosk_model_path.txt': '../models/' + speech_name,
        'vigemclient_dll.txt': '../native/ViGEmClient.dll',
    }.items():
        (config / name).write_text(value + '\n', encoding='utf-8')
    run(sys.executable, '-X', 'utf8', ROOT / 'tools/stage_release.py',
        '--target', target, '--phone-web', phone_web)
    run(sys.executable, '-X', 'utf8', ROOT / 'tools/sign_phone_web.py',
        '--bundle', target / 'app/phone_web', '--key', args.phone_signing_key)
    stage_release.verify_release_provenance(target / 'app', check_sources=True)
    run(sys.executable, '-X', 'utf8', ROOT / 'tools/check_runtime_deps.py', '--bundle', python)
    compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    run(compiler, '/nologo', '/target:winexe', '/optimize+',
        '/reference:System.Windows.Forms.dll', '/reference:System.Web.Extensions.dll',
        '/win32icon:' + str(ROOT / 'web/assets/motioncontrol.ico'),
        '/out:' + str(target / 'MotionControl.exe'), ROOT / 'tools/windows/MotionControl.cs')
    # 对应入口及安装向导的构建源码随包保留，不含本机签名密钥或用户配置。
    for relative in ('tools/windows/MotionControl.cs', 'tools/windows/MotionControl.iss',
                     'tools/build_windows_installer.py', 'tools/stage_release.py',
                     'tools/release_paths.py', 'requirements-runtime.txt', 'requirements-multicore-runtime.txt'):
        copy_file(ROOT / relative, target / 'source' / relative)
    manifest = {
        'schema': 'motioncontrol.windows_installer.v1', 'version': VERSION,
        'python_archive_sha256': sha256(args.python_archive),
        'microsoft_runtime': runtime_records,
        'models': [{'path': relative, 'sha256': sha256(source)} for relative, source in models.items()],
        'files': [{'path': path.relative_to(target).as_posix(), 'size': path.stat().st_size,
                   'sha256': sha256(path)} for path in sorted(target.rglob('*')) if path.is_file()],
    }
    (target / 'installer-payload.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('python-archive', 'python-env', 'vc-runtime-dir', 'model-root', 'gesture-model', 'vosk-model',
                 'vigem-client', 'phone-web', 'phone-signing-key', 'iscc'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--python-sha256', required=True, help='官方运行环境压缩包的 SHA-256 摘要')
    parser.add_argument('--head-model', type=Path)
    parser.add_argument('--target', type=Path, default=PC_DIR)
    parser.add_argument('--output-dir', type=Path, default=RELEASE_DIR / '安装包')
    args = parser.parse_args()
    try:
        target = stage(args)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        print('正在生成安装向导…', flush=True)
        run(args.iscc.resolve(), '/Q', '/DAppVersion=' + VERSION, '/DPayloadDir=' + str(target),
            '/DOutputDir=' + str(args.output_dir.resolve()), ROOT / 'tools/windows/MotionControl.iss')
        executable = args.output_dir / f'MotionControl-Setup-{VERSION}.exe'
        digest = sha256(executable)
        executable.with_suffix('.exe.sha256').write_text(digest + '  ' + executable.name + '\n', encoding='utf-8')
        print(f'安装包：{executable}\nSHA-256：{digest}')
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'构建未完成：{error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
