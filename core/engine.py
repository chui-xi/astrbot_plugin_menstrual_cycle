"""周期引擎：从经期记录推算当前阶段、预测下一次，并给出情绪向量。

设计要点
--------
1. 不依赖网络与 LLM，纯本地确定性计算；同样的输入永远得到同样的输出。
2. 周期长度用历史中位数平滑，样本不足时向用户设置值靠拢，避免第一次记录就"算出"离谱周期。
3. 情绪用「28 天等效日」上的关键帧插值，因此 24 天短周期和 35 天长周期都能自然伸缩。
4. 日间微扰用 (用户 + 日期) 做种子，保证同一天内多次查询结果一致，跨天又会换一种心情。
"""

from __future__ import annotations

import random
import statistics
from datetime import date, datetime, timedelta

from .models import (
    PHASE_SPECS,
    Comfort,
    EmotionVector,
    PeriodRecord,
    PhaseSpec,
    PhaseState,
    UserProfile,
    UserSettings,
    clamp,
    lerp,
    parse_date,
)

# 参考周期固定为 28 天，所有关键帧都写在这条轴上
REFERENCE_CYCLE = 28.0

# 相邻两次开始日落在什么区间才算「一次周期」：
# 上界放到 90 天，多囊一类长周期不会被当成漏记丢掉
MIN_CYCLE_DAYS = 15
MAX_CYCLE_DAYS = 90

# (28 天等效日, 情绪向量)
_KEYFRAMES: tuple[tuple[float, EmotionVector], ...] = (
    (1.0, EmotionVector(-0.50, 0.30, 0.22, 0.65, 0.80, 0.75, 0.12, 0.30)),
    (3.0, EmotionVector(-0.35, 0.35, 0.30, 0.55, 0.62, 0.72, 0.15, 0.35)),
    (5.0, EmotionVector(-0.12, 0.40, 0.42, 0.42, 0.32, 0.60, 0.22, 0.45)),
    (8.0, EmotionVector(0.25, 0.52, 0.65, 0.30, 0.08, 0.42, 0.42, 0.62)),
    (11.0, EmotionVector(0.50, 0.62, 0.82, 0.22, 0.05, 0.36, 0.62, 0.78)),
    (14.0, EmotionVector(0.68, 0.78, 0.86, 0.20, 0.05, 0.40, 0.86, 0.88)),
    (16.0, EmotionVector(0.60, 0.70, 0.78, 0.26, 0.08, 0.45, 0.70, 0.80)),
    (19.0, EmotionVector(0.40, 0.55, 0.62, 0.36, 0.10, 0.50, 0.50, 0.65)),
    (22.0, EmotionVector(0.18, 0.48, 0.50, 0.50, 0.18, 0.62, 0.42, 0.55)),
    (25.0, EmotionVector(-0.10, 0.55, 0.40, 0.66, 0.28, 0.75, 0.28, 0.40)),
    (27.0, EmotionVector(-0.32, 0.58, 0.32, 0.74, 0.38, 0.84, 0.22, 0.32)),
    (28.0, EmotionVector(-0.42, 0.55, 0.28, 0.70, 0.50, 0.85, 0.18, 0.30)),
    (29.0, EmotionVector(-0.50, 0.30, 0.22, 0.65, 0.80, 0.75, 0.12, 0.30)),
)

# 日间微扰幅度：疼痛单独放宽，让"今天特别难受"更真实
_JITTER = {
    "valence": 0.05,
    "arousal": 0.05,
    "energy": 0.05,
    "stress": 0.05,
    "pain": 0.08,
    "sensitive": 0.04,
    "libido": 0.05,
    "social": 0.05,
}


def _vector_at(reference_day: float) -> EmotionVector:
    """在关键帧上做分段线性插值。reference_day 允许 1..29 之外的值（会取模）。"""
    day = (reference_day - 1.0) % REFERENCE_CYCLE + 1.0
    if day <= _KEYFRAMES[0][0]:
        return _KEYFRAMES[0][1]
    for (day_a, vec_a), (day_b, vec_b) in zip(_KEYFRAMES, _KEYFRAMES[1:]):
        if day_a <= day <= day_b:
            span = day_b - day_a or 1.0
            return vec_a.interp(vec_b, (day - day_a) / span)
    return _KEYFRAMES[-1][1]


def _resolve_cycle_length(profile: UserProfile) -> tuple[int, bool]:
    """返回 (周期长度, 是否来自真实历史)。"""
    settings = profile.settings
    starts = [record.start for record in profile.sorted_periods()]
    diffs = [
        (later - earlier).days
        for earlier, later in zip(starts, starts[1:])
        if MIN_CYCLE_DAYS <= (later - earlier).days <= MAX_CYCLE_DAYS
    ]
    if not diffs:
        return max(20, min(45, settings.cycle_length)), False
    recent = diffs[-6:]
    median = statistics.median(recent)
    # 漏记一次会让差值近乎翻倍，先剔除离群再取中位数
    kept = [d for d in recent if 0.6 * median <= d <= 1.7 * median] or recent
    median = statistics.median(kept)
    if len(kept) >= 3:
        return int(round(median)), True
    # 样本太少：一半历史、一半默认，避免一次记录就锁死
    blended = median * len(kept) / 3 + settings.cycle_length * (3 - len(kept)) / 3
    return int(round(blended)), True


def cycle_variability(profile: UserProfile) -> tuple[float, bool]:
    """周期稳定度：(相对离散度, 是否算不规律)。

    用的是中位绝对偏差（MAD），比标准差抗离群：漏记一次不会让整条曲线失真。
    """
    starts = [record.start for record in profile.sorted_periods()]
    diffs = [
        (later - earlier).days
        for earlier, later in zip(starts, starts[1:])
        if MIN_CYCLE_DAYS <= (later - earlier).days <= MAX_CYCLE_DAYS
    ]
    if len(diffs) < 3:
        return 0.0, False
    recent = diffs[-8:]
    median = statistics.median(recent)
    if median <= 0:
        return 0.0, False
    deviations = sorted(abs(item - median) for item in recent)
    mad = statistics.median(deviations)
    spread = max(recent) - min(recent)
    irregular = mad / median >= 0.09 or spread >= 10
    return round(mad / median, 3), irregular


def active_comforts(
    profile: UserProfile, now: datetime | None = None
) -> list[Comfort]:
    """还在生效的「被哄」记录。"""
    moment = now or datetime.now()
    return [item for item in profile.comforts if item.active(moment)]


def _apply_comforts(vector: EmotionVector, comforts: list[Comfort]) -> EmotionVector:
    """把关心叠加进状态。同类多次做边际递减，别让一句「抱抱」刷满。"""
    if not comforts:
        return vector
    total = {"pain": 0.0, "stress": 0.0, "valence": 0.0, "social": 0.0, "energy": 0.0}
    seen: dict[str, int] = {}
    for item in comforts:
        weight = 0.62 ** seen.get(item.kind, 0)
        seen[item.kind] = seen.get(item.kind, 0) + 1
        for field in total:
            total[field] += getattr(item, field, 0.0) * weight
    return EmotionVector(
        valence=clamp(vector.valence + total["valence"], -1.0, 1.0),
        arousal=vector.arousal,
        energy=clamp(vector.energy + total["energy"]),
        stress=clamp(vector.stress + total["stress"]),
        pain=clamp(vector.pain + total["pain"]),
        sensitive=clamp(vector.sensitive - 0.03 * len(comforts)),
        libido=vector.libido,
        social=clamp(vector.social + total["social"]),
    ).clamped()


def pain_log_of(profile: UserProfile, day: date) -> float | None:
    """她当天自己报的疼痛（0-1）。自己说的比曲线算的准。"""
    value = profile.pain_logs.get(day.strftime("%Y-%m-%d"))
    if value is None:
        return None
    try:
        return clamp(float(value))
    except (TypeError, ValueError):
        return None


def _apply_pain_log(
    vector: EmotionVector, logged: float | None
) -> EmotionVector:
    """有自报疼痛就以它为主，曲线只保留一点余量，避免「说不疼还一直喊疼」。"""
    if logged is None:
        return vector
    blended = logged * 0.82 + vector.pain * 0.18
    return EmotionVector(
        valence=clamp(vector.valence - max(0.0, blended - 0.5) * 0.5, -1.0, 1.0),
        arousal=vector.arousal,
        energy=clamp(vector.energy - max(0.0, blended - 0.55) * 0.35),
        stress=clamp(vector.stress + max(0.0, blended - 0.5) * 0.3),
        pain=clamp(blended),
        sensitive=clamp(vector.sensitive + max(0.0, blended - 0.5) * 0.2),
        libido=vector.libido,
        social=clamp(vector.social - max(0.0, blended - 0.6) * 0.2),
    ).clamped()


def _resolve_period_length(profile: UserProfile, fallback: int) -> int:
    lengths = [record.length for record in profile.sorted_periods() if record.length]
    if not lengths:
        return max(2, min(10, fallback))
    return int(round(statistics.median(lengths[-6:])))


def _phase_bounds(cycle_length: int, period_length: int) -> dict[str, tuple[float, float]]:
    """按比例给出各阶段的 28 天等效日区间（左闭右开）。"""
    scale = REFERENCE_CYCLE / cycle_length
    period_end = period_length * scale
    ovulation_center = REFERENCE_CYCLE * 0.50
    return {
        "menstrual": (1.0, max(2.0, period_end + scale)),
        "follicular": (max(2.0, period_end + scale), ovulation_center - 1.5),
        "ovulation": (ovulation_center - 1.5, ovulation_center + 1.5),
        "luteal": (ovulation_center + 1.5, REFERENCE_CYCLE * 0.79),
        "pms": (REFERENCE_CYCLE * 0.79, REFERENCE_CYCLE + 1.0),
    }


def _phase_for(reference_day: float, cycle_length: int, period_length: int) -> PhaseSpec:
    bounds = _phase_bounds(cycle_length, period_length)
    for key in ("menstrual", "follicular", "ovulation", "luteal", "pms"):
        low, high = bounds[key]
        if low <= reference_day < high:
            return PHASE_SPECS[key]
    return PHASE_SPECS["pms"]


def _apply_modifiers(
    vector: EmotionVector,
    phase: PhaseSpec,
    settings: UserSettings,
) -> EmotionVector:
    """按用户设置放大疼痛与情绪幅度。"""
    scaled = vector.scaled(settings.emotion_gain)

    # 疼痛敏感度单独作用，避免被 emotion_gain 重复放大
    pain = clamp(vector.pain * settings.pain_sensitivity)
    # 经前期强度只作用于 PMS 阶段，往负面与高敏感方向推
    pms = clamp(settings.pms_strength, 0.0, 2.0)
    if phase.key == "pms" and pms != 1.0:
        delta = (pms - 1.0) * 0.25
        scaled = EmotionVector(
            valence=clamp(scaled.valence - abs(delta), -1.0, 1.0) if delta > 0 else clamp(scaled.valence + abs(delta) * 0.6, -1.0, 1.0),
            arousal=scaled.arousal,
            energy=clamp(scaled.energy + (delta * 0.3 if delta > 0 else -delta * 0.3)),
            stress=clamp(scaled.stress + delta),
            pain=clamp(max(pain, scaled.pain) + delta * 0.4),
            sensitive=clamp(scaled.sensitive + delta),
            libido=scaled.libido,
            social=clamp(scaled.social - delta * 0.6),
        )
    return EmotionVector(
        valence=scaled.valence,
        arousal=scaled.arousal,
        energy=scaled.energy,
        stress=scaled.stress,
        pain=clamp(pain if phase.key in {"menstrual", "pms"} else scaled.pain),
        sensitive=scaled.sensitive,
        libido=scaled.libido,
        social=scaled.social,
    ).clamped()


def _apply_overrides(
    vector: EmotionVector, overrides: dict[str, float]
) -> EmotionVector:
    """面板上手钉的数值直接替换对应维度，其余保持推算结果。"""
    if not overrides:
        return vector
    payload = vector.as_dict()
    for key, value in overrides.items():
        if key in payload:
            payload[key] = value
    payload["valence"] = clamp(payload["valence"], -1.0, 1.0)
    return EmotionVector(**payload).clamped()


def _jitter(vector: EmotionVector, seed: str) -> EmotionVector:
    rng = random.Random(seed)
    payload = {}
    for key, amplitude in _JITTER.items():
        payload[key] = getattr(vector, key) + rng.uniform(-amplitude, amplitude)
    payload["valence"] = clamp(payload["valence"], -1.0, 1.0)
    return EmotionVector(**payload).clamped()


def compute_state(
    profile: UserProfile,
    today: date | None = None,
    now: datetime | None = None,
    *,
    ignore_overrides: bool = False,
) -> PhaseState | None:
    """计算某一天的阶段与情绪。没有记录时返回 None。

    ``ignore_overrides`` 用来回看历史：管理员手动钉住的是「从现在起几小时」，
    不该把过去几天也一起改掉。
    """
    today = today or date.today()
    moment = now or datetime.combine(today, datetime.now().time())
    periods = profile.sorted_periods()
    if not periods:
        return None

    latest = periods[-1]
    cycle_length, _ = _resolve_cycle_length(profile)
    period_length = _resolve_period_length(profile, profile.settings.period_length)

    days_since_start = (today - latest.start).days
    # 记录在未来（用户预填）：按第 1 天处理
    if days_since_start < 0:
        days_since_start = 0

    stale = days_since_start > cycle_length + 10
    cycle_day = days_since_start % cycle_length + 1

    # 下一次开始日：沿着记录点按周期滚动
    next_start = latest.start + timedelta(days=cycle_length)
    guard = 0
    while next_start <= today and guard < 240:
        next_start += timedelta(days=cycle_length)
        guard += 1
    days_to_next = (next_start - today).days

    reference_day = (cycle_day - 1) * (REFERENCE_CYCLE / cycle_length) + 1.0
    phase = _phase_for(reference_day, cycle_length, period_length)
    underway = (
        latest.end is None and 0 <= (today - latest.start).days < period_length
    ) or (latest.end is not None and latest.start <= today <= latest.end)

    if underway:
        phase = PHASE_SPECS["menstrual"]

    base = _vector_at(reference_day)
    vector = _apply_modifiers(base, phase, profile.settings)
    vector = _jitter(vector, f"{profile.user_key}:{today.isoformat()}")

    # 自报疼痛优先于曲线，随后叠加还在生效的「被哄」
    logged = pain_log_of(profile, today)
    vector = _apply_pain_log(vector, logged)
    comforts = active_comforts(profile, moment)
    vector = _apply_comforts(vector, comforts)

    # 管理员在面板上手钉的数值最后覆盖，优先级最高
    overrides = {} if ignore_overrides else profile.active_overrides(moment)
    vector = _apply_overrides(vector, overrides)

    progress = (cycle_day - 1) / max(1, cycle_length)
    variability, irregular = cycle_variability(profile)

    return PhaseState(
        phase=phase,
        cycle_day=cycle_day,
        cycle_length=cycle_length,
        period_length=period_length,
        days_to_next=max(0, days_to_next),
        next_start=next_start,
        progress=progress,
        vector=vector,
        stale=stale,
        days_since_start=days_since_start,
        underway=underway,
        irregular=irregular,
        variability=variability,
        paused=profile.paused(today),
        pain_logged=logged,
        comforts=comforts,
        overrides=overrides,
        override_until=profile.override_until if overrides else "",
    )


def recent_states(
    profile: UserProfile, days: int = 2, today: date | None = None
) -> list[tuple[date, PhaseState]]:
    """回看最近几天（不含今天）的状态，给她一段「这几天」的记忆。

    回看一律忽略管理员钉住的数值：那是「从现在起几小时」，不该把昨天也改写。
    """
    if days <= 0:
        return []
    base = today or date.today()
    out: list[tuple[date, PhaseState]] = []
    for offset in range(1, days + 1):
        day = base - timedelta(days=offset)
        state = compute_state(profile, day, ignore_overrides=True)
        if state is not None:
            out.append((day, state))
    return out


def predict_next_periods(profile: UserProfile, count: int = 3) -> list[tuple[date, date]]:
    """预测未来若干次经期区间 (开始, 结束)。"""
    periods = profile.sorted_periods()
    if not periods:
        return []
    cycle_length, _ = _resolve_cycle_length(profile)
    period_length = _resolve_period_length(profile, profile.settings.period_length)
    state = compute_state(profile)
    start = state.next_start if state else periods[-1].start + timedelta(days=cycle_length)
    out: list[tuple[date, date]] = []
    for index in range(count):
        begin = start + timedelta(days=cycle_length * index)
        out.append((begin, begin + timedelta(days=max(0, period_length - 1))))
    return out


def cycle_stats(profile: UserProfile) -> dict[str, float | int | None]:
    """给报告用的统计信息。"""
    starts = [record.start for record in profile.sorted_periods()]
    diffs = [
        (later - earlier).days
        for earlier, later in zip(starts, starts[1:])
        if MIN_CYCLE_DAYS <= (later - earlier).days <= MAX_CYCLE_DAYS
    ]
    cycle_length, from_history = _resolve_cycle_length(profile)
    period_length = _resolve_period_length(profile, profile.settings.period_length)
    return {
        "records": len(starts),
        "avg_cycle": round(statistics.mean(diffs), 1) if diffs else None,
        "median_cycle": statistics.median(diffs) if diffs else None,
        "min_cycle": min(diffs) if diffs else None,
        "max_cycle": max(diffs) if diffs else None,
        "cycle_length": cycle_length,
        "period_length": period_length,
        "from_history": int(bool(from_history)),
    }


def normalize_profiles_order(periods: list[PeriodRecord]) -> list[PeriodRecord]:
    """去重并按开始日排序（同一天只保留一条）。"""
    seen: dict[str, PeriodRecord] = {}
    for record in periods:
        seen[record.start.isoformat()] = record
    return [seen[key] for key in sorted(seen)]


def pretty_date(value: date) -> str:
    return f"{value.month} 月 {value.day} 日"


def phase_progress_text(state: PhaseState) -> str:
    """简单的文本进度条。"""
    filled = int(round(state.progress * 10))
    filled = max(0, min(10, filled))
    return "▰" * filled + "▱" * (10 - filled)


def resolve_day_state(profile: UserProfile, target: date) -> PhaseState | None:
    """给定任意日期（可用于"那天我会是什么状态"类提问）。"""
    return compute_state(profile, target)


def total_days_between(left: date, right: date) -> int:
    return (right - left).days


def as_date(value: object) -> date | None:
    return parse_date(value)


def blend_vectors(vectors: list[EmotionVector]) -> EmotionVector:
    """多个向量的等权平均，供未来扩展（例如多用户共享）使用。"""
    if not vectors:
        return EmotionVector()
    keys = EmotionVector.KEYS
    averaged = {
        key: sum(getattr(vec, key) for vec in vectors) / len(vectors) for key in keys
    }
    return EmotionVector(**averaged).clamped()


def smooth(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return lerp(low, high, clamp(value))


__all__ = [
    "REFERENCE_CYCLE",
    "blend_vectors",
    "compute_state",
    "cycle_stats",
    "normalize_profiles_order",
    "phase_progress_text",
    "predict_next_periods",
    "pretty_date",
    "resolve_day_state",
    "smooth",
]
