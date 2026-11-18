"""统一的时间来源。

业务上的「今天」应该是使用者所在时区的今天，而不是服务器进程所在时区的今天：
AstrBot 多数部署在 Docker 里，容器默认 UTC，于是北京时间的 00:00–08:00
会被算成昨天——周期天数差一天、自报的疼痛记到前一天、提醒时段判错。

优先级：档案自己的 ``timezone`` → 实例级兜底（AstrBot 的 ``timezone`` 配置）
→ 系统本地。取不到时区数据（Windows 没装 tzdata）时静默回退，绝不抛异常。
"""

from __future__ import annotations

from datetime import date, datetime, tzinfo
from typing import Any

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - 极老的解释器
    ZoneInfo = None  # type: ignore[assignment]


def zone(name: str) -> tzinfo | None:
    """取一个时区。名字为空、库不可用或名字非法都返回 None。"""
    text = str(name or "").strip()
    if not text or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(text)
    except Exception:
        return None


def name_of(profile: Any = None, *, fallback: str = "") -> str:
    """档案自己的时区优先，没有就用实例兜底。"""
    own = str(getattr(profile, "timezone", "") or "").strip()
    return own or str(fallback or "").strip()


def today(profile: Any = None, *, fallback: str = "") -> date:
    """这个档案眼里的「今天」。"""
    found = zone(name_of(profile, fallback=fallback))
    if found is None:
        return date.today()
    return datetime.now(found).date()


def now(profile: Any = None, *, fallback: str = "") -> datetime:
    """这个档案眼里的「现在」。返回 naive datetime，与项目里其它比较保持一致。"""
    found = zone(name_of(profile, fallback=fallback))
    moment = datetime.now(found) if found is not None else datetime.now()
    return moment.replace(tzinfo=None)
