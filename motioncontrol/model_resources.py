"""模型资源共用诊断与迁移定位；轮询只查文件状态，重定位才验摘要并试加载。"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from . import config_transaction
from .depth_comparison_worker import read_packet
from .settings_persistence import SettingsPersistence, write_failure_reason

ACTIVE_MODEL = "mocapnet_personal"
SOURCE_FILES = ("tools.py", "MocapNETONNX.py", "holisticPartNames.py")
MODEL_FILES = ("upperbody_configuration.json", "neuralNetworkInputs.list", "neuralNetworkOutputs.list",
               "neuralNetworkJoints.list", "outputOffsets.csv", "outputScalars.csv", "outputMinima.csv", "outputMaxima.csv")


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_registry(data):
    if not isinstance(data, dict) or not isinstance(data.get("python"), str) or not data["python"].strip() or not isinstance(data.get("models"), dict):
        raise ValueError("模型登记需包含运行程序和模型列表")
    if not isinstance(data.get("relocation", {}), dict):
        raise ValueError("模型迁移记录格式无效")
    if "root" in data.get("relocation", {}) and not isinstance(data["relocation"]["root"], str):
        raise ValueError("模型依赖根目录登记无效")
    pinned = data.get("relocation", {}).get("sha256_by_path", {})
    if not isinstance(pinned, dict) or any(not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts
            or not isinstance(digest, str) or len(digest) != 64 for name, digest in pinned.items()):
        raise ValueError("模型配套文件摘要登记无效")
    for spec in data["models"].values():
        if not isinstance(spec, dict) or any(not isinstance(spec.get(key), str) or not spec[key].strip() for key in ("source", "checkpoint", "sha256")):
            raise ValueError("模型目录或权重登记不完整")
        if not isinstance(spec.get("packages", []), list) or any(not isinstance(path, str) or not path for path in spec.get("packages", [])):
            raise ValueError("扩展库目录登记无效")
        if any(key in spec and (not isinstance(spec[key], str) or not spec[key].strip()) for key in ("readout", "license")):
            raise ValueError("个人读出或许可证路径登记无效")
    return data


def resource_paths(registry, ident):
    spec = registry.get("models", {}).get(ident, {})
    paths = [("运行程序", registry.get("python"), False), ("模型源码", spec.get("source"), True),
             ("模型权重", spec.get("checkpoint"), False)]
    if ident == ACTIVE_MODEL:
        paths += [("个人倾角读出", spec.get("readout"), False), ("原许可证", spec.get("license"), False)]
        paths += [("扩展库目录", path, True) for path in spec.get("packages", [])]
        if spec.get("source"):
            paths += [("源码配套文件", str(Path(spec["source"]) / name), False) for name in SOURCE_FILES]
        if spec.get("checkpoint"):
            paths += [("模型配套文件", str(Path(spec["checkpoint"]).parent / name), False) for name in MODEL_FILES]
    return paths


def dependency_root(registry):
    pinned = registry.get("relocation", {}).get("root")
    if isinstance(pinned, str) and pinned:
        return Path(pinned)
    paths = [str(Path(path) if directory else Path(path).parent)
             for _, path, directory in resource_paths(registry, ACTIVE_MODEL) if path and Path(path).is_absolute()]
    try:
        result = Path(os.path.commonpath(paths))
    except ValueError:
        # 模型与运行程序分盘时，模型目录仍可单独迁移；其它缺失项明确列出。
        paths = [str(Path(registry["models"][ACTIVE_MODEL][key]).parent) for key in ("source", "checkpoint", "readout")]
        result = Path(os.path.commonpath(paths))
    if not paths or result == Path(result.anchor):
        raise ValueError("依赖未登记在共同目录中，请先按模型准备工具重新登记")
    return result


class ModelResources:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._verification = threading.Lock()
        self._cache_key = object()
        self._document = {}
        self._error = ""
        self._revision = "unread"
        self._diagnostics = None
        self._diagnosed_at = -float("inf")
        self._recovery_error = ""
        # 启动阶段按共用规则恢复损坏登记；模型是可选功能，失败不影响原有人体识别。
        try:
            self.recover()
        except (OSError, ValueError, TypeError, AttributeError) as error:
            self._recovery_error = "模型登记恢复未完成：" + (str(error) if isinstance(error, ValueError) else write_failure_reason(error))

    def recover(self):
        with self._lock:
            if self.path.exists() or self.path.with_name(self.path.name + ".last-good").exists():
                store = SettingsPersistence(self.path, label="单目前后倾模型登记", validate=validate_registry)
                if store.blocked:
                    raise ValueError(store.error)
                if store.status()["state"] != "ready":
                    raise ValueError(store.error or "模型登记尚未保存完整有效副本")
            self._recovery_error = ""
            self._cache_key, self._diagnostics = object(), None

    def _stamp(self):
        try:
            value = self.path.stat()
            return value.st_mtime_ns, value.st_size, value.st_ino
        except FileNotFoundError:
            return None
        except OSError as error:
            return "unreadable", getattr(error, "winerror", error.errno)

    def read(self):
        with self._lock:
            stamp = self._stamp()
            if self._cache_key != stamp:
                try:
                    document = validate_registry(json.loads(self.path.read_text(encoding="utf-8-sig")))
                    self._document, self._error = document, ""
                except FileNotFoundError:
                    self._document, self._error = {}, "未找到本机模型登记，请从本机配置备份恢复或重新登记模型"
                except (OSError, ValueError, TypeError, AttributeError):
                    self._document, self._error = {}, "本机模型登记无法读取，原文件已保留，请恢复有效副本"
                self._cache_key, self._diagnostics = stamp, None
                self._revision = hashlib.sha256(json.dumps([self._document, self._error], sort_keys=True,
                    ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
            return copy.deepcopy(self._document), self._error

    @property
    def revision(self):
        # 逐帧提交只读缓存，不查磁盘或模型摘要。
        with self._lock:
            return self._revision

    def snapshot(self):
        with self._lock:
            registry, error = self.read()
            return registry, error, self._revision

    def diagnose(self, known_models, *, force=False):
        registry, error = self.read()
        with self._lock:
            if not force and self._diagnostics is not None and time.monotonic() - self._diagnosed_at < 2:
                return copy.deepcopy(self._diagnostics)
            result = {}
            for ident, info in known_models.items():
                issues = []
                if error:
                    issues.append({"name": "模型登记", "path": str(self.path), "reason": error})
                elif ident not in registry["models"]:
                    issues.append({"name": "模型登记", "path": str(self.path), "reason": "尚未登记这个模型"})
                else:
                    if ident == ACTIVE_MODEL:
                        spec = registry["models"][ident]
                        if any(not isinstance(spec.get(key), str) or len(spec[key]) != 64 for key in ("sha256", "readout_sha256")):
                            issues.append({"name": "模型校验摘要", "path": str(self.path), "reason": "摘要登记不完整"})
                    for label, path, directory in resource_paths(registry, ident):
                        try:
                            valid = bool(path and (Path(path).is_dir() if directory else Path(path).is_file()))
                        except OSError:
                            valid = False
                        if not valid:
                            issues.append({"name": label, "path": str(path or "未登记"), "reason": "目录不可用" if directory else "文件不可用"})
                    checked = registry.get("relocation", {})
                    spec = registry["models"][ident]
                    if (ident == ACTIVE_MODEL and checked.get("root") and
                            checked.get("model_sha256") == spec.get("sha256") and
                            checked.get("readout_sha256") == spec.get("readout_sha256")):
                        missing = {item["path"] for item in issues}
                        for relative in checked.get("sha256_by_path", {}):
                            path = Path(checked["root"]) / relative
                            try:
                                exists = path.is_file()
                            except OSError:
                                exists = False
                            if str(path) not in missing and not exists:
                                issues.append({"name": "已登记配套文件", "path": str(path), "reason": "文件不可用"})
                result[ident] = {**info, "available": not issues, "issues": issues}
            try:
                root = str(dependency_root(registry)) if registry else ""
            except (ValueError, KeyError):
                root = ""
            self._diagnostics = {"models": result, "root": root, "error": error,
                                 "warning": self._recovery_error,
                                 "verified_at": registry.get("relocation", {}).get("verified_at", "")}
            self._diagnosed_at = time.monotonic()
            return copy.deepcopy(self._diagnostics)

    @staticmethod
    def _rebase(registry, old_root, new_root):
        result = copy.deepcopy(registry)
        def moved(value):
            path = Path(value)
            return str(new_root / path.relative_to(old_root)) if path.is_relative_to(old_root) else value
        result["python"] = moved(result["python"])
        for spec in result["models"].values():
            for key in ("source", "checkpoint", "readout", "license"):
                if spec.get(key):
                    spec[key] = moved(spec[key])
            if "packages" in spec:
                spec["packages"] = [moved(path) for path in spec["packages"]]
        return result

    @staticmethod
    def _fingerprints(registry, root):
        paths = {Path(path) for _, path, directory in resource_paths(registry, ACTIVE_MODEL) if path and not directory}
        spec = registry["models"][ACTIVE_MODEL]
        paths.update(Path(spec["source"]).glob("*.py"))
        paths.update(path for path in Path(spec["checkpoint"]).parent.iterdir() if path.is_file())
        return {path.relative_to(root).as_posix(): file_hash(path) for path in sorted(paths)
                if path.is_relative_to(root)}

    def prepare(self, folder, program_root):
        if not self._verification.acquire(blocking=False):
            raise ValueError("正在核对模型文件，请等待本次检查完成")
        try:
            original = self.path.read_bytes()
            registry = validate_registry(json.loads(original.decode("utf-8-sig")))
            if ACTIVE_MODEL not in registry["models"]:
                raise ValueError("尚未登记个人前后倾模型，请先完成模型准备")
            root = Path(folder).expanduser()
            if not root.is_absolute() or not root.is_dir():
                raise ValueError("请选择实际存在的依赖根目录")
            root = root.resolve()
            previous = dependency_root(registry)
            candidate = self._rebase(registry, previous, root)
            for label, value, directory in resource_paths(candidate, ACTIVE_MODEL):
                if not value or not (Path(value).is_dir() if directory else Path(value).is_file()):
                    raise ValueError(f"所选目录缺少{label}：{value or '未登记'}；需要保持原依赖目录结构")
            spec = candidate["models"][ACTIVE_MODEL]
            if not all(Path(spec[key]).is_relative_to(root) for key in ("source", "checkpoint", "readout")):
                raise ValueError("所选目录不是已登记模型的新根目录，请保持原依赖目录结构")
            for key, expected_key, label in (("checkpoint", "sha256", "模型权重"), ("readout", "readout_sha256", "个人倾角读出")):
                if not spec.get(expected_key) or file_hash(spec[key]) != spec[expected_key]:
                    raise ValueError(label + "与原登记不相符，原位置未改动")
            previous_check = registry.get("relocation", {})
            # 正式登记了新网络或个人读出时重新建立基线；同一版本迁移仍逐项核对。
            same_version = (previous_check.get("model_sha256") == spec["sha256"] and
                            previous_check.get("readout_sha256") == spec.get("readout_sha256"))
            pinned = previous_check.get("sha256_by_path", {}) if same_version else {}
            for relative, expected in pinned.items():
                path = (root / relative).resolve()
                if not path.is_relative_to(root) or not path.is_file() or file_hash(path) != expected:
                    raise ValueError("依赖文件与上次通过的版本不相符：" + relative)
            self._probe(candidate, program_root)
            candidate["relocation"] = {"root": str(root), "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                        "model_sha256": spec["sha256"], "readout_sha256": spec["readout_sha256"],
                                        "sha256_by_path": self._fingerprints(candidate, root)}
            return candidate, hashlib.sha256(original).hexdigest()
        finally:
            self._verification.release()

    def _probe(self, registry, program_root):
        """同一独立处理器工作进程完成试加载，不采集画面、不执行游戏输出。"""
        with tempfile.TemporaryDirectory(prefix=".model-resource-check-", dir=self.path.parent) as directory:
            check = Path(directory) / "registry.json"
            check.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
            try:
                from .owned_process import run
                result = run([registry["python"], "-X", "utf8", "-m", "motioncontrol.mocap_lean_worker",
                    "--runtime", str(check), "--model", ACTIVE_MODEL], cwd=program_root, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                message, _ = read_packet(io.BytesIO(result.stdout))
            except (OSError, EOFError, subprocess.TimeoutExpired) as error:
                raise ValueError("运行环境未能完成试加载，原位置未改动；请核对运行程序和扩展库是否完整") from error
            if not isinstance(message, dict):
                raise ValueError("模型试加载未返回有效状态，原位置未改动")
            if result.returncode or message.get("kind") != "ready" or message.get("device") != "cpu":
                raise ValueError("模型试加载未通过：" + str(message.get("error") or "未确认仅处理器计算"))

    def commit(self, candidate, expected):
        with self._lock:
            if hashlib.sha256(self.path.read_bytes()).hexdigest() != expected:
                raise ValueError("模型登记已被其它操作修改，请重新检查，旧结果未覆盖当前登记")
            store = SettingsPersistence(self.path, label="单目前后倾模型登记", validate=validate_registry)
            if store.blocked:
                raise ValueError(store.error)
            try:
                config_transaction.replace_documents(store.documents(validate_registry(candidate)), store.journal_path)
            except (OSError, ValueError) as error:
                raise ValueError("模型位置未能保存，原登记已保留：" + write_failure_reason(error)) from error
            self._cache_key, self._diagnostics = object(), None


def choose_dependency_folder():
    if os.name != "nt":
        raise ValueError("此环境不支持系统目录选择，请填写迁移后的完整路径")
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = """Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$dialog = [System.Windows.Forms.FolderBrowserDialog]::new()
$dialog.Description = '请选择迁移后的模型依赖根目录，保持原来的 models 与 runtime 结构'
try { if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Write($dialog.SelectedPath) } }
finally { $dialog.Dispose() }
"""
    from .owned_process import run
    result = run([str(shell), "-NoProfile", "-STA", "-Command", script], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError("系统目录选择未完成，可直接填写迁移后的完整路径")
    return result.stdout.decode("utf-8-sig").strip()
