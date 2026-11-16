"""Group Chat Plus 情绪联动（第二适配器）。

依据 astrbot_plugin_group_chat_plus V1.2.3.hotfix.2 的实际实现：

- `utils/mood_tracker.py` `class MoodTracker(config)`，纯内存、无数据库
- 状态容器 `self.moods: dict[chat_id, {"mood": str, "intensity": float, "last_update": float}]`
- 读：`get_current_mood(chat_id) -> str`、`get_mood_description(chat_id) -> str`（**同步**方法）
- 写：`update_mood_from_context(chat_id, recent_messages)` 是从聊天文本里抽情绪，
  不适合直接指定，所以这里直写 `moods[chat_id]` 并刷新 `last_update`，让它自己的
  衰减逻辑接着工作。
- `MoodTracker` 实例挂在插件对象上，但属性名不公开；用 isinstance 在 `vars(plugin)`
  里探测，避免依赖具体属性名。

对方面试的对象是「一个会话」，所以 chat_id 用本插件记下的 umo。
"""

from __future__ import annotations

import importlib
import time
from typing import Any

try:  # 插件里复用宿主 logger；脱离 AstrBot 时退回标准库
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_menstrual_cycle")

PLUGIN_NAMES = (
    "astrbot_plugin_group_chat_plus",
    "group_chat_plus",
)
NAME_HINTS = ("group_chat_plus", "groupchatplus", "chat_plus")

# 阶段 -> (情绪词, 基础强度)
# 词要落在对方默认关键词表里（开心 / 难过 / 生气 / 平静 一类），否则它自己的衰减会立刻覆盖
MOOD_WORDS: dict[str, tuple[str, float]] = {
    "menstrual": ("难过", 0.70),
    "follicular": ("开心", 0.55),
    "ovulation": ("开心", 0.72),
    "luteal": ("平静", 0.45),
    "pms": ("生气", 0.70),
}


class ChatPlusBridge:
    """把周期情绪写进 group_chat_plus 的会话情绪。"""

    def __init__(self, plugin: Any, enabled: bool = True) -> None:
        self.plugin = plugin
        self.enabled = enabled
        self._plugin_cache: Any = None
        self._tracker_cache: Any = None
        self._tracker_cls: type | None = None

    # -- 探测 ---------------------------------------------------------
    def _context(self) -> Any:
        return getattr(self.plugin, "context", None)

    def _find_plugin(self) -> Any:
        if self._plugin_cache is not None:
            return self._plugin_cache
        context = self._context()
        if context is None:
            return None

        getter = getattr(context, "get_registered_star", None)
        if callable(getter):
            for name in PLUGIN_NAMES:
                try:
                    meta = getter(name)
                except Exception:
                    meta = None
                cls = getattr(meta, "star_cls", None) if meta else None
                if cls is not None:
                    self._plugin_cache = cls
                    return cls

        lister = getattr(context, "get_all_stars", None)
        if callable(lister):
            try:
                metas = lister()
            except Exception:
                metas = []
            for meta in metas or []:
                text = " ".join(
                    str(getattr(meta, key, "") or "")
                    for key in ("name", "display_name", "root_dir_name", "module_path")
                ).lower()
                cls = getattr(meta, "star_cls", None)
                if cls is None or getattr(meta, "activated", True) is False:
                    continue
                if any(hint in text for hint in NAME_HINTS):
                    self._plugin_cache = cls
                    return cls
        return None

    def _mood_tracker_cls(self) -> type | None:
        if self._tracker_cls is not None:
            return self._tracker_cls
        plugin = self._find_plugin()
        if plugin is None:
            return None
        module_name = getattr(plugin, "__module__", "") or ""
        package = module_name.rsplit(".", 1)[0] if "." in module_name else ""
        candidates = []
        if package:
            candidates.append(f"{package}.utils.mood_tracker")
            candidates.append(f"{package}.utils")
        candidates.append("astrbot_plugin_group_chat_plus.utils.mood_tracker")
        for path in candidates:
            try:
                module = importlib.import_module(path)
            except Exception:
                continue
            cls = getattr(module, "MoodTracker", None)
            if isinstance(cls, type):
                self._tracker_cls = cls
                return cls
        return None

    def tracker(self) -> Any:
        """拿到对方插件里那个真实的 MoodTracker 实例（不是新建的）。"""
        if self._tracker_cache is not None:
            return self._tracker_cache
        plugin = self._find_plugin()
        if plugin is None:
            return None
        cls = self._mood_tracker_cls()
        if cls is None:
            # 退一步：按鸭子类型找（有 moods 字典 + get_current_mood）
            for value in vars(plugin).values():
                if hasattr(value, "moods") and hasattr(value, "get_current_mood"):
                    self._tracker_cache = value
                    return value
            return None
        for value in vars(plugin).values():
            if isinstance(value, cls):
                self._tracker_cache = value
                return value
        return None

    # -- 读 -----------------------------------------------------------
    def pull(self, chat_id: str) -> dict[str, Any] | None:
        tracker = self.tracker()
        if tracker is None:
            return None
        try:
            mood = tracker.get_current_mood(chat_id)
        except Exception:
            return None
        entry = {}
        moods = getattr(tracker, "moods", None)
        if isinstance(moods, dict):
            entry = moods.get(chat_id) or {}
        return {
            "mood": mood,
            "intensity": float(entry.get("intensity") or 0.0),
            "description": self._describe(tracker, chat_id),
        }

    @staticmethod
    def _describe(tracker: Any, chat_id: str) -> str:
        getter = getattr(tracker, "get_mood_description", None)
        if callable(getter):
            try:
                return str(getter(chat_id))
            except Exception:
                return ""
        return ""

    # -- 写 -----------------------------------------------------------
    def _word_for(self, snapshot: dict[str, Any]) -> tuple[str, float]:
        phase = str(snapshot.get("phase") or "")
        word, base = MOOD_WORDS.get(phase, ("平静", 0.4))
        # 用情绪向量微调：越难受越低、越亢奋越高
        valence = float(snapshot.get("valence") or 0.0)
        intensity = base + valence * 0.15
        if phase == "pms" and float(snapshot.get("stress") or 0) > 0.6:
            word = "生气"
        if phase == "menstrual" and float(snapshot.get("stress") or 0) > 0.7:
            word = "难过"
        return word, max(0.2, min(0.9, intensity))

    def push(
        self, snapshot: dict[str, Any], chat_id: str
    ) -> tuple[bool, str]:
        if not self.enabled:
            return False, "未开启 chat_plus 联动"
        if not chat_id:
            return False, "没有会话标识，无法写入"
        tracker = self.tracker()
        if tracker is None:
            return False, "未检测到 group_chat_plus 的 MoodTracker"
        moods = getattr(tracker, "moods", None)
        if not isinstance(moods, dict):
            return False, "对方的 moods 结构不认识"

        word, intensity = self._word_for(snapshot)
        try:
            moods[chat_id] = {
                "mood": word,
                "intensity": round(intensity, 2),
                "last_update": time.time(),
            }
        except Exception as exc:  # pragma: no cover - 结构变化时兜底
            return False, f"写入失败：{exc}"
        logger.debug(
            "[menstrual] 已写入 chat_plus 情绪 %s=%s(%.2f)", chat_id, word, intensity
        )
        return True, f"{word}（{intensity:.2f}）"

    # -- 文案 ---------------------------------------------------------
    def status_text(self) -> str:
        if not self.enabled:
            return "chat_plus 联动关闭"
        if self.tracker() is None:
            return "未检测到 group_chat_plus"
        return "已连接 group_chat_plus"


__all__ = ["ChatPlusBridge", "MOOD_WORDS"]
