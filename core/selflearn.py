"""Self Learning 情绪联动（真实接口适配）。

依据 astrbot_plugin_self_learning v4.9.5 的实际实现：

- 取实例：`context.get_registered_star(name).star_cls`
- 情绪对象：`star_cls.affection_manager`（`services/state/affection_manager.py`）
- 读：`await affection_manager.get_current_mood(group_id) -> BotMood | None`
- 写（无公开 setter）：
    1) `await affection_manager._set_immediate_mood(group_id, MoodType, intensity, description, hours)`
    2) 等价手工：`db_manager.save_bot_mood(...)` + 刷新 `current_moods[group_id]`
- `MoodType` 共 10 值：happy/sad/excited/calm/angry/anxious/playful/serious/nostalgic/curious

对方没有对外情绪 API，所以这里全部靠反射 + 降级，任何一步失败都不会影响本插件对话。
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import json
import logging
import sys
import time
import typing
from typing import Any

try:  # 插件里复用宿主 logger；脱离 AstrBot 时退回标准库
    from astrbot.api import logger
except Exception:  # pragma: no cover
    logger = logging.getLogger("astrbot_plugin_menstrual_cycle")

PLUGIN_NAMES = (
    "astrbot_plugin_self_learning",
    "self_learning",
    "astrbot_plugin_selflearning",
)

NAME_HINTS = ("self_learning", "selflearning", "self-learning")

# 阶段 -> (MoodType 名, 基础强度, 描述)
MOOD_RECIPE: dict[str, tuple[str, float, str]] = {
    "menstrual": ("SAD", 0.72, "身上不舒服，提不起劲，不想解释太多"),
    "follicular": ("HAPPY", 0.62, "人轻松了，话变多，愿意分享小事"),
    "ovulation": ("PLAYFUL", 0.70, "状态在线，想被夸，愿意主动靠近"),
    "luteal": ("ANXIOUS", 0.60, "有点钝也有点脆，容易往坏处想"),
    "pms": ("ANGRY", 0.75, "一点就着，底下其实是想被哄"),
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _module_of(obj: Any) -> Any:
    """拿到对象定义所在的模块（优先 sys.modules，避免重复导入的副作用）。"""
    name = type(obj).__module__
    module = sys.modules.get(name)
    if module is not None:
        return module
    try:
        return importlib.import_module(name)
    except Exception:
        return None


class SelfLearningBridge:
    """把生理周期状态写成 Self Learning 的 BotMood。"""

    def __init__(self, plugin: Any, enabled: bool = True) -> None:
        self.plugin = plugin
        self.enabled = enabled
        self.last_result: dict[str, Any] = {"ok": False, "detail": "尚未同步", "at": 0.0}
        self._meta_cache: Any = None
        self._mood_type_cls: Any = None

    # ------------------------------------------------------------------
    # 定位
    # ------------------------------------------------------------------
    def _find_star(self) -> Any:
        if self._meta_cache is not None:
            return self._meta_cache
        context = self.plugin.context

        getter = getattr(context, "get_registered_star", None)
        if callable(getter):
            for name in PLUGIN_NAMES:
                try:
                    meta = getter(name)
                except Exception:
                    continue
                if self._usable(meta):
                    self._meta_cache = meta
                    logger.info("[menstrual] 已连接 Self Learning：%s", name)
                    return meta

        try:
            stars = context.get_all_stars() or []
        except Exception:
            return None
        for meta in stars:
            hay = " ".join(
                str(getattr(meta, field, "") or "")
                for field in ("name", "display_name", "root_dir_name", "module_path")
            ).lower()
            if any(hint in hay for hint in NAME_HINTS) and self._usable(meta):
                self._meta_cache = meta
                logger.info("[menstrual] 已连接 Self Learning：%s", hay.strip())
                return meta
        return None

    @staticmethod
    def _usable(meta: Any) -> bool:
        if meta is None:
            return False
        if getattr(meta, "activated", True) is False:
            return False
        return getattr(meta, "star_cls", None) is not None

    def affection_manager(self) -> Any:
        meta = self._find_star()
        if meta is None:
            return None
        instance = getattr(meta, "star_cls", None)
        return getattr(instance, "affection_manager", None)

    def _mood_type(self) -> Any:
        if self._mood_type_cls is not None:
            return self._mood_type_cls
        manager = self.affection_manager()
        if manager is None:
            return None
        module = _module_of(manager)
        if module is not None:
            cls = getattr(module, "MoodType", None)
            if cls is not None:
                self._mood_type_cls = cls
                return cls
        # 退一步：从方法注解反射
        method = getattr(manager, "_set_immediate_mood", None) or getattr(
            manager, "set_mood", None
        )
        if callable(method):
            with contextlib.suppress(Exception):
                hints = typing.get_type_hints(method)
                cls = hints.get("mood_type")
                if cls is not None:
                    self._mood_type_cls = cls
                    return cls
        return None

    def _group_id(self, fallback: str = "") -> str:
        return fallback or "default"

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    async def pull(self, group_id: str) -> dict[str, Any] | None:
        manager = self.affection_manager()
        if manager is None:
            return None
        getter = getattr(manager, "get_current_mood", None)
        if not callable(getter):
            return None
        try:
            mood = await getter(group_id)
        except Exception:
            logger.debug("[menstrual] 读取 Self Learning 情绪失败", exc_info=True)
            return None
        if mood is None:
            return None
        payload = {
            "mood_type": str(getattr(getattr(mood, "mood_type", None), "value", getattr(mood, "mood_type", ""))),
            "intensity": float(getattr(mood, "intensity", 0.0) or 0.0),
            "description": str(getattr(mood, "description", "") or ""),
        }
        with contextlib.suppress(Exception):
            payload["effective"] = float(mood.effective_intensity())
        return payload

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    async def push(
        self, snapshot: dict[str, Any], group_id: str = "", duration_hours: int = 24
    ) -> tuple[bool, str]:
        if not self.enabled:
            self._export(snapshot)
            return False, "联动已关闭（只写本地快照）"

        manager = self.affection_manager()
        if manager is None:
            self._export(snapshot)
            return False, "没找到 Self Learning 的 affection_manager，已写本地快照"

        mood_name, intensity, description = self._recipe(snapshot)
        gid = self._group_id(group_id)
        ok, detail = await self._write_mood(
            manager, gid, mood_name, intensity, description, duration_hours
        )
        self._export(snapshot)
        self.last_result = {"ok": ok, "detail": detail, "at": time.time()}
        return ok, detail

    def _recipe(self, snapshot: dict[str, Any]) -> tuple[str, float, str]:
        phase = str(snapshot.get("phase") or "")
        name, base, description = MOOD_RECIPE.get(
            phase, ("CALM", 0.55, "情绪平稳，没什么起伏")
        )
        stress = float(snapshot.get("stress") or 0.0)
        pain = float(snapshot.get("pain") or 0.0)
        arousal = float(snapshot.get("arousal") or 0.0)
        intensity = base + 0.18 * (stress - 0.4) + 0.12 * (pain - 0.4)
        if phase == "pms" and arousal < 0.45:
            name = "SAD"
            description = "闷闷的，容易想哭，说不清哪里不对"
        return name, round(_clamp(intensity, 0.25, 0.95), 2), description

    async def _write_mood(
        self,
        manager: Any,
        group_id: str,
        mood_name: str,
        intensity: float,
        description: str,
        duration_hours: int,
    ) -> tuple[bool, str]:
        enum_cls = self._mood_type()
        mood_value = None
        if enum_cls is not None:
            with contextlib.suppress(Exception):
                mood_value = enum_cls[mood_name]
            if mood_value is None:
                with contextlib.suppress(Exception):
                    mood_value = next(
                        (item for item in enum_cls if item.name.lower() == mood_name.lower()),
                        None,
                    )
        if mood_value is None:
            mood_value = mood_name.lower()

        # 路径 1：私有但完整的写入方法
        setter = getattr(manager, "_set_immediate_mood", None)
        if callable(setter):
            try:
                await setter(group_id, mood_value, intensity, description, duration_hours)
                return True, f"_set_immediate_mood → {mood_name}({intensity})"
            except Exception as exc:
                logger.debug("[menstrual] _set_immediate_mood 不可用: %r", exc)

        # 路径 2：手工等价（官方 /set_mood 的同款写法）
        ok, detail = await self._manual_write(
            manager, group_id, mood_value, mood_name, intensity, description, duration_hours
        )
        if ok:
            return True, detail
        return False, "没有可用的情绪写入接口"

    async def _manual_write(
        self,
        manager: Any,
        group_id: str,
        mood_value: Any,
        mood_name: str,
        intensity: float,
        description: str,
        duration_hours: int,
    ) -> tuple[bool, str]:
        module = _module_of(manager)
        bot_mood_cls = getattr(module, "BotMood", None) if module else None
        db_manager = getattr(manager, "db_manager", None)
        saver = getattr(db_manager, "save_bot_mood", None) if db_manager else None

        if bot_mood_cls is None or not callable(saver):
            return False, ""

        kwargs: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            params = inspect.signature(bot_mood_cls).parameters
            kwargs = {
                "mood_type": mood_value,
                "intensity": intensity,
                "description": description,
                "start_time": time.time(),
                "duration_hours": duration_hours,
            }
            if "half_life_minutes" in params:
                kwargs.setdefault("half_life_minutes", 0.0)
            if "fade_threshold" in params:
                kwargs.setdefault("fade_threshold", 0.0)
            if "secondary_moods" in params:
                kwargs.setdefault("secondary_moods", [])

        try:
            await saver(group_id, mood_value, intensity, description, duration_hours)
        except Exception as exc:
            logger.debug("[menstrual] save_bot_mood 失败: %r", exc)
            return False, ""

        with contextlib.suppress(Exception):
            mood_obj = bot_mood_cls(**kwargs)
            caches = getattr(manager, "current_moods", None)
            if isinstance(caches, dict):
                caches[group_id] = mood_obj
            return True, f"save_bot_mood + 缓存刷新 → {mood_name}({intensity})"
        return True, f"save_bot_mood → {mood_name}({intensity})（缓存未刷新）"

    # ------------------------------------------------------------------
    # 本地快照
    # ------------------------------------------------------------------
    def _export(self, snapshot: dict[str, Any]) -> None:
        try:
            path = self.plugin.data_dir / "emotion_snapshot.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"updated_at": time.time(), **snapshot}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:
            logger.debug("[menstrual] 导出情绪快照失败", exc_info=True)

    def status_text(self) -> str:
        if not self.enabled:
            return "联动关闭"
        manager = self.affection_manager()
        if manager is None:
            return "未检测到 Self Learning"
        return f"已连接 Self Learning（{type(manager).__name__}）"


__all__ = ["SelfLearningBridge", "MOOD_RECIPE"]
