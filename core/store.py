"""轻量 JSON 存储。

AstrBot 的插件卸载/更新会覆盖插件目录，所以数据必须放在 data/plugin_data 下。
写入采用「临时文件 + os.replace」，避免进程被杀时留下半截 JSON。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

try:  # 插件里复用宿主 logger；脱离 AstrBot 时退回标准库
    from astrbot.api import logger
except Exception:  # pragma: no cover
    logger = logging.getLogger("astrbot_plugin_menstrual_cycle")

from .models import UserProfile

try:  # AstrBot >= 4.9.2 提供官方路径工具
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover - 兼容早期版本
    get_astrbot_data_path = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_menstrual_cycle"
STORE_VERSION = 2


def default_data_dir() -> Path:
    if get_astrbot_data_path is not None:
        try:
            return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        except Exception:  # pragma: no cover
            logger.warning("无法解析 AstrBot 数据目录，回退到插件目录")
    return Path(__file__).resolve().parent.parent / "data"


class CycleStore:
    """按用户维度保存周期记录。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_data_dir() / "cycles.json"
        self._lock = asyncio.Lock()
        self._cache: dict[str, UserProfile] | None = None

    async def load(self) -> dict[str, UserProfile]:
        async with self._lock:
            if self._cache is None:
                self._cache = await asyncio.to_thread(self._read_sync)
            return self._cache

    async def get(self, user_key: str) -> UserProfile | None:
        data = await self.load()
        return data.get(user_key)

    async def all_profiles(self) -> list[UserProfile]:
        data = await self.load()
        return list(data.values())

    async def upsert(self, profile: UserProfile) -> None:
        async with self._lock:
            if self._cache is None:
                self._cache = await asyncio.to_thread(self._read_sync)
            profile.updated_at = datetime.now().isoformat(timespec="seconds")
            self._cache[profile.user_key] = profile
            await asyncio.to_thread(self._write_sync, self._cache)

    async def delete(self, user_key: str) -> bool:
        async with self._lock:
            if self._cache is None:
                self._cache = await asyncio.to_thread(self._read_sync)
            if user_key not in self._cache:
                return False
            self._cache.pop(user_key, None)
            await asyncio.to_thread(self._write_sync, self._cache)
            return True

    async def flush(self) -> None:
        async with self._lock:
            if self._cache is not None:
                await asyncio.to_thread(self._write_sync, self._cache)

    def _read_sync(self) -> dict[str, UserProfile]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("周期数据文件解析失败: %s", self.path)
            broken = self.path.with_suffix(f".broken.{int(datetime.now().timestamp())}.json")
            try:
                self.path.replace(broken)
                logger.warning("已备份损坏的数据文件到 %s", broken)
            except Exception:
                pass
            return {}
        users = raw.get("users") if isinstance(raw, dict) else None
        if not isinstance(users, dict):
            return {}
        result: dict[str, UserProfile] = {}
        for key, payload in users.items():
            try:
                result[str(key)] = UserProfile.from_dict(payload)
            except Exception:
                logger.exception("跳过损坏的用户记录: %s", key)
        return result

    def _write_sync(self, cache: dict[str, UserProfile]) -> None:
        payload: dict[str, Any] = {
            "version": STORE_VERSION,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "users": {key: profile.as_dict() for key, profile in cache.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".cycles-", suffix=".json", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except Exception:
                pass
            raise


__all__ = ["CycleStore", "PLUGIN_NAME", "default_data_dir"]
