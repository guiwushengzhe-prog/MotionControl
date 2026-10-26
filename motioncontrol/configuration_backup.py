"""本地完整配置包：先校验、预览，下次启动在配置对象创建前事务恢复。"""
from __future__ import annotations

from datetime import datetime, timezone
import base64
import copy
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tempfile
import uuid
import zipfile

from . import config_transaction as transaction
from motioncontrol_shared.user_layout import USER_DATA_FILES

SCHEMA = "motioncontrol.configuration-backup.v1"
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILES = 1024
MANIFEST = "manifest.json"
PREVIEW = ".configuration-restore-preview.zip"
PENDING = ".configuration-restore.pending.json"
PACKAGE = ".configuration-restore.pending.zip"
JOURNAL = ".configuration-restore.journal"
# 账户、配对、发现标识及缓存属于当前安装，不进包，也不会被恢复覆盖。
PORTABLE_KEYS = ("profile_selection", "custom_games", "motion_mappings", "voice_mappings",
                 "personal_voice", "key_macros", "custom_poses", "pose_actions", "fitness", "studio")
LOCAL_KEYS = ("general_settings", "head_profile", "body_reference", "scene_layout", "scene_reference",
              "depth_comparison_runtime", "body_angle_learning", "monocular_lean_learning", "stereo_calibration")
FILES = frozenset(USER_DATA_FILES[key] for key in (*PORTABLE_KEYS, *LOCAL_KEYS))
PORTABLE = frozenset(USER_DATA_FILES[key] for key in PORTABLE_KEYS)
WITH_BACKUP = frozenset(USER_DATA_FILES[key] for key in
                        ("profile_selection", "general_settings", "head_profile", "key_macros", "custom_poses", "depth_comparison_runtime"))
PORTABLE_PREFERENCES = frozenset({"output_speed", "output_mode", "pose_model", "march_algorithm",
    "pose_capture_delay_s", "hand_mouse", "vertical_look", "action_chain", "sensitivity_experiments",
    "zone_trigger_mode", "zone_following_layout", "depth_comparison"})


def _encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def _identity(root):
    try:
        data = json.loads((Path(root) / ".motioncontrol-data.json").read_bytes())
        return str(data.get("id") or "") if data.get("schema") == 1 else ""
    except (OSError, ValueError, AttributeError):
        return ""


def _allowed(name):
    if name in FILES or name.removesuffix(".last-good") in WITH_BACKUP:
        return True
    path = PurePosixPath(name)
    return (len(path.parts) == 2 and path.parts[0] == "intent_recordings" and
            (path.name == "index.json" or (path.name.startswith("intent-") and path.suffix == ".jsonl"))
            and path.as_posix() == name and "\\" not in name and ":" not in name)


def _target(root, name):
    path = (Path(root) / name).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError("配置路径超出用户数据目录，已停止操作")
    return path


def _names(root):
    names = set(FILES)
    names.update(name + ".last-good" for name in WITH_BACKUP)
    folder = Path(root) / "intent_recordings"
    if folder.exists():
        names.update(p.relative_to(root).as_posix() for p in folder.iterdir() if p.is_file() and _allowed(p.relative_to(root).as_posix()))
    return sorted(names)


def _validate(name, raw):
    name = name.removesuffix(".last-good")
    if name == USER_DATA_FILES["scene_reference"]:
        if not raw.startswith(b"\xff\xd8"):
            raise ValueError("参考场景图片无法读取")
        return
    if name.endswith(".jsonl"):
        from .intent_recording import SCHEMA as intent_schema
        lines = raw.decode("utf-8-sig").splitlines()
        if not lines or json.loads(lines[0]).get("schema") != intent_schema:
            raise ValueError("个人动作学习文件无法读取")
        if any(not isinstance(json.loads(line), dict) for line in lines if line.strip()):
            raise ValueError("个人动作学习帧无法读取")
        return
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("配置内容应为一组设置")
    if name == USER_DATA_FILES["profile_selection"]:
        from .game_profiles import GameProfileStore
        GameProfileStore.validate_selection(data)
    elif name == USER_DATA_FILES["key_macros"]:
        from .key_macros import MacroStore
        MacroStore._validate_document(data)
    elif name == USER_DATA_FILES["custom_poses"]:
        from .custom_poses import CustomPoseStore
        CustomPoseStore._validate_document(data)
    elif name == USER_DATA_FILES["head_profile"]:
        from .head_control import HeadController
        HeadController._validate_profile_document(data)
    elif name == USER_DATA_FILES["voice_mappings"]:
        from motioncontrol_shared.mapping_schema import normalize_voice_mappings
        normalize_voice_mappings(data.get("mappings", []))
    elif name == USER_DATA_FILES["personal_voice"]:
        from motioncontrol_shared.mapping_schema import normalize_wake_word, normalize_emergency_phrases
        normalize_wake_word(data.get("wake_word", ""))
        normalize_emergency_phrases(data.get("emergency_stop_phrases", []))
    elif name == USER_DATA_FILES["motion_mappings"]:
        from motioncontrol_shared.mapping_schema import normalize_motion_item
        items = data.get("motions", data.get("mappings", []))
        if not isinstance(items, list):
            raise ValueError("身体动作映射无法读取")
        for item in items:
            normalize_motion_item(item)
    elif name == USER_DATA_FILES["studio"]:
        from .studio import StudioService, DEFAULT_CONFIG
        checker = object.__new__(StudioService)
        checker.config = copy.deepcopy(DEFAULT_CONFIG)
        checker._validated(data)
    elif name == USER_DATA_FILES["fitness"]:
        from .fitness import FitnessStore, DEFAULT_PROFILE
        if data.get("schema") != 1 or not isinstance(data.get("sessions"), list):
            raise ValueError("运动记录格式无法读取")
        checker = object.__new__(FitnessStore)
        checker.profile = dict(DEFAULT_PROFILE)
        checker._profile(data.get("profile", {}))
        if any(not isinstance(session, dict) or not session.get("session_id") for session in data["sessions"]):
            raise ValueError("运动记录缺少活动编号")
    elif name == USER_DATA_FILES["custom_games"]:
        from .game_profiles import GameProfileStore
        from motioncontrol_shared.profile_versions import SELECTION_SCHEMA, CUSTOM_GAMES_SCHEMA
        if data.get("schema") != CUSTOM_GAMES_SCHEMA:
            raise ValueError("自建游戏目录格式无法读取")
        GameProfileStore.validate_selection({"schema": SELECTION_SCHEMA, "selected_id": "generic-xbox",
            "overrides_by_profile": {}, "custom_games": data.get("games")})
    elif name == USER_DATA_FILES["depth_comparison_runtime"]:
        from .model_resources import validate_registry
        validate_registry(data)
    elif name == USER_DATA_FILES["pose_actions"]:
        from .pose_downloads import SCHEMA as action_schema, verify
        if data.get("schema") != action_schema or not isinstance(data.get("actions", []), list):
            raise ValueError("下载动作库格式无效")
        for item in data.get("actions", []):
            verify(item["document"], item["signature"])
    elif name == "intent_recordings/index.json":
        from .intent_recording import INDEX_SCHEMA
        if data.get("schema") != INDEX_SCHEMA or not isinstance(data.get("items"), dict):
            raise ValueError("个人动作学习索引无效")
        for item in data["items"].values():
            if not isinstance(item, dict) or not _allowed("intent_recordings/" + str(item.get("file", ""))):
                raise ValueError("个人动作学习索引路径无效")


def _pack(files, *, source_id, version, rollback=False):
    manifest = {"schema": SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(),
                "version": version, "source_id": source_id, "rollback": rollback,
                "files": [{"name": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                          for name, raw in sorted(files.items())]}
    if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_TOTAL_BYTES:
        raise ValueError("配置与个人学习数据超过备份容量，请先整理旧学习记录")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST, _encode(manifest))
        for name, raw in files.items():
            archive.writestr(name, raw)
    result = out.getvalue()
    if len(result) > MAX_PACKAGE_BYTES:
        raise ValueError("配置包超过32兆，请先整理旧学习记录")
    return result


def capture_configuration(root, *, include_backups=False, documents=None):
    """调用方持配置锁时只读出原文，耗时的校验和压缩在释放识别锁后执行。"""
    root = Path(root)
    files = {}
    total = 0
    for name in _names(root):
        if not include_backups and name.endswith(".last-good"):
            continue
        path = _target(root, name)
        if path.is_file():
            if path.stat().st_size > MAX_PACKAGE_BYTES:
                raise ValueError("单个配置文件过大：" + name)
            raw = path.read_bytes()
            total += len(raw)
            if len(files) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                raise ValueError("配置与个人学习数据超过备份容量，请先整理旧学习记录")
            files[name] = raw
    for name, document in (documents or {}).items():
        if name not in FILES:
            raise ValueError("未知的配置文档：" + name)
        files[name] = _encode(document)
    return files


def export_configuration(root, *, version="", validate=True, snapshot=None):
    files = capture_configuration(root, include_backups=not validate) if snapshot is None else snapshot
    result = _pack(files, source_id=_identity(root), version=version, rollback=not validate)
    if validate:
        read_package(result)
    return result


def read_package(raw):
    if not raw or len(raw) > MAX_PACKAGE_BYTES:
        raise ValueError("请选择不超过32兆的 MotionControl 配置备份包")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if (len(entries) > MAX_FILES + 1 or len(set(names)) != len(names) or MANIFEST not in names or
                    sum(item.file_size for item in entries) > MAX_TOTAL_BYTES or
                    any(item.file_size > MAX_PACKAGE_BYTES or item.flag_bits & 1 for item in entries)):
                raise ValueError("备份文件数量、大小或内容重复，无法恢复")
            if any(name != MANIFEST and not _allowed(name) for name in names):
                raise ValueError("备份含有不属于配置的文件，无法恢复")
            manifest = json.loads(archive.read(MANIFEST))
            if (not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA or
                    not isinstance(manifest.get("files"), list)):
                raise ValueError("这个备份版本无法读取")
            records = manifest["files"]
            if (any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in records) or
                    len({item["name"] for item in records}) != len(records) or
                    set(names) != {MANIFEST, *(item["name"] for item in records)}):
                raise ValueError("备份清单与实际文件不一致")
            files = {}
            for item in records:
                name = item["name"]
                content = archive.read(name)
                if len(content) != item.get("bytes") or hashlib.sha256(content).hexdigest() != item.get("sha256"):
                    raise ValueError("备份内容不完整：" + name)
                try:
                    if not name.endswith(".last-good") or name.removesuffix(".last-good") not in names:
                        _validate(name, content)
                except Exception as error:
                    raise ValueError(name + " 未通过内容检查：" + str(error)) from error
                files[name] = content
            index = json.loads(files.get("intent_recordings/index.json", b'{"items":{}}'))
            if any("intent_recordings/" + item["file"] not in files for item in index.get("items", {}).values()):
                raise ValueError("备份缺少索引引用的个人动作学习文件")
            if "game_profile_selection.json" not in files:
                raise ValueError("备份缺少完整游戏配置，不能用空包替换当前设置")
            return manifest, files
    except (OSError, ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        raise ValueError("备份检查未通过：" + str(error)) from error


def _portable(files, root):
    restored = {name: raw for name, raw in files.items() if name in PORTABLE or name.startswith("intent_recordings/")}
    for name in ("general_settings.json", "head_profile.json"):
        if name not in files:
            continue
        try:
            current = json.loads((Path(root) / name).read_bytes())
        except (OSError, ValueError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        saved = json.loads(files[name])
        if name == "general_settings.json":
            # 只搬已知的通用偏好；本机相机、声音设备、站姿和固定区域留在原位置。
            current = {**current, **{key: value for key, value in saved.items() if key in PORTABLE_PREFERENCES}}
        else:
            current = {**current, **{key: saved[key] for key in ("signal_version", "params") if key in saved}}
        restored[name] = _encode(current)
    if "studio.json" in restored:
        studio = json.loads(restored["studio.json"])
        studio.update(avatar_data_url="", screen_crop={"x": 0, "y": 0, "width": 1, "height": 1})
        restored["studio.json"] = _encode(studio)
    if "fitness.json" in restored:
        fitness = json.loads(restored["fitness.json"])
        fitness["cloud"] = {"uploaded": {}, "cursor": "", "profile_pushed_ms": 0}
        restored["fitness.json"] = _encode(fitness)
    return restored


def preview_configuration(root, raw):
    manifest, files = read_package(raw)
    selection = json.loads(files.get("game_profile_selection.json", b"{}"))
    def count(name, key):
        return len(json.loads(files.get(name, b"{}" )).get(key, []))
    token = hashlib.sha256(raw).hexdigest()
    transaction.atomic_bytes(Path(root) / PREVIEW, raw)
    return {"token": token, "created_at": manifest.get("created_at", ""), "version": manifest.get("version", ""),
            "same_data": bool(_identity(root) and _identity(root) == manifest.get("source_id")),
            "games": len(selection.get("overrides_by_profile", {})), "custom_games": len(selection.get("custom_games", [])),
            "macros": count("key_macros.json", "macros"), "poses": count("custom_poses.json", "poses"),
            "files": [{"name": name, "bytes": len(content)} for name, content in sorted(files.items())]}


def restore_documents(root, raw, mode):
    _, files = read_package(raw)
    files = {name: content for name, content in files.items() if not name.endswith(".last-good")}
    if mode not in {"full", "portable"}:
        raise ValueError("请选择本机恢复或跨电脑恢复")
    root = Path(root)
    if mode == "portable":
        files = _portable(files, root)
    managed = set(FILES if mode == "full" else PORTABLE) | {name for name in _names(root) if name.startswith("intent_recordings/")}
    managed.update(files)
    # 完整配置中的自建游戏是权威来源，兼容副本必须与之相符。
    if "game_profile_selection.json" in files:
        selection = json.loads(files["game_profile_selection.json"])
        if "custom_games" in selection:
            from motioncontrol_shared.profile_versions import CUSTOM_GAMES_SCHEMA
            files["custom_games.json"] = _encode({"schema": CUSTOM_GAMES_SCHEMA, "games": selection["custom_games"]})
    documents = {_target(root, name): files.get(name) for name in managed}
    for name in managed & WITH_BACKUP:
        documents[_target(root, name + ".last-good")] = files.get(name)
    return documents


def stage_configuration(root, token, mode, *, version=""):
    root = Path(root)
    if (root / PENDING).exists():
        raise ValueError("已有待恢复配置，请先重新打开程序完成恢复")
    raw = (root / PREVIEW).read_bytes()
    if not isinstance(token, str) or hashlib.sha256(raw).hexdigest() != token:
        raise ValueError("恢复预览已过期，请重新选择备份文件")
    restore_documents(root, raw, mode)
    folder = root / "backups"
    rollback = folder / ("恢复前-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8] + ".zip")
    # 当前配置即便有损坏，也完整保留原文供回退，不用默认值代替。
    transaction.atomic_bytes(rollback, export_configuration(root, version=version, validate=False))
    pending = {"schema": 1, "token": token, "mode": mode, "rollback": rollback.relative_to(root).as_posix()}
    with transaction.file_transaction((root / PACKAGE, root / PENDING), root / ".configuration-stage.journal"):
        transaction.atomic_bytes(root / PACKAGE, raw)
        transaction.atomic_bytes(root / PENDING, _encode(pending))
    return {"pending": True, "rollback": str(rollback)}


def pending_documents(root):
    root = Path(root)
    # 准备事务未提交时，下次启动会回退该次准备，不应预告尚未确认的配置。
    if (root / ".configuration-stage.journal").exists():
        return None
    if (root / JOURNAL).exists():
        # 上次恢复可能已删除待办标记；从事务原文投影目标，启动核对不碰正在使用的文件。
        journal = json.loads((root / JOURNAL).read_bytes())
        if journal.get("schema") != 1 or not isinstance(journal.get("files"), list):
            raise ValueError("配置恢复记录无法读取，原记录已保留")
        previous = {item["path"]: item["content"] for item in journal["files"]}
        pending = json.loads(base64.b64decode(previous[PENDING], validate=True))
        raw = base64.b64decode(previous[PACKAGE], validate=True)
    else:
        if not (root / PENDING).exists():
            return None
        pending = json.loads((root / PENDING).read_bytes())
        raw = (root / PACKAGE).read_bytes()
    if pending.get("schema") != 1 or hashlib.sha256(raw).hexdigest() != pending.get("token"):
        raise ValueError("待恢复配置不完整，原配置及恢复前副本已保留")
    return restore_documents(root, raw, pending.get("mode"))


def apply_pending_configuration(root):
    root = Path(root)
    transaction.recover(root / ".configuration-stage.journal")
    transaction.recover(root / JOURNAL)
    documents = pending_documents(root)
    if documents is None:
        return False
    # 删除待恢复标记也进入同一事务，中断后整体回退，下次继续。
    documents.update({root / PENDING: None, root / PACKAGE: None})
    documents[root / PREVIEW] = None
    # 恢复替换了这些文档，不能让旧的中断写入记录在各对象初始化时覆盖新内容。
    for journal in (".general-settings.journal", ".head-profile.journal", ".key-macros.journal",
                    ".custom-poses.journal", ".profile-store.journal", ".profile-install.journal",
                    ".view-control.journal", ".voice-config-transaction.json", ".depth-comparison-runtime.journal"):
        documents[root / journal] = None
    with transaction.file_transaction(documents, root / JOURNAL):
        for path, raw in documents.items():
            if raw is None:
                path.unlink(missing_ok=True)
            else:
                transaction.atomic_bytes(path, raw)
    return True


def projected_selection(root, program_root):
    """启动检查读取将要恢复的游戏状态，不提前修改运行中使用的配置。"""
    documents = pending_documents(root)
    if documents is None:
        return None
    from .game_profiles import GameProfileStore
    with tempfile.TemporaryDirectory(prefix="motioncontrol-restore-check-") as folder:
        path = Path(folder) / "game_profile_selection.json"
        raw = documents.get(_target(root, path.name))
        if raw is not None:
            path.write_bytes(raw)
        store = GameProfileStore(program_root, selection_path=path)
        return store.revision(), store.selected_id()
