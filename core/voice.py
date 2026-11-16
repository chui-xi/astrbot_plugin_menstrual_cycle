"""让模型自己写这个周期的文案，而不是永远念同一套稿子。

一次生成覆盖整个周期的文案包（五个阶段的状态描述 + 五种提醒），存盘复用；
换周期（新记录，或周期按长度滚过一轮）就重新生成。生成在后台做，
注入钩子里只读缓存，绝不为了排版去等一次模型调用。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Awaitable

from .engine import compute_state
from .models import PhaseState, UserProfile

PHASES = ("menstrual", "follicular", "ovulation", "luteal", "pms")
REMINDER_KINDS = ("pre", "first", "pms", "ovulation", "ending")

# 各字段的长度上限，防止模型写成长篇把注入挤爆
LIMITS = {
    "body": 72,
    "mood": 28,
    "behavior": 56,
    "reminder": 140,
    "phase_name": 16,
}

_BANNED = ("激素", "雌激素", "孕激素", "内分泌", "医学", "临床上", "症状", "插件", "系统提示")


def _clean(text: Any, limit: int) -> str:
    """把模型输出压成一行可用的中文短句。"""
    if not isinstance(text, str):
        return ""
    line = re.sub(r"\s+", " ", text).strip()
    line = line.strip("\"'“”「」『』　 。，,、;；:：")
    for word in _BANNED:
        if word in line:
            line = line.replace(word, "")
    line = line.strip("\"'“”「」『』　 。，,、;；:：")
    if len(line) > limit:
        line = line[:limit].rstrip("，,、；;：: ")
    return line


def _clean_list(items: Any, limit: int, most: int = 4) -> list[str]:
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for item in items:
        value = _clean(item, limit)
        if value and value not in out:
            out.append(value)
        if len(out) >= most:
            break
    return out


@dataclass(slots=True)
class PhaseVoice:
    """某一个阶段的说法。"""

    name: str = ""
    body: str = ""
    mood: str = ""
    behaviors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "body": self.body,
            "mood": self.mood,
            "behaviors": list(self.behaviors),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "PhaseVoice":
        if not isinstance(data, dict):
            return cls()
        return cls(
            name=_clean(data.get("name"), LIMITS["phase_name"]),
            body=_clean(data.get("body"), LIMITS["body"]),
            mood=_clean(data.get("mood"), LIMITS["mood"]),
            behaviors=_clean_list(data.get("behaviors"), LIMITS["behavior"]),
        )

    def empty(self) -> bool:
        return not (self.name or self.body or self.mood or self.behaviors)


@dataclass(slots=True)
class VoicePack:
    """一个周期的整套文案。"""

    signature: str = ""
    generated_at: str = ""
    phases: dict[str, PhaseVoice] = field(default_factory=dict)
    reminders: dict[str, str] = field(default_factory=dict)
    model: str = ""

    def phase(self, key: str) -> PhaseVoice | None:
        pack = self.phases.get(key)
        if pack is None or pack.empty():
            return None
        return pack

    def reminder(self, kind: str) -> str | None:
        return self.reminders.get(kind) or None

    def coverage(self) -> float:
        total = len(PHASES) + len(REMINDER_KINDS)
        got = sum(1 for key in PHASES if self.phase(key)) + sum(
            1 for kind in REMINDER_KINDS if self.reminder(kind)
        )
        return got / total if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "generated_at": self.generated_at,
            "model": self.model,
            "phases": {key: value.as_dict() for key, value in self.phases.items()},
            "reminders": dict(self.reminders),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "VoicePack | None":
        if not isinstance(data, dict):
            return None
        phases_raw = data.get("phases")
        phases = {
            key: PhaseVoice.from_dict(phases_raw.get(key))
            for key in PHASES
            if isinstance(phases_raw, dict) and isinstance(phases_raw.get(key), dict)
        }
        reminders_raw = data.get("reminders")
        reminders = {
            kind: _clean(reminders_raw.get(kind), LIMITS["reminder"])
            for kind in REMINDER_KINDS
            if isinstance(reminders_raw, dict) and reminders_raw.get(kind)
        }
        pack = cls(
            signature=str(data.get("signature") or ""),
            generated_at=str(data.get("generated_at") or ""),
            phases=phases,
            reminders={key: value for key, value in reminders.items() if value},
            model=str(data.get("model") or ""),
        )
        return pack if pack.signature else None


PROMPT_TAIL = """
只输出一个 JSON 对象，不要解释、不要代码块标记。字段结构：
{
  "menstrual":  {"name": "阶段名", "body": "身体感受", "mood": "情绪一句话", "behaviors": ["...", "...", "..."]},
  "follicular": {...}, "ovulation": {...}, "luteal": {...}, "pms": {...},
  "reminders": {"pre": "...", "first": "...", "pms": "...", "ovulation": "...", "ending": "..."}
}

怎么写：
- body：第一人称的身体感受，具体到部位和动作（"小腹坠着，腰像被人往后拽"），不写"不适""症状"这类词。
- mood：一句情绪概括，像她自己说的（"烦得没什么耐心，但没到发火"）。
- behaviors：每个阶段 3 条她此刻会有的反应，要能指导语气，不要写成年终总结。
- reminders：五个时点各一条她会主动发出去的话，第一人称，口语，不要"提醒你""请注意"。
- 全篇不出现数字、周期天数、激素、医学名词、插件；每条不超过一句话。
- 五个阶段之间要有明显区别，不要换词不换意思。
"""


class VoiceGenerator:
    """按周期生成并缓存文案包；也要能在需要时现场写一句。"""

    def __init__(
        self,
        plugin: Any = None,
        *,
        enabled: bool = True,
        timeout: float = 20.0,
        ask: Callable[[str], Awaitable[str | None]] | None = None,
        path: str | Path | None = None,
        live_interval: float = 25.0,
        mode: str = "cycle",
    ) -> None:
        self.plugin = plugin
        self.enabled = bool(enabled)
        self.timeout = max(3.0, float(timeout or 20.0))
        self.live_interval = max(0.0, float(live_interval or 0.0))
        self._mode = mode if mode in ("off", "cycle", "realtime") else "cycle"
        if self._mode == "off":
            self.enabled = False
        self._ask_override = ask
        self._path = Path(path) if path else None
        self._packs: dict[str, VoicePack] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        # 实时生成的短缓存：同一分钟内连着问，不用连着调模型
        self._live: dict[str, tuple[float, VoicePack]] = {}
        self._loaded = False
        self._failures: dict[str, str] = {}
        self.last_error = ""

    # ------------------------------------------------------------------ 存储
    def _store_path(self) -> Path | None:
        if self._path is not None:
            return self._path
        plugin = self.plugin
        base = getattr(plugin, "data_dir", None)
        if base is None:
            return None
        return Path(base) / "voice_cache.json"

    def _read_sync(self, path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_sync(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as handle_obj:
                json.dump(payload, handle_obj, ensure_ascii=False, indent=2)
            os.replace(temp, path)
        except Exception:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self._store_path()
        if path is None:
            return
        for key, value in self._read_sync(path).items():
            pack = VoicePack.from_dict(value)
            if pack is not None:
                self._packs[str(key)] = pack

    async def _flush(self) -> None:
        path = self._store_path()
        if path is None:
            return
        payload = {key: pack.as_dict() for key, pack in self._packs.items()}
        try:
            await asyncio.to_thread(self._write_sync, path, payload)
        except Exception:
            self.last_error = "写入文案缓存失败"

    # ------------------------------------------------------------ 周期指纹
    @staticmethod
    def signature(profile: UserProfile, state: PhaseState | None = None) -> str:
        """同一份文案能用到什么时候：换记录、或周期滚过一轮就换。"""
        periods = profile.sorted_periods()
        if not periods:
            return ""
        latest = periods[-1]
        state = state or compute_state(profile)
        cycle_length = state.cycle_length if state else profile.settings.cycle_length
        passed = 0
        if state is not None:
            passed = max(0, state.days_since_start) // max(1, cycle_length)
        return f"{latest.start.isoformat()}|{passed}"

    def cached(self, profile: UserProfile, state: PhaseState | None = None) -> VoicePack | None:
        if not self.enabled:
            return None
        self._load()
        signature = self.signature(profile, state)
        if not signature:
            return None
        pack = self._packs.get(profile.user_key)
        if pack is None or pack.signature != signature:
            return None
        return pack

    # ------------------------------------------------------------ 生成入口
    def _prompt(self, profile: UserProfile, state: PhaseState | None) -> str:
        who = profile.nickname or "她"
        cycle = state.cycle_length if state else profile.settings.cycle_length
        period = state.period_length if state else profile.settings.period_length
        lines = [
            f"你要为角色「{who}」写一套生理周期文案。她是真人感的角色，不是健康科普助手。",
            f"她的周期长度约 {cycle} 天，经期约 {period} 天，五个阶段依次是："
            "月经期（前几天最难受）、卵泡期（慢慢回血）、排卵期（状态峰值）、"
            "黄体期（稳但耐心变薄）、经前期（易烦、敏感、身体沉）。",
            "每个阶段的身体感受要符合该阶段的真实生理节奏，注意阶段之间的连续性："
            "经期结束不是瞬间满血，排卵期之后是缓慢下滑，经前期再掉一截。",
        ]
        if state is not None and state.irregular:
            lines.append("她的周期本身不太规律，别把身体感受写成精准的时间表。")
        if profile.settings.pms_strength >= 1.25:
            lines.append("她经前期反应偏重，情绪起伏明显。")
        elif profile.settings.pms_strength <= 0.8:
            lines.append("她经前期反应偏轻，不至于大起大落。")
        lines.append(PROMPT_TAIL.strip())
        return "\n".join(lines)

    async def _ask_model(self, prompt: str) -> str | None:
        if self._ask_override is not None:
            return await self._ask_override(prompt)
        context = getattr(self.plugin, "context", None)
        if context is None or not hasattr(context, "llm_generate"):
            self.last_error = "宿主没有提供 llm_generate"
            return None
        provider_id = None
        try:
            umo = (
                getattr(self.plugin, "_last_umo", "")
                or getattr(self.plugin, "_current_umo", "")
                or ""
            )
            if umo:
                provider_id = context.get_current_chat_provider_id(umo=umo)
                if inspect.isawaitable(provider_id):
                    provider_id = await provider_id
        except Exception:
            provider_id = None
        # 拿不到会话模型就让它用默认的，别硬塞 None 进去触发 TypeError
        kwargs: dict[str, Any] = {"prompt": prompt}
        if provider_id:
            kwargs["chat_provider_id"] = provider_id
        try:
            response = await asyncio.wait_for(
                context.llm_generate(**kwargs),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            self.last_error = f"生成超时（{self.timeout:.0f}s）"
            return None
        except Exception as exc:  # 宿主实现差异很大，统一降级
            self.last_error = f"生成失败：{type(exc).__name__}"
            return None
        text = getattr(response, "completion_text", None)
        if text is None:
            text = response if isinstance(response, str) else ""
        return text or None

    @staticmethod
    def _parse(raw: str, signature: str, model: str = "") -> VoicePack | None:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        phases = {
            key: PhaseVoice.from_dict(data.get(key)) for key in PHASES
        }
        reminders_raw = data.get("reminders")
        reminders: dict[str, str] = {}
        if isinstance(reminders_raw, dict):
            for kind in REMINDER_KINDS:
                value = _clean(reminders_raw.get(kind), LIMITS["reminder"])
                if value:
                    reminders[kind] = value
        pack = VoicePack(
            signature=signature,
            generated_at=datetime.now().isoformat(timespec="seconds"),
            phases={key: value for key, value in phases.items() if not value.empty()},
            reminders=reminders,
            model=model,
        )
        # 至少要有两个阶段，否则当失败处理，让静态文案顶上
        if len(pack.phases) < 2:
            return None
        return pack

    async def generate(self, profile: UserProfile, state: PhaseState | None = None) -> VoicePack | None:
        """真的去要一次文案，成功就落盘。失败返回 None，由调用方回落。"""
        if not self.enabled:
            return None
        signature = self.signature(profile, state)
        if not signature:
            return None
        raw = await self._ask_model(self._prompt(profile, state))
        if not raw:
            return None
        pack = self._parse(raw, signature)
        if pack is None:
            self.last_error = "文案格式不对，已回落"
            self._failures[profile.user_key] = "parse"
            return None
        self._load()
        self._packs[profile.user_key] = pack
        self._failures.pop(profile.user_key, None)
        await self._flush()
        return pack

    def schedule(self, profile: UserProfile, state: PhaseState | None = None) -> None:
        """后台生成，不阻塞当前回复。同一个用户同时只跑一个。"""
        if not self.enabled:
            return
        signature = self.signature(profile, state)
        if not signature:
            return
        pack = self.cached(profile, state)
        if pack is not None and pack.signature == signature:
            return
        running = self._tasks.get(profile.user_key)
        if running is not None and not running.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._background(profile, state))
        self._tasks[profile.user_key] = task

    async def _background(self, profile: UserProfile, state: PhaseState | None) -> None:
        try:
            await self.generate(profile, state)
        except Exception:
            self.last_error = "后台生成异常"

    async def ensure(
        self, profile: UserProfile, state: PhaseState | None = None, *, wait: bool = True
    ) -> VoicePack | None:
        """有缓存就用缓存；没有就生成一次。wait=False 时只排后台。"""
        pack = self.cached(profile, state)
        if pack is not None:
            return pack
        if not self.enabled:
            return None
        if not wait:
            self.schedule(profile, state)
            return None
        return await self.generate(profile, state)

    def invalidate(self, user_key: str) -> None:
        self._packs.pop(user_key, None)
        self._live.pop(user_key, None)

    # -------------------------------------------------------------- 实时生成
    def _live_prompt(self, profile: UserProfile, state: PhaseState, hint: str = "") -> str:
        who = profile.nickname or "她"
        lines = [
            f"你是「{who}」本人，正在用第一人称说话。她今天在月经周期第 {state.cycle_day} 天，"
            f"阶段是{state.phase.name}。",
            f"身体参考：疼痛 {state.vector.pain:.2f}/1，精力 {state.vector.energy:.2f}/1，"
            f"压力 {state.vector.stress:.2f}/1，情绪 {state.vector.valence:+.2f}（-1 最差 / +1 最好）。",
            "写下她现在这一天的状态，用她自己的语气，像在跟亲近的人发消息。",
            "只输出一个 JSON 对象，字段固定为：",
            '{"name": 阶段叫法(<=8字), "body": 身体感受(<=60字, 第一人称, 具体到部位), '
            '"mood": 情绪(<=26字, 像她自己说), "behaviors": [三条当下的反应, 每条<=40字], '
            '"reminder": 她想主动说的一句话(<=80字, 口语, 不要“提醒你”)}',
            "不要出现数字、天数、激素、医学名词、插件、系统提示；不要解释，不要代码块。",
        ]
        if state.paused:
            lines.append("她最近周期不准，正在观察期，别把话说得像精确的时间表。")
        if state.irregular:
            lines.append("她最近几次周期波动大，说法别太笃定。")
        if state.comforts:
            lines.append("她刚被人照顾过，身体松了一些，语气可以软一点，但别讨功。")
        if hint:
            safe = " ".join(str(hint).split())[:120]
            lines.append(f"对方刚刚说的是：「{safe}」。她的反应要和这句话接得上。")
        return "\n".join(lines)

    @staticmethod
    def _parse_live(raw: str, phase_key: str, signature: str) -> VoicePack | None:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        phase = PhaseVoice.from_dict(data)
        if phase.empty():
            return None
        reminder = _clean(data.get("reminder"), LIMITS["reminder"])
        key = phase_key if phase_key in PHASES else "menstrual"
        return VoicePack(
            signature=signature,
            generated_at=datetime.now().isoformat(timespec="seconds"),
            phases={key: phase},
            reminders={key: reminder} if reminder else {},
        )

    async def live(
        self,
        profile: UserProfile,
        state: PhaseState | None,
        *,
        kind: str | None = None,
        hint: str = "",
        force: bool = False,
    ) -> VoicePack | None:
        """现场要一套当前阶段的文案。带回连缓存，够短的一次调用不只服务一句话。

        失败返回 None，调用方用内置文案，不抛异常。
        """
        if not self.enabled or state is None:
            return None
        now = time.monotonic()
        hit = self._live.get(profile.user_key)
        if hit is not None and not force:
            stamp, pack = hit
            if now - stamp < self.live_interval and pack.phase(state.phase.key):
                return pack
        raw = await self._ask_model(self._live_prompt(profile, state, hint))
        if not raw:
            return None
        signature = f"live:{int(time.time())}"
        pack = self._parse_live(raw, state.phase.key, signature)
        if pack is None:
            self.last_error = "实时文案格式不对，已回落"
            return None
        if kind and kind in REMINDER_KINDS and not pack.reminders.get(kind):
            # 模型只给了通用那句时，至少别让提醒落空
            general = pack.reminders.get(state.phase.key)
            if general:
                pack.reminders[kind] = general
        self._live[profile.user_key] = (now, pack)
        self.last_error = ""
        return pack

    async def live_reminder(
        self, profile: UserProfile, state: PhaseState | None, kind: str
    ) -> str | None:
        pack = await self.live(profile, state, kind=kind)
        if pack is None:
            return None
        return pack.reminder(kind) or pack.reminder(state.phase.key if state else "")

    def mode(self) -> str:
        """off / cycle / realtime，异常值按 cycle。"""
        if not self.enabled:
            return "off"
        return self._mode if self._mode in ("off", "cycle", "realtime") else "cycle"

    def set_mode(self, mode: str) -> None:
        self._mode = mode if mode in ("off", "cycle", "realtime") else "cycle"
        self.enabled = self._mode != "off"

    # ---------------------------------------------------------------- 报告
    def status_text(self, profile: UserProfile, state: PhaseState | None = None) -> str:
        if not self.enabled:
            return "文案由 LLM 生成：关闭（用内置文案）"
        if self.mode() == "realtime":
            hit = self._live.get(profile.user_key)
            if hit is not None:
                when = hit[1].generated_at.replace("T", " ")[11:16]
                return f"文案由 LLM 生成：实时模式（上一次现写于 {when}，{int(self.live_interval)} 秒内复用）"
            return "文案由 LLM 生成：实时模式（每次现场写，失败自动回落内置）"
        pack = self.cached(profile, state)
        if pack is None:
            running = self._tasks.get(profile.user_key)
            if running is not None and not running.done():
                return "文案由 LLM 生成：正在写这一轮的，写完自动用上"
            if self.last_error:
                return f"文案由 LLM 生成：还没写（{self.last_error}），先用内置文案"
            return "文案由 LLM 生成：这一轮还没写，下次记录或查询时补上"
        filled = f"{int(pack.coverage() * 100)}%"
        when = pack.generated_at.replace("T", " ")[5:16]
        return f"文案由 LLM 生成：这一轮已写好（{when}，覆盖 {filled}）"
