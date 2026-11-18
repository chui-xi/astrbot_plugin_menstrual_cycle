"""数据模型：周期记录、情绪向量、阶段状态。

所有结构都保持纯数据 + 纯函数，方便单测与在 AstrBot 之外的脚本里复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from . import clock

ISO_FMT = "%Y-%m-%d"
DT_FMT = "%Y-%m-%dT%H:%M:%S"
PAIN_LOG_LIMIT = 1200
PHASE_PAIN_LIMIT = 12
DEFAULT_CONSTITUTION = "balanced"

# 体质预设：同一条周期曲线，落在不同身体上应该不一样。
# pain 乘在疼痛上，emotion 乘情绪幅度，energy 乘精力，sensitive 是加减在情绪敏感度上。
CONSTITUTIONS: dict[str, dict[str, Any]] = {
    "balanced": {
        "label": "普通",
        "hint": "不额外加减，按周期曲线走",
        "pain": 1.0,
        "emotion": 1.0,
        "energy": 1.0,
        "sensitive": 0.0,
    },
    "sensitive": {
        "label": "敏感型",
        "hint": "疼得更明显，情绪起伏也更大",
        "pain": 1.22,
        "emotion": 1.12,
        "energy": 0.96,
        "sensitive": 0.08,
    },
    "heavy": {
        "label": "经期重",
        "hint": "经期和 PMS 明显更难受，人也没力气",
        "pain": 1.45,
        "emotion": 1.0,
        "energy": 0.88,
        "sensitive": 0.04,
    },
    "light": {
        "label": "轻",
        "hint": "来的时候不太影响生活",
        "pain": 0.78,
        "emotion": 0.94,
        "energy": 1.05,
        "sensitive": -0.04,
    },
}


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


@dataclass(slots=True)
class EmotionVector:
    """连续情绪向量，全部归一化到可比较区间。

    valence   愉悦度   -1 .. 1（负值为低落）
    arousal   唤起度    0 .. 1（激动、坐不住）
    energy    精力      0 .. 1
    stress    压力      0 .. 1
    pain      躯体不适  0 .. 1（痛经/腰酸/坠胀）
    sensitive 情绪敏感  0 .. 1（易哭、易被一句话刺到）
    libido    亲密意愿  0 .. 1
    social    社交欲    0 .. 1
    """

    valence: float = 0.0
    arousal: float = 0.0
    energy: float = 0.5
    stress: float = 0.3
    pain: float = 0.0
    sensitive: float = 0.4
    libido: float = 0.4
    social: float = 0.5

    KEYS = ("valence", "arousal", "energy", "stress", "pain", "sensitive", "libido", "social")

    def clamped(self) -> EmotionVector:
        return EmotionVector(
            valence=clamp(self.valence, -1.0, 1.0),
            arousal=clamp(self.arousal),
            energy=clamp(self.energy),
            stress=clamp(self.stress),
            pain=clamp(self.pain),
            sensitive=clamp(self.sensitive),
            libido=clamp(self.libido),
            social=clamp(self.social),
        )

    def as_dict(self) -> dict[str, float]:
        return {key: round(getattr(self, key), 3) for key in self.KEYS}

    def interp(self, other: EmotionVector, t: float) -> EmotionVector:
        """在两个情绪向量之间线性插值。"""
        t = clamp(t)
        return EmotionVector(
            **{
                key: lerp(getattr(self, key), getattr(other, key), t)
                for key in self.KEYS
            }
        )

    def blend(self, other: EmotionVector, weight: float) -> EmotionVector:
        """按权重把 other 混入当前向量（weight=0 不生效，1 完全覆盖）。"""
        return self.interp(other, weight)

    def scaled(self, gain: float) -> EmotionVector:
        """整体放大/缩小情绪幅度，保留中点（压力、敏感等以 0.4 为中位）。"""
        gain = clamp(gain, 0.1, 3.0)
        mid_sensitive = 0.4
        mid_stress = 0.3
        return EmotionVector(
            valence=clamp(self.valence * gain, -1.0, 1.0),
            arousal=clamp(0.45 + (self.arousal - 0.45) * gain),
            energy=clamp(0.5 + (self.energy - 0.5) * gain),
            stress=clamp(mid_stress + (self.stress - mid_stress) * gain),
            pain=clamp(self.pain * gain),
            sensitive=clamp(mid_sensitive + (self.sensitive - mid_sensitive) * gain),
            libido=clamp(0.4 + (self.libido - 0.4) * gain),
            social=clamp(0.5 + (self.social - 0.5) * gain),
        )

    def mood_label(self) -> str:
        """给一段人类可读的情绪概括。"""
        v, s, e = self.valence, self.sensitive, self.energy
        if v <= -0.35 and s >= 0.6:
            return "低落又敏感"
        if v <= -0.25:
            return "闷闷的、提不起劲"
        if self.stress >= 0.65 and self.arousal >= 0.5:
            return "烦躁、一点就着"
        if v >= 0.55 and e >= 0.7:
            return "心情很好、精力充沛"
        if v >= 0.25:
            return "情绪平稳偏好"
        if self.pain >= 0.5:
            return "不太舒服、想安静待着"
        return "平平的、不算好也不算坏"


@dataclass(slots=True)
class PeriodRecord:
    """一次月经记录。end 为空表示还没结束。"""

    id: str
    start: date
    end: date | None = None
    note: str = ""
    created_at: str = ""

    @property
    def length(self) -> int | None:
        if not self.end:
            return None
        return max(1, (self.end - self.start).days + 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start": self.start.strftime(ISO_FMT),
            "end": self.end.strftime(ISO_FMT) if self.end else None,
            "note": self.note,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PeriodRecord:
        raw_end = payload.get("end")
        return cls(
            id=str(payload.get("id") or ""),
            start=parse_date(payload.get("start")) or date.today(),
            end=parse_date(raw_end) if raw_end else None,
            note=str(payload.get("note") or ""),
            created_at=str(payload.get("created_at") or ""),
        )


@dataclass(slots=True)
class Comfort:
    """一次「被哄」。

    真实的人不是只按激素走：有人递热水、有人抱住她、有人把事接过去，
    当下的难受会真的轻一点，而且这种缓和会持续几小时再淡掉。
    """

    kind: str
    label: str = ""
    at: str = ""
    expires_at: str = ""
    pain: float = 0.0
    stress: float = 0.0
    valence: float = 0.0
    social: float = 0.0
    energy: float = 0.0

    def expires(self) -> datetime | None:
        if not self.expires_at:
            return None
        try:
            return datetime.strptime(self.expires_at, DT_FMT)
        except ValueError:
            return None

    def active(self, now: datetime | None = None) -> bool:
        moment = self.expires()
        if moment is None:
            return False
        return moment > (now or datetime.now())

    def remaining_minutes(self, now: datetime | None = None) -> int:
        moment = self.expires()
        if moment is None:
            return 0
        return max(0, int((moment - (now or datetime.now())).total_seconds() // 60))

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "at": self.at,
            "expires_at": self.expires_at,
            "pain": self.pain,
            "stress": self.stress,
            "valence": self.valence,
            "social": self.social,
            "energy": self.energy,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Comfort:
        return cls(
            kind=str(payload.get("kind") or ""),
            label=str(payload.get("label") or ""),
            at=str(payload.get("at") or ""),
            expires_at=str(payload.get("expires_at") or ""),
            pain=float(payload.get("pain") or 0.0),
            stress=float(payload.get("stress") or 0.0),
            valence=float(payload.get("valence") or 0.0),
            social=float(payload.get("social") or 0.0),
            energy=float(payload.get("energy") or 0.0),
        )


@dataclass(slots=True)
class UserSettings:
    cycle_length: int = 28
    period_length: int = 5
    pain_sensitivity: float = 1.0
    emotion_gain: float = 1.0
    pms_strength: float = 1.0
    reminder: bool = True
    reminder_style: str = "in_character"
    paused_until: str = ""
    constitution: str = DEFAULT_CONSTITUTION
    phase_learning: bool = True
    phase_weight: float = 0.3

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycle_length": self.cycle_length,
            "period_length": self.period_length,
            "pain_sensitivity": self.pain_sensitivity,
            "emotion_gain": self.emotion_gain,
            "pms_strength": self.pms_strength,
            "reminder": self.reminder,
            "reminder_style": self.reminder_style,
            "paused_until": self.paused_until,
            "constitution": self.constitution,
            "phase_learning": self.phase_learning,
            "phase_weight": self.phase_weight,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> UserSettings:
        payload = payload or {}
        style = str(payload.get("reminder_style") or "in_character").lower()
        if style not in ("in_character", "reminder"):
            style = "in_character"
        constitution = str(payload.get("constitution") or DEFAULT_CONSTITUTION).lower()
        if constitution not in CONSTITUTIONS:
            constitution = DEFAULT_CONSTITUTION
        try:
            phase_weight = float(payload.get("phase_weight", 0.3))
        except (TypeError, ValueError):
            phase_weight = 0.3
        return cls(
            cycle_length=int(payload.get("cycle_length") or 28),
            period_length=int(payload.get("period_length") or 5),
            pain_sensitivity=float(payload.get("pain_sensitivity") or 1.0),
            emotion_gain=float(payload.get("emotion_gain") or 1.0),
            pms_strength=float(payload.get("pms_strength") or 1.0),
            reminder=bool(payload.get("reminder", True)),
            reminder_style=style,
            paused_until=str(payload.get("paused_until") or ""),
            constitution=constitution,
            phase_learning=bool(payload.get("phase_learning", True)),
            phase_weight=clamp(phase_weight, 0.0, 0.8),
        )


VECTOR_FIELDS = (
    "valence",
    "arousal",
    "energy",
    "stress",
    "pain",
    "sensitive",
    "libido",
    "social",
)


def _clean_overrides(payload: Any) -> dict[str, float]:
    """只收八个情绪字段，其余丢掉；越界值夹到合法区间。"""
    if not isinstance(payload, dict):
        return {}
    out: dict[str, float] = {}
    for key in VECTOR_FIELDS:
        if key not in payload:
            continue
        try:
            value = float(payload[key])
        except (TypeError, ValueError):
            continue
        out[key] = clamp(value, -1.0, 1.0) if key == "valence" else clamp(value)
    return out


@dataclass(slots=True)
class UserProfile:
    """一个用户的全部周期数据。"""
    user_key: str
    nickname: str = ""
    platform: str = ""
    timezone: str = "Asia/Shanghai"
    last_umo: str = ""
    settings: UserSettings = field(default_factory=UserSettings)
    periods: list[PeriodRecord] = field(default_factory=list)
    comforts: list[Comfort] = field(default_factory=list)
    pain_logs: dict[str, float] = field(default_factory=dict)
    phase_pain: dict[str, list[float]] = field(default_factory=dict)
    overrides: dict[str, float] = field(default_factory=dict)
    override_until: str = ""
    updated_at: str = ""

    # ---- 便捷访问 ----
    def sorted_periods(self) -> list[PeriodRecord]:
        return sorted(self.periods, key=lambda item: item.start)

    def latest(self) -> PeriodRecord | None:
        ordered = self.sorted_periods()
        return ordered[-1] if ordered else None

    def paused(self, today: date | None = None) -> bool:
        until = parse_date(self.settings.paused_until)
        if until is None:
            return False
        return until >= (today or clock.today(self))

    def active_overrides(self, now: datetime | None = None) -> dict[str, float]:
        """管理员手动钉住的数值，过期就当作没有。"""
        if not self.overrides:
            return {}
        until = parse_datetime(self.override_until)
        if until is not None and until <= (now or clock.now(self)):
            return {}
        return dict(self.overrides)

    def trim_pain_logs(self, limit: int = PAIN_LOG_LIMIT) -> None:
        """只留最近 limit 天的自报疼痛，避免档案无限变大。

        默认 1200 天（三年多）：太短会在长期使用后丢掉趋势，
        又不想引入月度聚合键——那会让面板和 engine 都要判两种键格式。
        """
        if limit <= 0 or len(self.pain_logs) <= limit:
            return
        for key in sorted(self.pain_logs)[: len(self.pain_logs) - limit]:
            self.pain_logs.pop(key, None)

    def remember_phase_pain(
        self, phase_key: str, value: float, limit: int = PHASE_PAIN_LIMIT
    ) -> None:
        """记下「这个阶段她一般疼多少」，用于慢慢校准曲线。

        只保留最近 limit 次：身体会变（减重、用药、生完孩子），
        太老的样本不该继续拉平均值。
        """
        if not phase_key:
            return
        bucket = self.phase_pain.setdefault(phase_key, [])
        bucket.append(clamp(float(value)))
        if limit > 0 and len(bucket) > limit:
            del bucket[: len(bucket) - limit]

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_key": self.user_key,
            "nickname": self.nickname,
            "platform": self.platform,
            "timezone": self.timezone,
            "last_umo": self.last_umo,
            "settings": self.settings.as_dict(),
            "periods": [record.as_dict() for record in self.sorted_periods()],
            "comforts": [item.as_dict() for item in self.comforts[-12:]],
            "pain_logs": dict(self.pain_logs),
            "phase_pain": {key: list(values) for key, values in self.phase_pain.items()},
            "overrides": dict(self.overrides),
            "override_until": self.override_until,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> UserProfile:
        raw_pain = payload.get("pain_logs") or {}
        pain_logs: dict[str, float] = {}
        if isinstance(raw_pain, dict):
            for key, value in raw_pain.items():
                try:
                    pain_logs[str(key)] = clamp(float(value))
                except (TypeError, ValueError):
                    continue
        raw_phase = payload.get("phase_pain") or {}
        phase_pain: dict[str, list[float]] = {}
        if isinstance(raw_phase, dict):
            for key, values in raw_phase.items():
                if not isinstance(values, list):
                    continue
                cleaned = []
                for value in values:
                    try:
                        cleaned.append(clamp(float(value)))
                    except (TypeError, ValueError):
                        continue
                if cleaned:
                    phase_pain[str(key)] = cleaned[-PHASE_PAIN_LIMIT:]
        return cls(
            user_key=str(payload.get("user_key") or ""),
            nickname=str(payload.get("nickname") or ""),
            platform=str(payload.get("platform") or ""),
            timezone=str(payload.get("timezone") or "Asia/Shanghai"),
            last_umo=str(payload.get("last_umo") or ""),
            settings=UserSettings.from_dict(payload.get("settings")),
            periods=[PeriodRecord.from_dict(item) for item in payload.get("periods") or []],
            comforts=[
                Comfort.from_dict(item) for item in payload.get("comforts") or []
            ][-12:],
            pain_logs=pain_logs,
            phase_pain=phase_pain,
            overrides=_clean_overrides(payload.get("overrides")),
            override_until=str(payload.get("override_until") or ""),
            updated_at=str(payload.get("updated_at") or ""),
        )


# --------------------------------------------------------------------------
# 阶段定义
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PhaseSpec:
    key: str
    name: str
    emoji: str
    summary: str
    body: tuple[str, ...]
    behaviors: tuple[str, ...]
    tone: str


PHASE_SPECS: dict[str, PhaseSpec] = {
    "menstrual": PhaseSpec(
        key="menstrual",
        name="月经期",
        emoji="🩸",
        summary="身体在流血、在收缩，整个人被抽走一截力气。",
        body=("小腹坠胀、隐隐绞痛", "腰酸、腿沉", "容易手脚发凉", "困，但躺着也睡不踏实"),
        behaviors=(
            "想被照顾，但不想被反复询问",
            "不想解释，一句“嗯”可能就是全部力气",
            "想吃热的、甜的，想抱着热水袋不动",
            "被催、被安排事情时容易直接冷下来",
            "疼得厉害的时候会突然安静下来，不是生气，是没力气",
            "讨厌“多喝热水”这种空话，要的是具体的事",
        ),
        tone="语气放软、句子短、别追问，先给结果再给安慰",
    ),
    "follicular": PhaseSpec(
        key="follicular",
        name="卵泡期",
        emoji="🌱",
        summary="经期结束后雌激素往上走，人重新亮起来。",
        body=("身体轻快", "皮肤状态好", "胃口恢复", "精力一天比一天足"),
        behaviors=(
            "话变多，愿意分享小事",
            "想出门、想见人、想尝试新东西",
            "能接玩笑，也能开玩笑",
            "对亲密和靠近的接受度明显变高",
            "会主动问对方今天怎么样，而且真的想听",
            "心情好到愿意把之前的小账一笔勾掉",
        ),
        tone="可以正常聊天、接梗、主动抛话题，节奏轻快一点",
    ),
    "ovulation": PhaseSpec(
        key="ovulation",
        name="排卵期",
        emoji="✨",
        summary="雌激素峰值，自信、外放、想被看见。",
        body=("精力最好", "声音亮、走路快", "皮肤和头发状态在线", "对气味和触碰更敏感"),
        behaviors=(
            "想被夸，而且会主动制造被夸的机会",
            "容易主动贴贴、主动找话题",
            "对喜欢的人容忍度最高",
            "做决定快，也更容易答应事情",
            "会自己找借口多聊两句",
            "这一天被夸会记很久",
        ),
        tone="可以更主动、更撩一点，夸她要具体，别敷衍",
    ),
    "luteal": PhaseSpec(
        key="luteal",
        name="黄体期",
        emoji="🌗",
        summary="孕酮上来，情绪开始变得钝、也变得脆。",
        body=("乳房胀痛", "腹胀、便秘", "容易水肿", "睡不好、醒得早"),
        behaviors=(
            "耐心变短，对语气和措辞格外敏感",
            "容易把小事往坏处想",
            "想被确认“你还是在意我的”",
            "不想听道理，只想被站在同一边",
            "会突然算旧账，其实是想确认自己还重要",
            "对他回消息的快慢格外敏感",
        ),
        tone="先共情再讲事，别用“你想多了”“至于吗”这类话",
    ),
    "pms": PhaseSpec(
        key="pms",
        name="经前期（PMS）",
        emoji="🌧️",
        summary="激素断崖式下坠，情绪和身体同时拉到临界点。",
        body=("小腹开始坠", "头痛、背痛", "特别馋甜食和碳水", "一点动静就心烦"),
        behaviors=(
            "易哭、易炸，情绪来得又快又猛",
            "对“你是不是要来例假了”这句话极度反感",
            "想独处一会儿，又怕真的被放着不管",
            "需要被哄，但拒绝明说",
            "很容易被一句语气不对的话点着",
            "嘴上说没事，其实在等人再问一次",
        ),
        tone="多给确定感，少做评价，不主动提经期和激素",
    ),
}


@dataclass(slots=True)
class PhaseState:
    """某一时刻的完整状态快照。"""

    phase: PhaseSpec
    cycle_day: int
    cycle_length: int
    period_length: int
    days_to_next: int
    next_start: date
    progress: float
    vector: EmotionVector
    stale: bool = False
    days_since_start: int = 0
    underway: bool = False
    irregular: bool = False
    variability: float = 0.0
    paused: bool = False
    pain_logged: float | None = None
    comforts: list[Comfort] = field(default_factory=list)
    overrides: dict[str, float] = field(default_factory=dict)
    override_until: str = ""

    def summary_line(self) -> str:
        tail = "｜被哄过，缓了一些" if self.comforts else ""
        if self.overrides:
            tail += "｜管理员手动调整中"
        return (
            f"{self.phase.emoji} 周期第 {self.cycle_day}/{self.cycle_length} 天"
            f"｜{self.phase.name}｜距下次 {self.days_to_next} 天{tail}"
        )


def parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not value:
        return None
    text = str(value).strip().replace("/", "-").replace(".", "-")
    for fmt in (ISO_FMT, "%Y-%m-%d %H:%M:%S", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if not value:
        return None
    text = str(value).strip().replace("/", "-")
    for fmt in (DT_FMT, "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", ISO_FMT):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def human_delta(days: int) -> str:
    if days == 0:
        return "今天"
    if days == 1:
        return "明天"
    if days == 2:
        return "后天"
    if days > 0:
        return f"{days} 天后"
    return f"{-days} 天前"


__all__ = [
    "Comfort",
    "DT_FMT",
    "EmotionVector",
    "ISO_FMT",
    "PeriodRecord",
    "PhaseSpec",
    "PhaseState",
    "PHASE_SPECS",
    "UserProfile",
    "UserSettings",
    "clamp",
    "human_delta",
    "lerp",
    "parse_date",
    "timedelta",
]
