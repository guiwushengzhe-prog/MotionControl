"""录制目录：小的姿态点、大的画面和原始深度分开保存。"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

CATEGORIES = {"pose": "姿态点", "video": "视频", "depth": "深度"}
POSE_SCHEMAS = {"motioncontrol.pose_recording.v1", "motioncontrol.trigger_recording.v1"}
STEREO_SCHEMA = "motioncontrol.stereo_recording.v1"
MEDIA_FILES = {
    "video": ("kinect/color", "kinect/color_frames.jsonl"),
    "depth": ("kinect/depth.bin", "kinect/depth_frames.jsonl"),
}


def _label(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" ._")[:64] or "录制"


def _recorded_at(value=None) -> datetime:
    if isinstance(value, datetime):
        return value
    if value:
        return datetime.fromisoformat(str(value))
    return datetime.now().astimezone()


def source_name(source: str) -> str:
    if str(source).startswith(("mobile", "phone")):
        return "手机"
    if str(source).startswith(("computer", "local", "native", "pc")):
        return "电脑"
    return "骨骼来源"  # 不把会话编号或设备路径塞进名字。


def recording_path(root: Path, *, source="", label="骨骼", recorded_at=None,
                   category="pose", suffix=".jsonl") -> Path:
    """同一时间和名称可供姿态点、视频使用；此处不访问磁盘。"""
    at = _recorded_at(recorded_at)
    name = at.strftime("%H-%M-%S-%f") + "_" + source_name(source) + "_" + _label(label)
    return Path(root) / CATEGORIES[category] / at.strftime("%Y-%m-%d") / (name + suffix)


def available_path(path: Path) -> Path:
    """迁移和写盘时避开已有名字，绝不覆盖另一段录制。"""
    candidate, index = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        index += 1
    return candidate


def _header(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8-sig") as stream:
            value = json.loads(stream.readline(65536))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _pose_label(header: dict) -> str:
    if header.get("schema") == "motioncontrol.trigger_recording.v1":
        return "触发片段"
    duration = header.get("duration_s")
    return f"骨骼_{duration:g}秒" if isinstance(duration, (int, float)) else "骨骼"


def _inside(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("录制路径超出数据文件夹")
    return resolved


def _split_stereo_media(root: Path, session: Path) -> None:
    """配套索引和画面一起移动，骨骼文件的内容及相对关系不改。"""
    descriptor = session / "配套文件.json"
    if descriptor.exists():
        layout = json.loads(descriptor.read_text(encoding="utf-8"))
    else:
        layout = {"schema": "motioncontrol.recording_layout.v1", "files": {}}
        for category, names in MEDIA_FILES.items():
            destination = root / CATEGORIES[category] / session.parent.name / session.name
            for name in names:
                layout["files"][name] = os.path.relpath(destination / name, session).replace("\\", "/")
        # 先保存路径表；中途被关闭时，下次沿同一张表继续，所有原始文件仍然在磁盘上。
        descriptor.write_text(json.dumps(layout, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, relative in layout["files"].items():
        source = _inside(root, session / name)
        destination = _inside(root, session / relative)
        if not source.exists():
            continue
        if destination.exists():
            raise FileExistsError(f"配套录制已存在，未覆盖：{destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)


def recording_file(session: Path, name: str) -> Path:
    """读旧会话或整理后的会话；视频被用户清理后，此路径可以不存在。"""
    original = session / name
    if original.exists():
        return original
    descriptor = session / "配套文件.json"
    if descriptor.exists():
        layout = json.loads(descriptor.read_text(encoding="utf-8"))
        name = str(name).replace("\\", "/")
        for prefix, relative in layout.get("files", {}).items():
            if name == prefix or name.startswith(prefix + "/"):
                return (session / relative / name[len(prefix):].lstrip("/")).resolve()
    return original


def organize_recordings(root: Path) -> dict:
    """只整理已知格式；保持原始字节，不删除数据，不处理仍在录制的会话。"""
    root = Path(root).resolve()
    result = {"moved": [], "errors": []}
    if not root.is_dir():
        return result
    legacy = list(root.glob("*.jsonl")) + list((root / "triggered").glob("*.jsonl"))
    for source in legacy:
        header = _header(source)
        if header.get("schema") not in POSE_SCHEMAS or not header.get("recorded_at"):
            continue
        try:
            destination = available_path(recording_path(root, source=header.get("source", ""),
                                                       label=_pose_label(header), recorded_at=header["recorded_at"]))
            _inside(root, source)
            _inside(root, destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
            result["moved"].append({"from": str(source.relative_to(root)), "to": str(destination.relative_to(root))})
        except (OSError, ValueError) as exc:
            result["errors"].append(str(exc))
    sessions = []
    for source in root.glob("stereo-*"):
        try:
            header = json.loads((source / "manifest.json").read_text(encoding="utf-8-sig"))
            if not isinstance(header, dict) or header.get("schema") != STEREO_SCHEMA or header.get("state") != "done":
                continue
            at = _recorded_at(header["recorded_at"])
            name = at.strftime("%H-%M-%S-%f") + "_双目深度对照"
            day = at.strftime("%Y-%m-%d")
            destination = root / CATEGORIES["pose"] / day / name
            index = 2
            while any((root / category / day / destination.name).exists() for category in CATEGORIES.values()):
                destination = destination.with_name(f"{name}_{index}")
                index += 1
            _inside(root, source)
            _inside(root, destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
            sessions.append(destination)
            result["moved"].append({"from": str(source.relative_to(root)), "to": str(destination.relative_to(root))})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result["errors"].append(str(exc))
    # 整理过程被关闭后可以重试拆分，不重复移动已经完成的文件。
    for manifest in (root / CATEGORIES["pose"]).glob("*/*/manifest.json"):
        if manifest.parent in sessions:
            continue
        try:
            header = json.loads(manifest.read_text(encoding="utf-8-sig"))
            if isinstance(header, dict) and header.get("schema") == STEREO_SCHEMA:
                sessions.append(manifest.parent)
        except (OSError, ValueError, AttributeError):
            continue
    for session in sessions:
        try:
            _split_stereo_media(root, session)
        except (OSError, ValueError) as exc:
            result["errors"].append(str(exc))
    if result["moved"]:
        with (root / "整理记录.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": datetime.now().astimezone().isoformat(), **result}, ensure_ascii=False) + "\n")
        (root / "文件夹说明.txt").write_text(
            "姿态点：骨骼点和识别记录，占用较小。\n"
            "视频：录像或连续彩色照片，可单独清理，不影响姿态点文件。\n"
            "深度：原始深度画面，通常占用较大；不再分析深度时可单独清理。\n"
            "每类按日期归档；同一段录制使用相同时间和名字，配套文件.json 记录对应路径。\n"
            "清理视频或深度后，仍可回放骨骼；不能再重跑对应的图像识别或原始深度分析。\n"
            "整理记录.jsonl 保存旧名字到新名字的对应关系。\n", encoding="utf-8")
    return result


def recordings_summary(root: Path) -> dict:
    groups = {key: {"count": 0, "bytes": 0} for key in (*CATEGORIES, "other")}
    pose_clips, media_clips = set(), {"video": set(), "depth": set()}
    if root.is_dir():
        for path in root.rglob("*"):
            try:
                if not path.is_file():
                    continue
                relative = path.relative_to(root)
                category = next((key for key, name in CATEGORIES.items() if relative.parts[0] == name), "other")
                # 尚未整理的旧数据也能正确分出画面和深度。
                if category == "other" and relative.parts[0].startswith("stereo-"):
                    category = "video" if "color" in relative.parts or path.name == "color_frames.jsonl" else (
                        "depth" if path.name in {"depth.bin", "depth_frames.jsonl"} else "pose")
                elif category == "other" and path.suffix == ".jsonl" and path.name != "整理记录.jsonl":
                    category = "pose"
                groups[category]["bytes"] += path.stat().st_size
                if category == "pose":
                    session = next((p for p in path.parents if (p / "manifest.json").is_file() and p != root), None)
                    if session is not None:
                        pose_clips.add(str(session))
                    elif path.suffix == ".jsonl":
                        pose_clips.add(str(path))
                elif category in media_clips:
                    # 一个照片序列是一段视频，不把每张照片算成一次录制。
                    key = relative.parts[:3] if relative.parts[0] in CATEGORIES.values() else relative.parts[:1]
                    media_clips[category].add(key)
            except OSError:
                continue
    groups["pose"]["count"] = len(pose_clips)
    for key, clips in media_clips.items():
        groups[key]["count"] = len(clips)
    return {"count": len(pose_clips), "bytes": sum(group["bytes"] for group in groups.values()), **groups}
