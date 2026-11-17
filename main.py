"""AstrBot 生理周期插件：把真实的月经周期映射成身体状态和情绪状态。

设计要点
- 周期长度、经期长度都从中位数推，用户设置只作兜底；
- 情绪按 28 天等效日插值，短周期长周期都能伸缩；
- 状态通过 on_llm_request 注入到临时内容（不改 system_prompt，前缀缓存照常命中），
  模型只负责"演"，不用知道数值；
- 可选把状态推给 Self Learning 一类情绪插件，联动走 duck typing 探测。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import random
import re
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import date, datetime, timedelta
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # 4.x
    from astrbot.api.provider import ProviderRequest
except Exception:  # pragma: no cover - 老版本没有该实体
    ProviderRequest = Any  # type: ignore[assignment,misc]

from .core import comfort as cm
from .core import text as tt
from .core.engine import compute_state, cycle_stats, predict_next_periods
from .core.models import (
    CONSTITUTIONS,
    PeriodRecord,
    PhaseState,
    UserProfile,
    clamp,
    parse_date,
)
from .core.chatplus import ChatPlusBridge
from .core.selflearn import SelfLearningBridge
from .core.store import CycleStore, default_data_dir
from .core.voice import VoiceGenerator
from .core.webapi import DashboardAPI

def _load_message_chain() -> Any:
    """跨版本拿 MessageChain。

    4.29 起挪到了 ``core.message.message_event_result``（``core/message`` 下
    不再有 ``message_chain.py``），4.9~4.28 在 ``core.message.message_chain``，
    更早的版本还有 ``api.message_chain`` 这个转发层。依次探测，拿不到就返回
    None——主动提醒会因此降级为"不发"，但插件其余功能不受影响。
    """
    for module_name in (
        "astrbot.core.message.message_event_result",  # 4.29+
        "astrbot.core.message.message_chain",  # 4.9 ~ 4.28
        "astrbot.api.message_chain",
        "astrbot.api.event",
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # pragma: no cover - 该版本没有这个模块
            continue
        chain_cls = getattr(module, "MessageChain", None)
        if chain_cls is not None:
            return chain_cls
    return None


MessageChain = _load_message_chain()


def _load_text_part() -> Any:
    """跨版本拿 TextPart。

    AstrBot 4.x 用 ``TextPart`` 承载"只给模型看、不落库"的临时内容
    （``ContentPart.mark_as_temp()``）。官方路径是 ``astrbot.api.message_components``，
    核心实现在 ``astrbot.core.agent.message``，更早的版本在 ``core.message.components``。
    拿不到就返回 None——这时状态卡不注入，但命令、面板、提醒都不受影响。
    """
    for module_name in (
        "astrbot.api.message_components",
        "astrbot.core.agent.message",
        "astrbot.core.message.components",
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # pragma: no cover - 该版本没有这个模块
            continue
        part_cls = getattr(module, "TextPart", None)
        if part_cls is not None:
            return part_cls
    return None


TextPart = _load_text_part()

PLUGIN_NAME = "astrbot_plugin_menstrual_cycle"
# 周期归谁：character = 她自己有一条，跟谁聊都是同一个身体；user = 每个聊天对象各一条
CHARACTER_SCOPE = "character"
USER_SCOPE = "user"
CHARACTER_KEY = "character:self"
COMMAND_WORDS = ("经期", "月经", "例假", "生理期", "period", "cycle")

SYNONYMS: dict[str, str] = {}
for _canon, _words in {
    "开始": ("开始", "来了", "第一天", "记", "start", "begin"),
    "结束": ("结束", "走了", "完了", "干净了", "end", "finish", "stop"),
    "状态": ("状态", "看看", "怎么样", "现在", "status", "now", "state"),
    "预测": ("预测", "下次", "什么时候", "预测下次", "forecast", "next", "predict"),
    "历史": ("历史", "记录", "列表", "history", "list", "log"),
    "设置": ("设置", "配置", "setting", "settings", "config", "set"),
    "提醒": ("提醒", "remind", "reminder"),
    "同步": ("同步", "sync", "push"),
    "疼痛": ("疼", "痛", "椎", "pain", "ouch", "疼死了"),
    "文案": ("文案", "说法", "wording", "rewrite"),
    "暂停": ("暂停", "停经", "观察", "pause"),
    "恢复": ("恢复", "继续", "resume", "unpause"),
    "删除": ("删除", "删", "delete", "del", "remove"),
    "重置": ("重置", "清空", "reset", "clear"),
    "帮助": ("帮助", "help", "?", "？", "说明", "菜单"),
}.items():
    for _word in _words:
        SYNONYMS[_word] = _canon

_SETTING_RE = re.compile(
    r"(周期长度|周期|cycle|经期长度|经期|period|疼痛|pain|情绪|emotion|mood|pms|提醒|reminder)"
    r"\s*[=:：]?\s*(开|关|on|off|[0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)

# 设置项 -> (字段名, 类型, 最小值, 最大值, 中文名)
SETTING_FIELDS: dict[str, tuple[str, str, float, float, str]] = {
    "周期长度": ("cycle_length", "int", 15, 60, "周期长度"),
    "周期": ("cycle_length", "int", 15, 60, "周期长度"),
    "cycle": ("cycle_length", "int", 15, 60, "周期长度"),
    "经期长度": ("period_length", "int", 1, 12, "经期长度"),
    "经期": ("period_length", "int", 1, 12, "经期长度"),
    "period": ("period_length", "int", 1, 12, "经期长度"),
    "疼痛": ("pain_sensitivity", "float", 0.2, 2.5, "疼痛敏感度"),
    "pain": ("pain_sensitivity", "float", 0.2, 2.5, "疼痛敏感度"),
    "情绪": ("emotion_gain", "float", 0.2, 2.5, "情绪幅度"),
    "emotion": ("emotion_gain", "float", 0.2, 2.5, "情绪幅度"),
    "mood": ("emotion_gain", "float", 0.2, 2.5, "情绪幅度"),
    "pms": ("pms_strength", "float", 0.0, 3.0, "PMS 强度"),
    "提醒": ("reminder", "bool", 0, 1, "主动提醒"),
    "reminder": ("reminder", "bool", 0, 1, "主动提醒"),
}


class MenstrualCyclePlugin(Star):
    version = "1.9.1"

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.data_dir = default_data_dir()
        self.store = CycleStore(self.data_dir / "cycles.json")
        self._bridge = SelfLearningBridge(
            self, enabled=bool(self._cfg("enable_selflearn_sync", True))
        )
        self._chatplus = ChatPlusBridge(
            self, enabled=bool(self._cfg("enable_chatplus_sync", True))
        )
        self._voice = self._new_voice()
        self._reminder_task: asyncio.Task[None] | None = None
        self._sent_path = self.data_dir / "reminders.json"
        self._sent: dict[str, str] = {}
        self._phase_sent: dict[str, str] = {}
        self._no_chain_warned = False
        # 注入耗时与档位统计（面板要看，只留最近 200 次）
        self._perf: dict[str, Any] = {
            "recent": [],
            "levels": {"brief": 0, "normal": 0, "full": 0},
            "count": 0,
        }

    # 三个联动模块都做成惰性 property：热重载或测试里绕过 __init__ 时不会 AttributeError
    @property
    def bridge(self) -> SelfLearningBridge:
        mod = self.__dict__.get("_bridge")
        if mod is None:
            mod = SelfLearningBridge(
                self, enabled=bool(self._cfg("enable_selflearn_sync", True))
            )
            self.__dict__["_bridge"] = mod
        return mod

    @property
    def chatplus(self) -> ChatPlusBridge:
        mod = self.__dict__.get("_chatplus")
        if mod is None:
            mod = ChatPlusBridge(
                self, enabled=bool(self._cfg("enable_chatplus_sync", True))
            )
            self.__dict__["_chatplus"] = mod
        return mod

    def _new_voice(self) -> VoiceGenerator:
        return VoiceGenerator(
            self,
            enabled=bool(self._cfg("enable_llm_voice", True)),
            timeout=float(self._cfg("voice_timeout", 20)),
            path=self.data_dir / "voice_cache.json",
            mode=str(self._cfg("voice_mode", "cycle")),
            live_interval=float(self._cfg("live_interval", 300)),
        )

    def _realtime(self, scope: str = "") -> bool:
        """实时模式是否覆盖这个场景。

        scope="injection" 时还要看 realtime_injection——每次对话都多调一次模型
        是很实在的开销，默认不开。
        """
        if not self.voice.enabled:
            return False
        if str(self._cfg("voice_mode", "cycle") or "cycle") != "realtime":
            return False
        if scope == "injection":
            return bool(self._cfg("realtime_injection", False))
        return True

    @property
    def voice(self) -> VoiceGenerator:
        """文案生成器；热重载或测试里绕过 __init__ 时按需补建。"""
        gen = self.__dict__.get("_voice")
        if gen is None:
            gen = self._new_voice()
            self.__dict__["_voice"] = gen
        return gen

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        await self.store.load()
        self._load_sent()
        if self._cfg("enable_reminder", True):
            self._reminder_task = asyncio.create_task(
                self._reminder_loop(), name="menstrual-reminder"
            )
        if self._cfg("enable_dashboard", True):
            try:
                registered = DashboardAPI(self).register()
                if not registered:
                    self._log.info(
                        "[menstrual] 当前 AstrBot 不支持插件 Web API，控制台只读不可用"
                    )
            except Exception as exc:  # 面板挂掉不该影响主流程
                self._log.warning("[menstrual] 控制台注册失败：%s", exc)
        self._log.info("[menstrual] 已启动，数据目录 %s", self.data_dir)

    async def terminate(self) -> None:
        if self._reminder_task is not None:
            self._reminder_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reminder_task
            self._reminder_task = None
        with contextlib.suppress(Exception):
            await self.store.flush()

    # ------------------------------------------------------------------
    # 命令入口（多个别名各挂一个薄壳，避免依赖 alias 参数）
    # ------------------------------------------------------------------
    @filter.command("经期")
    async def cmd_period(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        async for result in self._dispatch(event):
            yield result

    @filter.command("月经")
    async def cmd_menses(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        async for result in self._dispatch(event):
            yield result

    @filter.command("例假")
    async def cmd_alias_a(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        async for result in self._dispatch(event):
            yield result

    @filter.command("生理期")
    async def cmd_alias_b(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        async for result in self._dispatch(event):
            yield result

    @filter.command("period")
    async def cmd_alias_c(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        async for result in self._dispatch(event):
            yield result

    @filter.command("cycle")
    async def cmd_alias_d(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        async for result in self._dispatch(event):
            yield result

    # ------------------------------------------------------------------
    # LLM 钩子：把状态写进系统提示词
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def inject_cycle_state(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        if not self._cfg("enable_llm_injection", True):
            return
        started = time.perf_counter()
        try:
            profile = await self._profile_for(event, create=False)
            if profile is None:
                return
            # 先看这句里有没有「关心」，有了当场算进状态，本次回复就能体现
            await self._maybe_comfort(profile, event)
            state = compute_state(profile)
            if state is None:
                return
            # 这一轮模型自己写的文案：实时模式现写，但只等一个小预算。
            # 注入是主回复的前置步骤，不能为了文案把人晾在那十几秒。
            voice = None
            if self._realtime("injection"):
                budget = int(self._cfg("injection_wait_ms", 0) or 0)
                if budget > 0:
                    try:
                        voice = await asyncio.wait_for(
                            self.voice.live(
                                profile,
                                state,
                                hint=getattr(event, "message_str", "") or "",
                            ),
                            timeout=budget / 1000.0,
                        )
                    except (asyncio.TimeoutError, TimeoutError):
                        voice = None
                        self._log.info(
                            "[menstrual] 文案生成超过 %d ms，这一条先用现成的", budget
                        )
                        self.voice.schedule(profile, state)
                else:
                    # 0 = 一秒都不等：用上一次写好的，后台再补一份新的
                    self.voice.schedule(profile, state)
            if voice is None:
                voice = self.voice.cached(profile, state)
                if voice is None:
                    self.voice.schedule(profile, state)
            message = getattr(event, "message_str", "") or ""
            level = (
                tt.injection_level(message)
                if self._cfg("enable_injection_levels", True)
                else "full"
            )
            block = tt.build_injection(
                profile,
                state,
                max_chars=int(self._cfg("injection_max_chars", 900)),
                include_numbers=bool(self._cfg("injection_numbers", True)),
                voice=voice,
                detail=level,
                self_mention=bool(self._cfg("allow_self_mention", True)),
                history_days=int(self._cfg("injection_history_days", 2) or 0),
            )
            if not self._append_injection(req, block):
                return
            await self._remember_umo(profile, event)
            self._record_injection(level, started)
        except Exception:
            self._log.exception("[menstrual] 注入状态失败")

    def _append_injection(self, req: ProviderRequest, block: str) -> bool:
        """把状态卡挂到临时内容上，绝不动 system_prompt。

        系统提示词一动，前缀缓存整段作废——每轮都在变的状态卡就是最贵的写法。
        ``extra_user_content_parts`` 带进来的内容只给这一次请求看，标成临时之后
        也不会写回会话历史，缓存该命中还是命中。
        """
        parts = getattr(req, "extra_user_content_parts", None)
        if parts is None or TextPart is None:
            if not getattr(self, "_no_parts_warned", False):
                self.__dict__["_no_parts_warned"] = True
                self._log.warning(
                    "[menstrual] 这个 AstrBot 版本没有 extra_user_content_parts，"
                    "状态卡不注入；命令、面板、提醒照常"
                )
            return False
        try:
            part = TextPart(text=block)
            marker = getattr(part, "mark_as_temp", None)
            if callable(marker):
                part = marker()
            parts.append(part)
            return True
        except Exception:
            self._log.exception("[menstrual] 状态卡挂载失败")
            return False

    def _record_injection(self, level: str, started: float) -> None:
        """记一次注入的耗时与档位。面板用它回答「插件到底拖慢了多少」。"""
        perf = self.__dict__.get("_perf")
        if not isinstance(perf, dict):
            # 热重载、或者测试里绕过 __init__ 时按需补一个
            perf = {
                "recent": [],
                "levels": {"brief": 0, "normal": 0, "full": 0},
                "count": 0,
            }
            self.__dict__["_perf"] = perf
        elapsed = (time.perf_counter() - started) * 1000.0
        perf["count"] = int(perf.get("count") or 0) + 1
        levels = perf.setdefault("levels", {})
        levels[level] = int(levels.get(level) or 0) + 1
        recent = perf.setdefault("recent", [])
        recent.append(round(elapsed, 2))
        if len(recent) > 200:
            del recent[: len(recent) - 200]

    async def _maybe_comfort(self, profile: UserProfile, event: AstrMessageEvent) -> None:
        """他递热水、抱一下、把事接过去——她当场就会松一点。

        只读这句话本身，不做意图推断；否定句（"不想喝热水"）会被词表挡掉。
        """
        if not self._cfg("enable_comfort_buff", True):
            return
        if not profile.periods:
            # 还没有任何周期记录的人不建"只有安慰"的空档案
            return
        text = (getattr(event, "message_str", "") or "").strip()
        if not text or text[:1] in {"/", "／", "!", "！"}:
            return
        try:
            entry = cm.register(
                profile,
                text,
                hours=float(self._cfg("comfort_hours", 6.0)),
            )
        except Exception:
            self._log.debug("[menstrual] 识别关心失败", exc_info=True)
            return
        if entry is None:
            return
        with contextlib.suppress(Exception):
            await self.store.upsert(profile)

    async def _remember_umo(self, profile: UserProfile, event: AstrMessageEvent) -> None:
        """把会话来源落盘，主动提醒要用。「经期」是命令触发，不一定会走 llm 钩子，两边都补。"""
        umo = event.unified_msg_origin or ""
        if umo:
            # 生成文案时要用它挑当前会话的模型
            self.__dict__["_last_umo"] = umo
        if self._scope() == CHARACTER_SCOPE:
            return  # 她自己的身体周期，不记聊天来源
        if not self._cfg("remember_session", True):
            return
        if not umo or profile.last_umo == umo:
            return
        profile.last_umo = umo
        if profile.periods:  # 还没建档的先留在内存，等第一次记录再落盘
            with contextlib.suppress(Exception):
                await self.store.upsert(profile)

    # ------------------------------------------------------------------
    # LLM 工具：让模型自己查/记
    # ------------------------------------------------------------------
    @filter.llm_tool(name="period_cycle")
    async def period_cycle_tool(
        self, event: AstrMessageEvent, action: str, value: str = ""
    ) -> str:
        """查询或记录用户的生理周期状态，用于回答"你现在怎么样""肚子疼不疼"这类问题，或在用户说"我来例假了""今天疼得厉害"时补记。

        Args:
            action(string): 动作，status=当前状态，forecast=未来预测，history=最近记录，log_start=记录经期开始，log_end=记录经期结束，log_pain=记录今天的疼痛程度
            value(string): log_start / log_end 使用的日期，格式 YYYY-MM-DD；log_pain 填 0-10 的疼痛程度；留空表示今天
        """
        act = (action or "").strip().lower()
        if not self._cfg("enable_llm_tool", True):
            return "这个工具被关掉了；直接用系统提示里的状态卡回答即可，不要声称记录了什么。"
        if (
            act.startswith("log")
            and self._admin_only()
            and not self._is_admin(event)
        ):
            return "记录和改动只有管理员能做；你可以直接回答，但不要声称已经写进她的档案。"
        profile = await self._profile_for(event, create=act.startswith("log"))
        if profile is None:
            return "这个用户还没有任何周期记录，先让他用「经期 开始」记一次。"

        if act in ("log_start", "start", "log"):
            day = parse_date(value) if value else date.today()
            if day is None:
                return f"日期「{value}」无法解析。"
            if not await self._record_start(profile, day):
                return f"{tt.pretty_date(day)} 已经记过了。"
            state = compute_state(profile)
            return state.summary_line() if state else "已记录。"

        if act in ("log_end", "end"):
            day = parse_date(value) if value else date.today()
            if day is None:
                return f"日期「{value}」无法解析。"
            ok, message = await self._record_end(profile, day, "")
            return message

        if act in ("log_pain", "pain"):
            raw = (value or "").strip().replace("分", "").replace("/10", "")
            try:
                level = float(raw)
            except ValueError:
                return "log_pain 需要一个 0-10 的数字。"
            if not 0 <= level <= 10:
                return "疼痛程度要落在 0-10 之间。"
            profile.pain_logs[date.today().isoformat()] = clamp(level / 10.0)
            profile.trim_pain_logs()
            await self.store.upsert(profile)
            state = compute_state(profile)
            if state is not None:
                profile.remember_phase_pain(state.phase.key, level / 10.0)
                await self.store.upsert(profile)
            return state.summary_line() if state else "已记录。"

        state = compute_state(profile)
        if act in ("forecast", "next", "predict"):
            return tt.render_forecast(profile)
        if act in ("history", "list"):
            return tt.render_history(profile)
        if state is None:
            return "还没有记录，无法推算状态。"
        voice = self.voice.cached(profile, state)
        if voice is None:
            self.voice.schedule(profile, state)
        return tt.render_status(profile, state, voice=voice)

    # ------------------------------------------------------------------
    # 谁能用：只认 AstrBot 配置里的管理员
    # ------------------------------------------------------------------
    def _admin_ids(self) -> set[str]:
        """AstrBot 全局 admins_id + 插件自己的 extra_admins。"""
        out: set[str] = set()
        try:
            cfg = self.context.get_config()
            for item in list(cfg.get("admins_id") or []):
                text = str(item).strip()
                if text:
                    out.add(text)
        except Exception:
            pass
        raw = str(self._cfg("extra_admins", "") or "")
        for part in raw.replace("，", ",").replace("；", ",").replace(";", ",").split(","):
            text = part.strip()
            if text:
                out.add(text)
        return out

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """平台侧管理员（event.role）或 AstrBot 配置里的管理员，都算。"""
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        role = str(getattr(event, "role", "") or "").strip().lower()
        if role in ("admin", "owner", "administrator", "管理员", "群主"):
            return True
        try:
            sender = str(event.get_sender_id() or "").strip()
        except Exception:
            sender = ""
        return bool(sender) and sender in self._admin_ids()

    def _admin_only(self) -> bool:
        return bool(self._cfg("enable_admin_only", True))

    # ------------------------------------------------------------------
    # 命令分发
    # ------------------------------------------------------------------
    async def _dispatch(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        if self._admin_only() and not self._is_admin(event):
            if self._cfg("admin_deny_reply", True):
                yield event.plain_result(tt.render_admin_denied())
            return

        tokens = self._tokens(event)
        canonical: str | None = None
        args: list[str] = []
        if tokens:
            canonical = SYNONYMS.get(tokens[0].lower())
            if canonical is not None:
                args = tokens[1:]
            else:
                # 首词不认识：当成自然语言，直接给状态
                canonical = "状态"
                args = tokens
        if canonical is None:
            canonical = "帮助"

        try:
            async for result in self._handle(canonical, args, event):
                yield result
        except Exception:
            self._log.exception("[menstrual] 处理命令失败")
            yield event.plain_result("刚刚算岔了，稍等再试一次。")

    async def _handle(
        self, action: str, args: list[str], event: AstrMessageEvent
    ) -> AsyncGenerator[Any, None]:
        today = date.today()

        if action == "帮助":
            yield event.plain_result(tt.render_help())
            return

        profile = await self._profile_for(
            event,
            create=action in ("开始", "设置", "提醒", "状态", "同步", "疼痛", "暂停", "恢复"),
        )
        if profile is not None:
            await self._remember_umo(profile, event)

        if action == "开始":
            if profile is None:
                yield event.plain_result("读不到你的档案，稍后重试一次。")
                return
            day = parse_date(args[0]) if args else today
            if day is None:
                yield event.plain_result(
                    f"没看懂「{args[0]}」这个日期，写 11-08 或 2026-11-08 都行。"
                )
                return
            if day > today:
                yield event.plain_result("未来的日期记不了，等她真的来了再说。")
                return
            if not await self._record_start(profile, day):
                state = compute_state(profile)
                body = tt.render_status(profile, state) if state else ""
                yield event.plain_result(
                    f"{tt.pretty_date(day)} 已经在记录里了，不重复记。\n\n{body}".strip()
                )
                return
            state = compute_state(profile)
            head = f"记下了：{tt.pretty_date(day)} 是第一天。"
            # 新周期 = 新的一套说法：先用内置文案回话，同时让模型写这一轮的
            self.voice.invalidate(profile.user_key)
            if state is not None:
                self.voice.schedule(profile, state)
            body = tt.render_status(profile, state, voice=self.voice.cached(profile, state)) if state else ""
            yield event.plain_result(f"{head}\n\n{body}".strip())
            await self._auto_sync(profile, state)
            return

        if action == "结束":
            if profile is None:
                yield event.plain_result("还没有正在进行的记录，先用「经期 开始」记第一天。")
                return
            day = parse_date(args[0]) if args else today
            note = " ".join(args[1:]) if len(args) > 1 else ""
            if day is None:
                yield event.plain_result(f"没看懂「{args[0]}」这个日期。")
                return
            ok, message = await self._record_end(profile, day, note)
            yield event.plain_result(message)
            if ok:
                state = compute_state(profile)
                await self._auto_sync(profile, state)
            return

        if action == "暂停":
            if profile is None:
                profile = await self._profile_for(event, create=True)
            if profile is None:
                yield event.plain_result("读不到你的档案，稍后重试一次。")
                return
            days = 0
            for token in args:
                if token.isdigit():
                    days = max(1, min(365, int(token)))
                    break
            if days == 0:
                yield event.plain_result(
                    "用「经期 暂停 30」进入观察期：这 30 天不做精确推算，也不再定时提醒。\n"
                    "出差、用药、怀孕、周期乱到没法算，都可以这么处理。想恢复用「经期 恢复」。"
                )
                return
            until = today + timedelta(days=days)
            profile.settings.paused_until = until.isoformat()
            await self.store.upsert(profile)
            yield event.plain_result(
                f"好，接下来 {days} 天按观察期走（到 {tt.pretty_date(until)}）。"
                "这段时间我只按状态说话，不报天数。"
            )
            return

        if action == "恢复":
            if profile is None:
                yield event.plain_result("还没有你的档案。")
                return
            if not profile.paused(today):
                yield event.plain_result("现在不在观察期，不用恢复。")
                return
            profile.settings.paused_until = ""
            await self.store.upsert(profile)
            state = compute_state(profile)
            body = tt.render_status(profile, state) if state else ""
            yield event.plain_result(f"恢复推算。\n\n{body}".strip())
            return

        if action == "疼痛":
            if profile is None:
                yield event.plain_result("先「经期 开始」记一次，我才有上下文。")
                return
            key = today.isoformat()
            if not args:
                current = profile.pain_logs.get(key)
                head = (
                    f"今天记的是 {int(round(current * 10))}/10。"
                    if current is not None
                    else "今天还没记。"
                )
                yield event.plain_result(
                    f"{head}用「经期 疼 7」说个 0-10 的数，10 是疼到动不了。"
                )
                return
            raw = args[0].replace("分", "").replace("/10", "").strip()
            try:
                level = float(raw)
            except ValueError:
                yield event.plain_result("给个 0-10 之间的数字，比如「经期 疼 7」。")
                return
            if level < 0 or level > 10:
                yield event.plain_result("0 到 10 之间，10 是疼到动不了。")
                return
            profile.pain_logs[key] = clamp(level / 10.0)
            profile.trim_pain_logs()
            await self.store.upsert(profile)
            state = compute_state(profile)
            if state is not None:
                profile.remember_phase_pain(state.phase.key, level / 10.0)
                await self.store.upsert(profile)
            body = tt.render_status(profile, state) if state else ""
            yield event.plain_result(f"记下了：今天疼 {level:g}/10。\n\n{body}".strip())
            await self._auto_sync(profile, state)
            return

        if profile is None or not profile.periods:
            yield event.plain_result(
                "还没有记录。先用「经期 开始」记第一天，我才能推算阶段和情绪。"
            )
            return

        if action == "状态":
            state = compute_state(profile)
            if state is None:
                yield event.plain_result("记录有问题，先「经期 开始」补一条。")
                return
            if args and args[0].lower() in ("简", "短", "brief", "quick", "one"):
                yield event.plain_result(tt.render_brief(profile, state))
                return
            if self._realtime():
                # 查一次等三秒太蠢：先用上一次现写好的，没有就后台补一份新的
                voice = self.voice.live_cached(profile)
                fresh = False
                if voice is None:
                    budget = int(self._cfg("injection_wait_ms", 0) or 0)
                    if budget > 0:
                        try:
                            voice = await asyncio.wait_for(
                                self.voice.live(profile, state),
                                timeout=budget / 1000.0,
                            )
                        except (asyncio.TimeoutError, TimeoutError):
                            voice = None
                    if voice is None:
                        voice = self.voice.cached(profile, state)
                        fresh = True
                        self.voice.schedule(profile, state)
            else:
                voice = self.voice.cached(profile, state)
                fresh = voice is None
                if fresh:
                    self.voice.schedule(profile, state)
            text = tt.render_status(profile, state, voice=voice)
            if fresh and self.voice.enabled:
                text = f"{text}\n\n✍️ 这一轮的文案还在写，下次查就是新的（想马上看到用「经期 文案 写」）"
            links = []
            if self._cfg("enable_selflearn_sync", True):
                links.append(self.bridge.status_text())
            if self._cfg("enable_chatplus_sync", True):
                links.append(self.chatplus.status_text())
            if links:
                text = f"{text}\n\n🔗 {' ｜ '.join(links)}"
            yield event.plain_result(text)
            await self._auto_sync(profile, state)
            return

        if action == "文案":
            state = compute_state(profile)
            if state is None:
                yield event.plain_result("记录有问题，先「经期 开始」补一条。")
                return
            head = args[0].lower() if args else ""
            if head in ("写", "刷新", "生成", "重写", "refresh", "gen"):
                if self._realtime():
                    pack = await self.voice.live(profile, state, force=True)
                else:
                    pack = await self.voice.ensure(profile, state, wait=True)
                if pack is None:
                    reason = self.voice.last_error or "模型没给可用的格式"
                    yield event.plain_result(
                        f"这次没写成：{reason}。先按内置文案走，不影响使用。"
                    )
                    return
                note = ""
                if pack.coverage() < 1.0:
                    note = f"\n（覆盖 {int(pack.coverage() * 100)}%，缺的几段继续用内置的）"
                yield event.plain_result(
                    f"写好了，这一轮按新的说。\n\n{tt.render_status(profile, state, voice=pack)}{note}"
                )
                return
            if head in ("清", "重置", "clear", "reset"):
                self.voice.invalidate(profile.user_key)
                yield event.plain_result("这一轮的文案清掉了，下次注入时会重新写。")
                return
            yield event.plain_result(
                f"{self.voice.status_text(profile, state)}\n\n"
                "「经期 文案 写」立刻重写这一轮（要等十几秒）\n"
                "「经期 文案 清」丢掉这一轮，下次重新生成"
            )
            return

        if action == "预测":
            yield event.plain_result(tt.render_forecast(profile))
            return

        if action == "历史":
            limit = 6
            if args and args[0].isdigit():
                limit = max(1, min(30, int(args[0])))
            yield event.plain_result(tt.render_history(profile, limit=limit))
            return

        if action == "设置":
            changes, errors = self._apply_settings(profile, " ".join(args))
            if changes:
                await self.store.upsert(profile)
            lines = [f"· {item}" for item in changes]
            lines.extend(f"⚠️ {item}" for item in errors)
            if not lines:
                yield event.plain_result(
                    "这么改：`经期 设置 周期 29 经期 5 疼痛 1.2 情绪 1.1 pms 1.3 提醒 开`"
                )
                return
            yield event.plain_result("⚙️ 已更新\n" + "\n".join(lines)
                                     + "\n\n" + tt.render_settings(profile))
            return

        if action == "提醒":
            value = (args[0].lower() if args else "")
            if value in ("口吻", "风格", "style", "voice"):
                profile.settings.reminder_style = (
                    "reminder"
                    if profile.settings.reminder_style == "in_character"
                    else "in_character"
                )
                await self.store.upsert(profile)
                label = (
                    "她的口吻"
                    if profile.settings.reminder_style == "in_character"
                    else "系统提醒"
                )
                yield event.plain_result(f"提醒改成{label}了。")
                return
            if value in ("开", "on", "true", "1"):
                profile.settings.reminder = True
            elif value in ("关", "off", "false", "0"):
                profile.settings.reminder = False
            else:
                yield event.plain_result(
                    f"主动提醒现在是「{'开' if profile.settings.reminder else '关'}」，"
                    "用「经期 提醒 开/关」切换。"
                )
                return
            await self.store.upsert(profile)
            yield event.plain_result(
                f"主动提醒已{'开启' if profile.settings.reminder else '关闭'}。"
            )
            return

        if action == "同步":
            state = compute_state(profile)
            if state is None:
                yield event.plain_result("没有可同步的状态。")
                return
            snapshot = tt.selflearn_snapshot(profile, state)
            chat_id = profile.last_umo or await self._user_key(event)
            ok, detail = await self.bridge.push(
                snapshot,
                group_id=chat_id,
                duration_hours=int(self._cfg("mood_duration_hours", 24)),
            )
            lines = [tt.render_sync_result(snapshot, ok, detail)]
            if self._cfg("enable_chatplus_sync", True):
                cp_ok, cp_detail = self.chatplus.push(snapshot, chat_id)
                lines.append(f"群情绪：{'✅ ' if cp_ok else '· '}{cp_detail}")
            yield event.plain_result("\n".join(lines))
            return

        if action == "删除":
            if not args:
                yield event.plain_result("用「经期 删除 2」删第 2 条，序号看「经期 历史」。")
                return
            ok, message = await self._delete_record(profile, args[0])
            yield event.plain_result(message)
            return

        if action == "重置":
            removed = await self.store.delete(profile.user_key)
            yield event.plain_result(
                "已清空你的全部周期数据。" if removed else "本来就没有数据。"
            )
            return

        yield event.plain_result(tt.render_help())

    # ------------------------------------------------------------------
    # 业务动作
    # ------------------------------------------------------------------
    async def _record_start(self, profile: UserProfile, day: date) -> bool:
        """记一次经期开始；返回 False 表示这天已经在记录里，没有新增。"""
        periods = profile.sorted_periods()
        for record in periods:
            if record.end is None:
                if record.start == day:
                    return False
                # 旧记录还没收尾，说明是她忘了记结束，不能拿它挡住新的开始日
                continue
            if record.start <= day <= record.end:
                return False
        # 有未结束的旧记录且这次更晚：把旧的收尾到前一天
        open_records = [r for r in periods if r.end is None and r.start < day]
        if open_records:
            tail = open_records[-1]
            tail.end = max(day - timedelta(days=1), tail.start)
        profile.periods.append(
            PeriodRecord(id=uuid.uuid4().hex[:8], start=day, end=None, note="")
        )
        await self.store.upsert(profile)
        return True

    async def _record_end(
        self, profile: UserProfile, day: date, note: str
    ) -> tuple[bool, str]:
        open_records = [r for r in profile.sorted_periods() if r.end is None]
        if not open_records:
            return False, "没有正在进行的记录，先用「经期 开始」记第一天。"
        record = open_records[-1]
        if day < record.start:
            return False, (
                f"结束日不能早于开始日（{tt.pretty_date(record.start)}）。"
            )
        if day > date.today():
            return False, "结束日不能是未来。"
        record.end = day
        if note:
            record.note = note
        await self.store.upsert(profile)
        length = record.length or 0
        lines = [
            f"记下了：{tt.pretty_date(record.start)} — {tt.pretty_date(day)}，共 {length} 天。"
        ]
        stats = cycle_stats(profile)
        if stats["median_cycle"]:
            next_start = predict_next_periods(profile, 1)
            tail = f"，下次大概 {tt.pretty_date(next_start[0][0])}" if next_start else ""
            lines.append(
                f"现在的中位周期是 {stats['median_cycle']} 天"
                f"（{stats['records']} 次记录）{tail}。"
            )
        return True, "\n".join(lines)

    async def _delete_record(
        self, profile: UserProfile, token: str
    ) -> tuple[bool, str]:
        ordered = profile.sorted_periods()
        target: PeriodRecord | None = None
        if token.isdigit():
            index = int(token)
            if 1 <= index <= len(ordered):
                target = ordered[index - 1]
        else:
            day = parse_date(token)
            if day is not None:
                target = next((r for r in ordered if r.start == day), None)
        if target is None:
            return False, "没找到这条记录，用「经期 历史」看一下序号。"
        profile.periods = [r for r in profile.periods if r.id != target.id]
        await self.store.upsert(profile)
        return True, f"已删除 {tt.pretty_date(target.start)} 开始的记录。"

    def _apply_settings(
        self, profile: UserProfile, raw: str
    ) -> tuple[list[str], list[str]]:
        changes: list[str] = []
        errors: list[str] = []
        if not raw.strip():
            return changes, errors
        for match in _SETTING_RE.finditer(raw):
            key = match.group(1).lower()
            value = match.group(2).lower()
            spec = SETTING_FIELDS.get(key) or SETTING_FIELDS.get(key.replace(" ", ""))
            if spec is None:
                continue
            field, kind, low, high, label = spec
            if kind == "bool":
                if value in ("开", "on"):
                    setattr(profile.settings, field, True)
                elif value in ("关", "off"):
                    setattr(profile.settings, field, False)
                else:
                    errors.append(f"{label}只能填开或关")
                    continue
                changes.append(f"{label}：{'开' if getattr(profile.settings, field) else '关'}")
                continue
            try:
                number = float(value)
            except ValueError:
                errors.append(f"{label}要填数字")
                continue
            if number < low or number > high:
                errors.append(f"{label}应该在 {low:g}～{high:g} 之间，收到 {value}")
                continue
            if kind == "int":
                setattr(profile.settings, field, int(round(number)))
                changes.append(f"{label}：{int(round(number))} 天")
            else:
                setattr(profile.settings, field, round(number, 2))
                changes.append(f"{label}：{round(number, 2):g}")
        return changes, errors

    async def _auto_sync(
        self, profile: UserProfile, state: PhaseState | None
    ) -> None:
        if state is None or not self._cfg("auto_sync_on_change", False):
            return
        with contextlib.suppress(Exception):
            await self.bridge.push(
                tt.selflearn_snapshot(profile, state),
                group_id=profile.last_umo or profile.user_key,
                duration_hours=int(self._cfg("mood_duration_hours", 24)),
            )
        if self._cfg("enable_chatplus_sync", True):
            with contextlib.suppress(Exception):
                self.chatplus.push(
                    tt.selflearn_snapshot(profile, state),
                    profile.last_umo or profile.user_key,
                )

    # ------------------------------------------------------------------
    # 主动提醒
    # ------------------------------------------------------------------
    async def _reminder_loop(self) -> None:
        interval = max(5, int(self._cfg("reminder_check_minutes", 20))) * 60
        while True:
            try:
                await self._check_reminders()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log.exception("[menstrual] 提醒任务出错")
            await asyncio.sleep(interval)

    async def _check_reminders(self) -> None:
        if not self._cfg("enable_reminder", True):
            return
        if self._in_quiet_window():
            return
        today_key = date.today().isoformat()
        for profile in await self.store.all_profiles():
            if not profile.settings.reminder or not profile.last_umo:
                continue
            if self._sent.get(profile.user_key) == today_key:
                continue
            state = compute_state(profile)
            if state is None or state.paused:
                continue
            kind = self._decide_reminder(state)
            if not kind:
                continue
            # 同一周期的同类提醒只发一次，避免整个 PMS 期天天念叨
            marker = f"{kind}:{state.next_start.isoformat()}"
            if self._phase_sent.get(profile.user_key) == marker:
                continue
            # 这一轮模型写的提醒文案；还没有就先按内置的发，别为了等文案错过时点
            if self._realtime():
                line = await self.voice.live_reminder(profile, state, kind)
                if not line:
                    line = tt.render_reminder(state, kind, profile.settings.reminder_style)
            else:
                voice = self.voice.cached(profile, state)
                if voice is None:
                    self.voice.schedule(profile, state)
                line = tt.render_reminder(
                    state, kind, profile.settings.reminder_style, voice=voice
                )
            if MessageChain is None:  # 宿主版本里找不到消息链，提醒只能放弃
                if not self._no_chain_warned:
                    self._no_chain_warned = True
                    self._log.warning(
                        "[menstrual] 没找到 MessageChain，主动提醒已停用；"
                        "查询、注入、联动都正常"
                    )
                return
            try:
                await self.context.send_message(
                    profile.last_umo,
                    MessageChain().message(line),
                )
            except Exception:
                self._log.warning(
                    "[menstrual] 向 %s 发送提醒失败", profile.user_key, exc_info=True
                )
                continue
            self._sent[profile.user_key] = today_key
            self._phase_sent[profile.user_key] = marker
            self._save_sent()

    def _decide_reminder(self, state: PhaseState) -> str | None:
        if state.underway and state.days_since_start == 0:
            return "first"
        if state.days_to_next == 1:
            return "pre"
        if state.phase.key == "pms":
            return "pms"
        if state.underway and state.days_since_start >= max(1, state.period_length - 1):
            return "ending"
        if state.phase.key == "ovulation" and state.cycle_day == int(
            round(state.cycle_length * 0.5)
        ):
            return "ovulation"
        return None

    def _in_quiet_window(self) -> bool:
        start = int(self._cfg("reminder_hour_from", 9))
        end = int(self._cfg("reminder_hour_to", 23))
        hour = datetime.now().hour
        if start <= end:
            return not (start <= hour < end)
        return end <= hour < start

    def _load_sent(self) -> None:
        try:
            if self._sent_path.exists():
                data = json.loads(self._sent_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    return
                sent = data.get("sent")
                if isinstance(sent, dict):
                    self._sent = {str(k): str(v) for k, v in sent.items()}
                    phase = data.get("phase")
                    if isinstance(phase, dict):
                        self._phase_sent = {str(k): str(v) for k, v in phase.items()}
                else:
                    # 老格式：整个文件就是当日去重表
                    self._sent = {str(k): str(v) for k, v in data.items()}
        except Exception:
            self._log.debug("[menstrual] 读取提醒记录失败", exc_info=True)

    def _save_sent(self) -> None:
        try:
            self._sent_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "sent": self._sent,
                "phase": self._phase_sent,
            }
            self._sent_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            self._log.debug("[menstrual] 保存提醒记录失败", exc_info=True)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    async def _profile_for(
        self, event: AstrMessageEvent, create: bool = True
    ) -> UserProfile | None:
        key = await self._user_key(event)
        character = self._scope() == CHARACTER_SCOPE
        if character:
            # 她自己的身体状态跟聊天对象无关，不记对方的昵称、平台、会话
            return await self.ensure_character_profile(key)

        profile = await self.store.get(key)
        if profile is None:
            if not create:
                return None
            profile = UserProfile(user_key=key)
            self._apply_default_settings(profile)

        try:
            nickname = event.get_sender_name() or ""
        except Exception:
            nickname = ""
        if nickname:
            profile.nickname = nickname
        platform = ""
        with contextlib.suppress(Exception):
            platform = event.get_platform_id() or ""
        if platform:
            profile.platform = platform
        umo = event.unified_msg_origin or ""
        if umo:
            profile.last_umo = umo
        return profile

    async def ensure_character_profile(self, key: str = "") -> UserProfile:
        """她自己那条周期，不存在就按「已经在走」的状态建出来。"""
        target = key or await self._character_key()
        profile = await self.store.get(target)
        if profile is not None:
            return profile
        profile = UserProfile(user_key=target)
        self._apply_default_settings(profile)
        name = str(self._cfg("character_name", "") or "").strip()
        if name:
            profile.nickname = name[:40]
        self._seed_default_cycle(profile)
        with contextlib.suppress(Exception):
            await self.store.upsert(profile)
        return profile

    def _apply_default_settings(self, profile: UserProfile) -> None:
        """把配置里的默认身体参数写进新档案（之后面板还能各自改）。"""
        settings = profile.settings
        for field_name, key, low, high in (
            ("cycle_length", "default_cycle_length", 15, 90),
            ("period_length", "default_period_length", 1, 12),
        ):
            try:
                value = int(self._cfg(key, getattr(settings, field_name)) or 0)
            except (TypeError, ValueError):
                continue
            if low <= value <= high:
                setattr(settings, field_name, value)
        for field_name, key, low, high in (
            ("pain_sensitivity", "default_pain_sensitivity", 0.2, 2.5),
            ("emotion_gain", "default_emotion_gain", 0.2, 2.5),
            ("pms_strength", "default_pms_strength", 0.0, 3.0),
        ):
            try:
                value = float(self._cfg(key, getattr(settings, field_name)))
            except (TypeError, ValueError):
                continue
            setattr(settings, field_name, max(low, min(high, value)))
        preset = str(self._cfg("default_constitution", settings.constitution) or "").lower()
        if preset in CONSTITUTIONS:
            settings.constitution = preset

    def _seed_default_cycle(self, profile: UserProfile) -> None:
        """第一次运行时她已经在周期中间了，不该从「今天第 1 天」突然开始。

        默认落在哪一天由 `default_cycle_day` 决定（1..周期长度）；
        填 0 表示不指定，按周期长度随机挑一天。
        """
        if profile.periods:
            return
        length = max(15, int(profile.settings.cycle_length or 28))
        try:
            wanted = int(self._cfg("default_cycle_day", 0) or 0)
        except (TypeError, ValueError):
            wanted = 0
        if 1 <= wanted <= length:
            offset = wanted - 1
        else:
            offset = random.randint(0, length - 1)
        start = date.today() - timedelta(days=offset)
        profile.periods.append(
            PeriodRecord(
                id=f"p{uuid.uuid4().hex[:8]}",
                start=start,
                end=None,
                note="",
                created_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            )
        )

    def _scope(self) -> str:
        """character：她自己只有一个周期，跟谁聊都一样；user：按聊天对象分别记。"""
        value = str(self._cfg("cycle_scope", CHARACTER_SCOPE) or "").strip().lower()
        return value if value in (CHARACTER_SCOPE, USER_SCOPE) else CHARACTER_SCOPE

    async def _character_key(self, event: AstrMessageEvent | None = None) -> str:
        """她这条身体的档案键。

        默认所有会话共用同一条（跟谁聊都是同一个她）；也可以按 bot 或人格分开——
        同一个 AstrBot 挂了多个账号/多个人格时，各自的周期不该互相串。
        """
        mode = str(self._cfg("character_scope_key", "single") or "single").strip().lower()
        if mode == "bot" and event is not None:
            self_id = ""
            platform = ""
            with contextlib.suppress(Exception):
                self_id = str(getattr(event, "self_id", "") or "")
            with contextlib.suppress(Exception):
                platform = event.get_platform_id() or ""
            if self_id:
                return f"character:{platform}:{self_id}" if platform else f"character:{self_id}"
        elif mode == "persona" and event is not None:
            persona = await self._persona_key(event)
            if persona:
                return f"character:persona:{persona}"
        return CHARACTER_KEY

    async def _persona_key(self, event: AstrMessageEvent) -> str:
        """当前会话用的是哪个人格。拿不到就返回空，退回共用一条。"""
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return ""
        umo = getattr(event, "unified_msg_origin", "") or ""
        for name in ("get_default_persona_v3", "get_default_persona", "get_persona"):
            getter = getattr(manager, name, None)
            if not callable(getter):
                continue
            try:
                result = getter(umo=umo)
                if inspect.isawaitable(result):
                    result = await result
            except TypeError:
                try:
                    result = getter(umo)
                    if inspect.isawaitable(result):
                        result = await result
                except Exception:
                    continue
            except Exception:
                continue
            if isinstance(result, dict):
                label = result.get("name") or result.get("persona_id") or ""
            else:
                label = getattr(result, "name", "") or ""
            if label:
                return str(label)[:40]
        return ""

    async def _user_key(self, event: AstrMessageEvent) -> str:
        if self._scope() == CHARACTER_SCOPE:
            return await self._character_key(event)
        platform = ""
        sender = ""
        with contextlib.suppress(Exception):
            platform = event.get_platform_id() or ""
        with contextlib.suppress(Exception):
            sender = event.get_sender_id() or ""
        if not sender:
            sender = event.unified_msg_origin or "unknown"
        return f"{platform}:{sender}"

    def _tokens(self, event: AstrMessageEvent) -> list[str]:
        raw = (event.message_str or "").strip()
        raw = raw.lstrip("/／!！")
        tokens = [token for token in re.split(r"\s+", raw) if token]
        if tokens and tokens[0].lower() in COMMAND_WORDS:
            tokens = tokens[1:]
        return tokens

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    @property
    def _log(self) -> Any:
        return getattr(self, "logger", logger)


__all__ = ["MenstrualCyclePlugin"]
