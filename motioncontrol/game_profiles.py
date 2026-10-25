"""Desktop-side persistence for the selected game profile and its overrides.

The validation rules themselves live in ``motioncontrol_shared`` so the cloud
can apply exactly the same ones.  What stays here is everything that touches
this machine: reading and atomically rewriting
``game_profile_selection.json``, caching the catalog against its mtime, and
falling back to the generic profile when a generated library is missing.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import uuid
from pathlib import Path

from motioncontrol_shared.catalog import catalog_entry, load_catalog, load_profile
from motioncontrol.user_paths import user_path
from motioncontrol.config_transaction import atomic_bytes, recover, replace_documents
from motioncontrol_shared.motion_conflicts import validate_motion_bindings
from motioncontrol_shared.regions import (
    MAX_CUSTOM_REGIONS, is_region_id, normalize_region_name, normalize_region_rect, normalize_regions,
)
from motioncontrol_shared.profile_schema import (
    DEFAULT_MODE_NAME,
    MAX_GAME_MODES,
    _merge_bindings,
    is_mode_id,
    normalize_bindings,
    normalize_mode_name,
)
from motioncontrol_shared.profile_versions import (
    CATALOG_SCHEMA,
    CUSTOM_GAMES_SCHEMA,
    SELECTION_SCHEMA,
    is_selection_v1,
    migrate_selection_v1_to_v2,
)


def _clean_appid(value) -> str:
    """Steam AppID 只留数字，其余当没填。

    玩家会把整个商店链接粘进来，也会顺手打个空格。与其拒绝他，不如把数字捞
    出来——填错格式被打回去的人，多半就不填了。
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:12]


class ProfileSelectionChanged(ValueError):
    """The editor belongs to a different game than the active selection."""


class GameProfileStore:
    """Tiny lazy-loaded offline game-profile library.

    Only catalog.json is loaded eagerly. Individual profile JSON files are read
    when selected/opened, so hundreds or thousands of games do not inflate the
    runtime working set.
    """

    def __init__(self, root: Path, selection_path: Path | None = None) -> None:
        self.root = Path(root)
        self.library_dir = self.root / "game_profiles"
        self.profile_dir = self.library_dir / "profiles"
        self.catalog_path = self.library_dir / "catalog.json"
        # User data, so it lives outside the program folder: unzipping an
        # upgrade must not leave a player's per-game mappings behind.
        self.selection_path = (Path(selection_path) if selection_path is not None
                               else user_path("profile_selection"))
        # 玩家自己添加的游戏。和选择、覆盖一样是用户数据，不进程序目录。
        self.custom_games_path = self.selection_path.parent / user_path("custom_games").name
        self.backup_path = self.selection_path.with_name(self.selection_path.name + ".last-good")
        self.journal_path = self.selection_path.parent / ".profile-store.journal"
        self._lock = threading.RLock()
        self._catalog: dict | None = None
        self._catalog_mtime_ns = -1
        self.recovery_count = 0
        self.recovery_message = self.last_error = ""
        self._migrated = False
        self._revision_state = None
        recover(self.journal_path)
        self._selection = self._load_selection()
        self._custom = self._load_custom_games()
        self._disk_stamp = self._stamp()
        if self._migrated:
            self._save_selection(self._selection)
        elif self.selection_path.exists():
            atomic_bytes(self.backup_path, self._encoded_state(self._selection, self._custom))

    def _stamp(self):
        def stamp(path):
            try:
                stat = path.stat()
                return stat.st_mtime_ns, stat.st_size, stat.st_ino
            except FileNotFoundError:
                return None
        return tuple(stamp(p) for p in (self.selection_path, self.custom_games_path))

    def _load_selection(self) -> dict:
        try:
            original = self.selection_path.read_bytes()
            return self._parse_selection(original)
        except (FileNotFoundError, ValueError, UnicodeError) as error:
            if not self.backup_path.exists():
                if isinstance(error, FileNotFoundError):
                    return {"schema": SELECTION_SCHEMA, "selected_id": "generic-xbox", "overrides_by_profile": {}}
                raise ValueError("游戏配置无法读取，未找到有效副本；原文件已保留") from error
            raw = self.backup_path.read_bytes()
            restored = self._parse_selection(raw)
            if self.selection_path.exists():
                damaged = self.selection_path.read_bytes()
                digest = hashlib.sha256(damaged).hexdigest()[:16]
                preserved = self.selection_path.with_name(self.selection_path.name + f".invalid-{digest}")
                if not preserved.exists():
                    atomic_bytes(preserved, damaged)
            atomic_bytes(self.selection_path, raw)
            self.recovery_count += 1
            self.recovery_message = "已从最近有效副本恢复游戏配置，损坏内容已保留"
            return restored

    def _parse_selection(self, original: bytes) -> dict:
        data = json.loads(original.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("游戏配置格式无法读取，原文件已保留")
        if is_selection_v1(data):
            migrated = migrate_selection_v1_to_v2(data)
            backup = self.selection_path.with_name(self.selection_path.name + ".v1.bak")
            try:
                with backup.open("xb") as stream:
                    stream.write(original)
            except FileExistsError:
                pass
            data = migrated
            self._migrated = True
        return self.validate_selection(data)

    @classmethod
    def validate_selection(cls, data: dict) -> dict:
        """读取、恢复预览共用校验；不访问或修改用户文件。"""
        if not isinstance(data, dict):
            raise ValueError("游戏配置格式无法读取，原文件已保留")
        if data.get("schema") != SELECTION_SCHEMA or not isinstance(data.get("overrides_by_profile"), dict):
            raise ValueError("游戏配置格式无法读取，原文件已保留")
        if not isinstance(data.get("selected_id"), str) or any(
            not isinstance(value, dict) for value in data["overrides_by_profile"].values()
        ):
            raise ValueError("游戏映射数据无效，原文件已保留")
        launch_modes = data.get("launch_mode_by_profile", {})
        if not isinstance(launch_modes, dict) or any(
            not isinstance(key, str) or not key or not isinstance(mode, str) or mode not in {"normal", "admin"}
            for key, mode in launch_modes.items()
        ):
            raise ValueError("游戏启动权限数据无效，原文件已保留")
        game_modes = data.get("modes_by_profile", {})
        if not isinstance(game_modes, dict) or any(
            not isinstance(items, list) or any(
                not isinstance(item, dict) or not is_mode_id(item.get("id")) or not isinstance(item.get("name"), str)
                or not isinstance(item.get("overrides", {}), dict) for item in items)
            for items in game_modes.values()
        ):
            raise ValueError("游戏模式数据无效，原文件已保留")
        regions = data.get("regions_by_profile", {})
        if not isinstance(regions, dict) or any(not isinstance(key, str) or not key for key in regions):
            raise ValueError("游戏区域数据无效，原文件已保留")
        for items in regions.values():
            normalize_regions(items)
        for overrides in [*data["overrides_by_profile"].values(),
                          *(mode.get("overrides", {}) for modes in game_modes.values() for mode in modes)]:
            validate_motion_bindings(_merge_bindings({}, overrides))
        if "custom_games" in data:
            games = data["custom_games"]
            if not isinstance(games, list) or any(
                not isinstance(game, dict) or not isinstance(game.get("id"), str)
                or not game["id"].startswith(cls.CUSTOM_PREFIX)
                or not isinstance(game.get("name"), str) or not game["name"].strip() for game in games
            ):
                raise ValueError("自建游戏信息无效，原文件已保留")
        return data

    @staticmethod
    def _encoded_state(selection, custom):
        selection = copy.deepcopy(selection)
        selection["custom_games"] = copy.deepcopy(custom["games"])
        return json.dumps(selection, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")

    def _save_selection(self, selection: dict, custom: dict | None = None) -> None:
        if self._stamp() != self._disk_stamp:
            raise ProfileSelectionChanged("配置文件已更新，已阻止旧状态覆盖，请重新读取后重试")
        custom = copy.deepcopy(self._custom if custom is None else custom)
        encoded = self._encoded_state(selection, custom)
        selection = self._parse_selection(encoded)
        replace_documents({self.selection_path: selection, self.custom_games_path: custom,
                           self.backup_path: selection}, self.journal_path)
        self._selection, self._custom = selection, custom
        self._disk_stamp = self._stamp()
        self.last_error = ""

    def refresh_from_disk(self) -> bool:
        """只在文件变化时重读；失败保留已生效配置并阻止旧状态写回。"""
        with self._lock:
            try:
                if self._stamp() == self._disk_stamp:
                    return False
                selection = self._load_selection()
                custom = self._load_custom_games(selection)
                changed = selection != self._selection or custom != self._custom
                self._selection, self._custom = selection, custom
                self._disk_stamp = self._stamp()
                self.last_error = ""
                if changed:
                    self.recovery_count += 1
                    self.recovery_message = "已重新载入完整游戏配置"
                return changed
            except (OSError, ValueError, TypeError) as error:
                self.last_error = f"配置重载未完成，继续使用最近有效状态：{error}"
                return False

    def persistence_status(self):
        with self._lock:
            return {"state": "error" if self.last_error else "ready", "error": self.last_error,
                    "revision": self.revision(),
                    "recovery_count": self.recovery_count, "message": self.recovery_message,
                    "selection_path": str(self.selection_path.resolve()),
                    "custom_games_path": str(self.custom_games_path.resolve())}

    def configuration_document(self):
        """完整备份共用已加载的游戏身份和全部绑定。"""
        with self._lock:
            return json.loads(self._encoded_state(self._selection, self._custom))

    def revision(self):
        with self._lock:
            state = self._selection, self._custom
            # 保存与重读都整体替换状态；每次轮询不重复编码全部游戏的绑定。
            if self._revision_state is None or any(a is not b for a, b in zip(state, self._revision_state)):
                raw = json.dumps(state, sort_keys=True, ensure_ascii=False).encode("utf-8")
                self._revision_value = hashlib.sha256(raw).hexdigest()[:20]
                self._revision_state = state
            return self._revision_value

    # ---- 玩家自己添加的游戏 -------------------------------------------------
    #
    # 内置目录只有两百个。搜不到的游戏以前只能占用别人的坑位：界面上一直显示着
    # 错的游戏名，而且第二个未收录的游戏就没地方放了。
    #
    # 自定义条目只存「叫什么」和「以哪个内置档为底」。按键绑定仍然走原来那套
    # overrides_by_profile，按 id 存——覆盖机制本来就在，这里缺的只是让它有一
    # 个属于自己的 id 可用。

    CUSTOM_PREFIX = "custom-"

    def _load_custom_games(self, selection=None) -> dict:
        """完整游戏配置内的自建条目为准；兼容旧版的独立文件。"""
        selection = self._selection if selection is None else selection
        if isinstance(selection.get("custom_games"), list):
            data = {"games": selection["custom_games"]}
        else:
            try:
                data = json.loads(self.custom_games_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                return {"schema": CUSTOM_GAMES_SCHEMA, "games": []}
        if not isinstance(data, dict) or not isinstance(data.get("games"), list):
            return {"schema": CUSTOM_GAMES_SCHEMA, "games": []}
        games = []
        for raw in data["games"]:
            if not isinstance(raw, dict):
                continue
            ident = str(raw.get("id", "")).strip()
            name = str(raw.get("name", "")).strip()
            if not ident.startswith(self.CUSTOM_PREFIX) or not name:
                continue
            games.append({
                "id": ident,
                "name": name,
                "base": str(raw.get("base") or "generic-xbox"),
                "appid": _clean_appid(raw.get("appid")),
            })
        return {"schema": CUSTOM_GAMES_SCHEMA, "games": games}

    def _save_custom_games(self, data: dict) -> None:
        self._save_selection(self._selection, custom=data)

    def import_custom_game(self, record: dict) -> None:
        """Install the same custom game ID from a shared game configuration."""
        ident = str(record.get("id", "")).strip()
        name = str(record.get("name", "")).strip()
        base = str(record.get("base") or "generic-xbox").strip()
        if not ident.startswith(self.CUSTOM_PREFIX) or not name or len(name) > 80:
            raise ValueError("自建游戏信息无效")
        with self._lock:
            existing = self._custom_entry(ident)
            if existing is not None:
                return  # Keep any local rename of the same shared game ID.
            self.get_profile(base)
            item = {"id": ident, "name": name, "base": base,
                    "appid": _clean_appid(record.get("appid"))}
            self._save_custom_games({"schema": CUSTOM_GAMES_SCHEMA,
                                     "games": [*self._custom["games"], item]})

    def _custom_entry(self, profile_id: str) -> dict | None:
        ident = str(profile_id).strip()
        for item in self._custom["games"]:
            if item["id"] == ident:
                return item
        return None

    def add_custom_game(self, name: str, base: str = "generic-xbox",
                        appid: str | int | None = None) -> dict:
        """添加一个内置目录里没有的游戏。

        id 用随机后缀而不是从名字生成：两个人都叫「测试」时不该撞在一起，改名
        也不该让已经调好的按键跟着失效——绑定是按 id 存的。

        appid 选填，但它是这条记录唯一能跨机器对上号的东西。名字对不上：一个人
        写「黑神话悟空」，另一个写「黑神话：悟空」，第三个写英文名——靠名字合并
        就只能人工来。AppID 是 Steam 给的，唯一且查得到。

        所以现在就收下它，哪怕上传功能还没做：等做的时候再问，已经添加过游戏的
        人得回去把每一个都重填一遍。
        """
        label = str(name).strip()
        if not label:
            raise ValueError("游戏名不能为空")
        if len(label) > 80:
            raise ValueError("游戏名太长了")
        with self._lock:
            # 底档必须真实存在，否则选中的那一刻才会炸。
            self.get_profile(base) if not str(base).startswith(self.CUSTOM_PREFIX) else None
            if any(item["name"] == label for item in self._custom["games"]):
                raise ValueError(f"已经有一个叫「{label}」的游戏了")
            ident = self.CUSTOM_PREFIX + uuid.uuid4().hex[:10]
            record = {"id": ident, "name": label, "base": str(base),
                      "appid": _clean_appid(appid)}
            games = list(self._custom["games"])
            games.append(record)
            self._save_custom_games({"schema": CUSTOM_GAMES_SCHEMA, "games": games})
            return dict(record)

    def remove_custom_game(self, profile_id: str) -> dict:
        """删掉一个自定义游戏，连同它的按键覆盖。

        留着孤儿覆盖没有意义：那个 id 再也不会出现，而它会一直占着存储、并在
        导出配置时被一起带走。
        """
        ident = str(profile_id).strip()
        with self._lock:
            if self._custom_entry(ident) is None:
                raise KeyError(f"没有这个自定义游戏：{ident}")
            games = [x for x in self._custom["games"] if x["id"] != ident]
            selection = copy.deepcopy(self._selection)
            selection["overrides_by_profile"].pop(ident, None)
            selection.get("launch_mode_by_profile", {}).pop(ident, None)
            selection.get("modes_by_profile", {}).pop(ident, None)
            selection.get("regions_by_profile", {}).pop(ident, None)
            # 正选着它就退回通用档，否则界面会指向一个已经不存在的游戏。
            if selection["selected_id"] == ident:
                selection["selected_id"] = "generic-xbox"
            self._save_selection(selection, custom={"schema": CUSTOM_GAMES_SCHEMA, "games": games})
            return self.effective_profile()

    def rename_custom_game(self, profile_id: str, name: str) -> dict:
        """改名。按键是按 id 存的，所以改名不影响已经调好的绑定。"""
        label = str(name).strip()
        if not label:
            raise ValueError("游戏名不能为空")
        if len(label) > 80:
            raise ValueError("游戏名太长了")
        ident = str(profile_id).strip()
        with self._lock:
            if self._custom_entry(ident) is None:
                raise KeyError(f"没有这个自定义游戏：{ident}")
            if any(x["name"] == label and x["id"] != ident for x in self._custom["games"]):
                raise ValueError(f"已经有一个叫「{label}」的游戏了")
            games = [{**x, "name": label} if x["id"] == ident else x
                     for x in self._custom["games"]]
            self._save_custom_games({"schema": CUSTOM_GAMES_SCHEMA, "games": games})
            return {"id": ident, "name": label}

    def _load_catalog(self) -> dict:
        try:
            stat = self.catalog_path.stat()
            if self._catalog is not None and stat.st_mtime_ns == self._catalog_mtime_ns:
                return self._catalog
            self._catalog = load_catalog(self.library_dir)
            self._catalog_mtime_ns = stat.st_mtime_ns
            return self._catalog
        except Exception:
            self._catalog = {"schema": CATALOG_SCHEMA, "count": 0, "games": []}
            self._catalog_mtime_ns = -1
            return self._catalog

    def list_games(self, query: str = "") -> dict:
        with self._lock:
            catalog = self._load_catalog()
            # 自己添加的排在最前面：会去翻这个列表的人，多半就是为了找自己加的
            # 那个，而内置的两百个可以搜。
            mine = [{"id": item["id"], "name": item["name"],
                     "profile": "", "source": "custom", "verified": False,
                     "base": item["base"], "appid": item.get("appid")}
                    for item in self._custom["games"]]
            games = mine + list(catalog["games"])
            q = str(query).strip().casefold()
            if q:
                games = [g for g in games if q in g["name"].casefold() or q in str(g.get("appid", ""))]
            return {
                "schema": CATALOG_SCHEMA,
                "count": len(games),
                "library_count": catalog["count"] + len(mine),
                "custom_count": len(mine),
                "selected_id": self._selection["selected_id"],
                "games": copy.deepcopy(games),
            }

    def _catalog_entry(self, profile_id: str) -> dict:
        return catalog_entry(self._load_catalog(), profile_id)

    def get_profile(self, profile_id: str) -> dict:
        with self._lock:
            entry = self._custom_entry(profile_id)
            if entry is None:
                return load_profile(self.library_dir, profile_id, self._load_catalog())
            # 自定义游戏本身不存按键：它借用一个内置档当底，改的部分照旧走
            # overrides_by_profile。底档哪天不见了就退回通用档，不能让一个
            # 自定义条目把控制器卡死。
            try:
                profile = load_profile(self.library_dir, entry["base"], self._load_catalog())
            except Exception:
                profile = load_profile(self.library_dir, "generic-xbox", self._load_catalog())
            profile["id"] = entry["id"]
            profile["name"] = entry["name"]
            profile["source"] = "custom"
            profile["base"] = entry["base"]
            # AppID 也带出来。界面上要显示它，而且以后按 AppID 上传配置时，拿到
            # 的就是这一份——不带的话每个用到 profile 的地方都要再回头查一次条目。
            profile["appid"] = entry.get("appid", "")
            return profile

    def selected_id(self) -> str:
        with self._lock:
            return self._selection["selected_id"]

    def effective_profile(self, mode_id: str | None = None) -> dict:
        """当前游戏某个模式真正生效的那份绑定。mode_id 为空或已不存在时是默认模式。"""
        with self._lock:
            selected_id = self._selection["selected_id"]
            try:
                profile = self.get_profile(selected_id)
            except Exception:
                # A missing generated library must not brick the existing controller.
                profile = self.get_profile("generic-xbox")
                profile["requested_id"] = selected_id
                profile["unavailable"] = True
                selected_id = "generic-xbox"
            modes = self._modes_for(selected_id)
            mode = next((item for item in modes if item["id"] == mode_id), None) if mode_id else None
            overrides = (mode.get("overrides", {}) if mode
                         else self._selection["overrides_by_profile"].get(selected_id, {}))
            profile["bindings"] = _merge_bindings(profile.get("bindings", {}), overrides)
            profile["selected_id"] = selected_id
            profile["overrides"] = copy.deepcopy(overrides)
            profile["mode"] = {"id": mode["id"], "name": mode["name"]} if mode else {"id": None, "name": DEFAULT_MODE_NAME}
            profile["modes"] = [{"id": None, "name": DEFAULT_MODE_NAME},
                                *({"id": item["id"], "name": item["name"]} for item in modes)]
            profile["regions"] = copy.deepcopy(self._selection.get("regions_by_profile", {}).get(selected_id, []))
            # 与这份快照同时取版本，避免响应中的新版本配上旧绑定。
            profile["configuration_revision"] = self.revision()
            return profile

    # 独立框的名称和位置跟着游戏，各模式只保存它们的按键与触发点。
    def _check_region_profile(self, profile_id: str) -> str:
        if not isinstance(profile_id, str) or profile_id != self._selection["selected_id"]:
            raise ProfileSelectionChanged("当前游戏已改变，区域未保存。请重新选择游戏后重试")
        return profile_id

    def _regions_for(self, profile_id: str) -> list[dict]:
        return self._selection.get("regions_by_profile", {}).get(profile_id, [])

    def set_regions(self, profile_id: str, regions: list[dict]) -> None:
        """安装已校验的游戏区域；云端导入也使用同一份规则。"""
        normalized = normalize_regions(regions)
        with self._lock:
            self.get_profile(profile_id)
            selection = copy.deepcopy(self._selection)
            by_game = selection.setdefault("regions_by_profile", {})
            if normalized:
                by_game[profile_id] = normalized
            else:
                by_game.pop(profile_id, None)
            if not by_game:
                selection.pop("regions_by_profile", None)
            self._save_selection(selection)

    def add_region(self, profile_id: str, name=None, rect=None, *, mode=None, follow=None) -> dict:
        with self._lock:
            selected = self._check_region_profile(profile_id)
            items = copy.deepcopy(self._regions_for(selected))
            if len(items) >= MAX_CUSTOM_REGIONS:
                raise ValueError(f"每个游戏最多新增 {MAX_CUSTOM_REGIONS} 个区域")
            if name is None:
                names = {item["name"] for item in items}
                number = next(i for i in range(1, MAX_CUSTOM_REGIONS + 2) if f"自定义区域{i}" not in names)
                name = f"自定义区域{number}"
            if rect is None:
                offset = .035 * (len(items) % 5)
                rect = {"x1": .35 + offset, "x2": .55 + offset,
                        "y1": .35 + offset, "y2": .55 + offset}
            ident = "user_" + uuid.uuid4().hex[:8]
            while any(item["id"] == ident for item in items):
                ident = "user_" + uuid.uuid4().hex[:8]
            item = {"id": ident, "name": normalize_region_name(name), "rect": normalize_region_rect(rect)}
            if mode is not None:
                item["mode"] = mode
                if mode == "following":
                    item["follow"] = follow
                item = normalize_regions([item])[0]
            self.set_regions(selected, [*items, item])
            return copy.deepcopy(item)

    def update_region(self, profile_id: str, ident: str, name=None, rect=None, *, follow=None) -> dict:
        with self._lock:
            selected = self._check_region_profile(profile_id)
            items = copy.deepcopy(self._regions_for(selected))
            if not is_region_id(ident):
                raise ValueError("只能修改自定义区域，不能操作内置区域")
            item = next((item for item in items if item["id"] == ident), None)
            if item is None:
                raise KeyError("这个区域已经删掉了")
            if name is not None:
                item["name"] = normalize_region_name(name)
            if rect is not None:
                item["rect"] = normalize_region_rect(rect)
                if follow is not None and item.get("mode") == "following":
                    item["follow"] = follow
            self.set_regions(selected, items)
            return copy.deepcopy(item)

    def update_region_rects(self, profile_id: str, rects: dict) -> dict:
        with self._lock:
            selected = self._check_region_profile(profile_id)
            if not isinstance(rects, dict):
                raise ValueError("区域布局必须是矩形列表")
            items = copy.deepcopy(self._regions_for(selected))
            by_id = {item["id"]: item for item in items}
            for ident, rect in rects.items():
                if not is_region_id(ident):
                    raise ValueError("布局中只能包含自定义区域")
                if ident not in by_id:
                    raise KeyError("布局中有已经删掉的区域")
                by_id[ident]["rect"] = normalize_region_rect(rect)
            self.set_regions(selected, items)
            return self.effective_profile()

    def remove_region(self, profile_id: str, ident: str) -> dict:
        with self._lock:
            selected = self._check_region_profile(profile_id)
            if not is_region_id(ident):
                raise ValueError("只能删除自定义区域，不能操作内置区域")
            items = self._regions_for(selected)
            if not any(item["id"] == ident for item in items):
                raise KeyError("这个区域已经删掉了")
            selection = copy.deepcopy(self._selection)
            remaining = [item for item in items if item["id"] != ident]
            by_game = selection.get("regions_by_profile", {})
            if remaining:
                by_game[selected] = remaining
            else:
                by_game.pop(selected, None)
            if not by_game:
                selection.pop("regions_by_profile", None)
            trigger = f"zone.{ident}"

            def clean(overrides):
                result = copy.deepcopy(overrides)
                result.pop(trigger, None)
                for binding in result.values():
                    if isinstance(binding, dict) and (binding.get("alternate_when") == trigger or binding.get("reset_trigger") == trigger):
                        for key in ("alternate_mode", "alternate_when", "alternate_action", "reset_trigger", "extra_actions"):
                            binding.pop(key, None)
                return result

            overrides = selection["overrides_by_profile"]
            if selected in overrides:
                overrides[selected] = clean(overrides[selected])
            for mode in selection.get("modes_by_profile", {}).get(selected, []):
                mode["overrides"] = clean(mode.get("overrides", {}))
            self._save_selection(selection)
            return self.effective_profile()

    # ---- 模式：同一个游戏的几套按键 -----------------------------------------
    #
    # 默认模式就是 overrides_by_profile 里原来那份；别的模式存在 modes_by_profile，
    # 同样是叠在内置档上的覆盖（规则见 motioncontrol_shared.profile_schema）。
    # 这里只管当前选中的游戏；切到哪个模式是运行时的事，不写进文件。

    def _modes_for(self, profile_id: str) -> list[dict]:
        return self._selection.get("modes_by_profile", {}).get(profile_id, [])

    def modes(self) -> list[dict]:
        """当前游戏的模式，默认模式在最前（编号为 None），也是「下一个」轮换的顺序。"""
        with self._lock:
            return [{"id": None, "name": DEFAULT_MODE_NAME},
                    *({"id": item["id"], "name": item["name"]}
                      for item in self._modes_for(self._selection["selected_id"]))]

    def mode_bindings(self) -> list[dict]:
        """当前游戏每个模式生效的绑定（默认在前）。语音按这些口令的合集建语法。"""
        with self._lock:
            return [self.effective_profile(item["id"])["bindings"] for item in self.modes()]

    def _write_modes(self, profile_id: str, modes: list[dict]) -> None:
        selection = copy.deepcopy(self._selection)
        by_game = selection.setdefault("modes_by_profile", {})
        if modes:
            by_game[profile_id] = copy.deepcopy(modes)
        else:
            by_game.pop(profile_id, None)
        if not by_game:
            selection.pop("modes_by_profile", None)
        self._save_selection(selection)

    def _check_mode_name(self, name: str, modes: list[dict], *, except_id: str | None = None) -> str:
        name = normalize_mode_name(name)
        if name == DEFAULT_MODE_NAME or any(item["name"] == name and item["id"] != except_id for item in modes):
            raise ValueError(f"已经有一个叫「{name}」的模式了")
        return name

    def add_mode(self, name: str, copy_from: str | None = None) -> dict:
        """新建一个模式，按键从 copy_from（默认模式或另一个模式）复制过来，再去改。"""
        with self._lock:
            profile_id = self._selection["selected_id"]
            modes = list(self._modes_for(profile_id))
            if len(modes) >= MAX_GAME_MODES:
                raise ValueError(f"一个游戏除默认外最多 {MAX_GAME_MODES} 个模式")
            label = self._check_mode_name(name, modes)
            if copy_from:
                source = next((item for item in modes if item["id"] == copy_from), None)
                if source is None:
                    raise KeyError("要复制的模式已经不在了")
                overrides = source.get("overrides", {})
            else:
                overrides = self._selection["overrides_by_profile"].get(profile_id, {})
            ident = "mode-" + uuid.uuid4().hex[:8]
            modes.append({"id": ident, "name": label, "overrides": copy.deepcopy(overrides)})
            self._write_modes(profile_id, modes)
            return {"id": ident, "name": label}

    def rename_mode(self, mode_id: str, name: str) -> dict:
        with self._lock:
            profile_id = self._selection["selected_id"]
            modes = list(self._modes_for(profile_id))
            if not any(item["id"] == mode_id for item in modes):
                raise KeyError("这个模式已经不在了")
            label = self._check_mode_name(name, modes, except_id=mode_id)
            self._write_modes(profile_id, [{**item, "name": label} if item["id"] == mode_id else item
                                           for item in modes])
            return {"id": mode_id, "name": label}

    def remove_mode(self, mode_id: str) -> None:
        with self._lock:
            profile_id = self._selection["selected_id"]
            modes = list(self._modes_for(profile_id))
            if not any(item["id"] == mode_id for item in modes):
                raise KeyError("这个模式已经不在了")
            self._write_modes(profile_id, [item for item in modes if item["id"] != mode_id])

    def set_modes(self, profile_id: str, modes: list[dict], check=None) -> None:
        """装别人分享的配置时整组换上那个游戏的模式。调用方已按共用规则校验过；
        这里再按这个游戏的底档合一遍，``check`` 同 set_overrides。"""
        with self._lock:
            base = self.get_profile(profile_id).get("bindings", {})
            for item in modes:
                merged = _merge_bindings(base, item.get("overrides", {}))
                validate_motion_bindings(merged)
                if check is not None:
                    check(merged)
            self._write_modes(profile_id, modes)

    def select(self, profile_id: str) -> dict:
        with self._lock:
            profile = self.get_profile(profile_id)  # validate before persisting
            overrides = self._selection["overrides_by_profile"].get(profile_id, {})
            validate_motion_bindings(_merge_bindings(profile.get("bindings", {}), overrides))
            self._save_selection({**self._selection, "selected_id": str(profile_id)})
            return self.effective_profile()

    def requires_admin(self, profile_id: str) -> bool:
        with self._lock:
            return self._selection.get("launch_mode_by_profile", {}).get(profile_id) == "admin"

    def set_admin(self, profile_id: str, enabled: bool) -> None:
        with self._lock:
            self.get_profile(profile_id)  # 同按键映射一样，只接受存在的游戏。
            selection = copy.deepcopy(self._selection)
            modes = selection.setdefault("launch_mode_by_profile", {})
            wanted = "admin" if enabled else "normal"
            if modes.get(profile_id) == wanted:
                return
            modes[profile_id] = wanted
            self._save_selection(selection)

    def set_overrides(self, overrides: dict, profile_id: str | None = None, check=None,
                      mode_id: str | None = None) -> dict:
        """``check`` sees the merged bindings before anything is saved and may
        raise to refuse them -- the voice service uses it so a game's phrase
        cannot take one that is already spoken for.

        mode_id 为空存的是默认模式，否则存那个模式的按键。"""
        if not isinstance(overrides, dict):
            raise ValueError("overrides must be an object")
        # Validate by applying to the currently selected base profile before saving.
        with self._lock:
            if profile_id is not None and profile_id != self._selection["selected_id"]:
                raise ProfileSelectionChanged("当前游戏已改变，映射未保存。请重新选择游戏后重试")
            selected = self._selection["selected_id"]
            modes = self._modes_for(selected)
            if mode_id is not None and not any(item["id"] == mode_id for item in modes):
                raise ProfileSelectionChanged("这个模式已经删掉了，映射未保存")
            base = self.get_profile(selected)
            merged = _merge_bindings(base.get("bindings", {}), overrides)
            validate_motion_bindings(merged)
            if check is not None:
                check(merged)
            if mode_id is None:
                selection = copy.deepcopy(self._selection)
                selection["overrides_by_profile"][selected] = copy.deepcopy(overrides)
                self._save_selection(selection)
            else:
                self._write_modes(selected, [{**item, "overrides": copy.deepcopy(overrides)}
                                             if item["id"] == mode_id else item for item in modes])
            return self.effective_profile(mode_id)
