"""文案层：状态卡、预测、历史，以及注入给模型的状态提示词。

这里所有的中文都是「给她看/给模型看」的自然语言，刻意避免医学腔和客服腔。
"""

from __future__ import annotations

import random
from datetime import date

from . import clock
from . import comfort as comfort_mod
from .engine import (
    compute_state,
    cycle_stats,
    phase_progress_text,
    predict_next_periods,
    pretty_date,
    recent_states,
)
from .models import (
    EmotionVector,
    PhaseState,
    UserProfile,
    human_delta,
    parse_date,
)


def _bar(value: float, width: int = 10) -> str:
    filled = max(0, min(width, int(round(value * width))))
    return "▰" * filled + "▱" * (width - filled)


def _rotate(items: list[str], count: int, seed: str) -> list[str]:
    """按日期轮换条目，避免每天看到同一批话。"""
    if not items:
        return []
    pool = list(items)
    random.Random(seed).shuffle(pool)
    return pool[: min(count, len(pool))]


def _voice_phase(voice: object | None, key: str):
    """模型写的那一版阶段文案；没有就返回 None，交给内置文案。"""
    if voice is None:
        return None
    getter = getattr(voice, "phase", None)
    if not callable(getter):
        return None
    try:
        return getter(key)
    except Exception:
        return None


def _voice_reminder(voice: object | None, kind: str) -> str:
    if voice is None:
        return ""
    getter = getattr(voice, "reminder", None)
    if not callable(getter):
        return ""
    try:
        return getter(kind) or ""
    except Exception:
        return ""


def _pick_body(state: PhaseState, limit: int = 2) -> str:
    """按当前疼痛程度挑身体感受；疼得厉害时补一句绞痛的体感，但不和阶段描述重复。"""
    chosen = list(state.phase.body)[:limit]
    if state.vector.pain >= 0.55 and not any(
        ("绞" in item or "坠" in item) for item in chosen
    ):
        chosen = ["小腹一阵一阵地绞，腰也坠得厉害", *chosen][:limit]
    return "，".join(chosen)


def render_status(profile: UserProfile, state: PhaseState, voice: object | None = None) -> str:
    vec = state.vector
    written = _voice_phase(voice, state.phase.key)
    name = (written.name if written else "") or state.phase.name
    lines = [
        f"{state.phase.emoji} {name} · 第 {state.cycle_day} 天"
        f"（周期 {state.cycle_length} 天）",
        f"`{phase_progress_text(state)}` {int(state.progress * 100)}%",
        "",
        f"身体：{(written.body if written else '') or _pick_body(state)}",
        f"情绪：{(written.mood if written else '') or vec.mood_label()}"
        f"（愉悦 {vec.valence:+.2f}｜压力 {vec.stress:.2f}｜敏感 {vec.sensitive:.2f}）",
        f"精力 {_bar(vec.energy)} {vec.energy:.2f}　亲密意愿 {_bar(vec.libido)} {vec.libido:.2f}",
    ]
    if state.pain_logged is not None:
        lines.append(
            f"疼痛：{_bar(state.pain_logged)} {state.pain_logged:.2f}"
            f"（你自己报的 {int(round(state.pain_logged * 10))}/10）"
        )
    lines.extend(["", "她现在的样子："])
    seed = f"{profile.user_key}:{state.cycle_day}:{state.phase.key}"
    beats = list(written.behaviors) if written and written.behaviors else list(state.phase.behaviors)
    lines.extend(f"· {item}" for item in _rotate(beats, 4, seed))
    if state.comforts:
        lines.append("")
        lines.append(f"刚刚：{comfort_mod.summarize(state.comforts)}")
        feeling = comfort_mod.reward_text(state.comforts)
        if feeling:
            lines.append(f"体感：{feeling}")
    lines.append("")
    if state.underway:
        lines.append(f"经期进行中：第 {state.days_since_start + 1} 天")
    if state.paused:
        lines.append("状态：观察期，先不做精确推算")
    elif state.irregular:
        lines.append(
            f"下次大概：{pretty_date(state.next_start)}前后（{human_delta(state.days_to_next)}，估的）"
        )
    else:
        lines.append(
            f"下次预计：{pretty_date(state.next_start)}（{human_delta(state.days_to_next)}）"
        )
    if state.irregular:
        lines.append(
            f"📉 最近不太规律（波动 {state.variability * 100:.0f}%），预测只能当参考。"
        )
    if state.stale:
        lines.append(
            "⚠️ 距离上次记录已经超过一个周期，推算可能不准了，来一句「经期 开始」补一条吧。"
        )
    return "\n".join(lines)


def render_forecast(profile: UserProfile, count: int = 3) -> str:
    stats = cycle_stats(profile)
    predictions = predict_next_periods(profile, count)
    if not predictions:
        return "还没有任何记录，先用「经期 开始」记一次，我才能推算。"
    state = compute_state(profile)
    soft = bool(state and state.irregular)
    lines = [
        f"📅 未来 {len(predictions)} 次{'大概' if soft else '预测'}"
        f"（按 {stats['cycle_length']} 天周期{'估的' if soft else ''}）"
    ]
    for index, (begin, end) in enumerate(predictions, start=1):
        days = (end - begin).days + 1
        begin_text = f"约 {pretty_date(begin)}前后" if soft else pretty_date(begin)
        end_text = f"{pretty_date(end)}前后" if soft else pretty_date(end)
        lines.append(
            f"{index}. {begin_text} — {end_text}（{days} 天，"
            f"{human_delta((begin - clock.today(profile)).days)}）"
        )
    lines.append("")
    if stats["median_cycle"]:
        lines.append(
            f"历史：{stats['records']} 次记录，中位周期 {stats['median_cycle']} 天"
            f"（最短 {stats['min_cycle']}｜最长 {stats['max_cycle']}｜平均 {stats['avg_cycle']}）"
        )
    else:
        lines.append(f"历史：仅 {stats['records']} 次记录，暂用默认 {stats['cycle_length']} 天推算。")
    lines.append(f"经期长度按 {stats['period_length']} 天计算。")
    if soft:
        lines.append(
            f"📉 你最近几次相差得有点多（波动 {state.variability * 100:.0f}%），"
            "上面只能当范围看；连续记几次会越来越准。"
        )
    return "\n".join(lines)


def render_history(profile: UserProfile, limit: int = 6) -> str:
    ordered = profile.sorted_periods()
    if not ordered:
        return "还没有记录。用「经期 开始」记第一天，「经期 结束」记最后一天。"
    window = ordered[-limit:]
    offset = len(ordered) - len(window)
    lines = [f"🗂 最近 {len(window)} 次记录（按时间排，序号可用于删除）"]
    for index in range(len(window) - 1, -1, -1):
        record = window[index]
        if record.end:
            span = f"{pretty_date(record.start)} — {pretty_date(record.end)}（{record.length} 天）"
        else:
            span = f"{record.start.month} 月 {record.start.day} 日开始（未结束）"
        note = f"　备注：{record.note}" if record.note else ""
        lines.append(f"{offset + index + 1}. {span}{note}")
    stats = cycle_stats(profile)
    lines.append("")
    lines.append(
        f"共 {stats['records']} 次｜周期中位数 {stats['median_cycle'] or '—'} 天"
        f"｜经期长度 {stats['period_length']} 天"
    )
    return "\n".join(lines)


def render_admin_denied() -> str:
    return (
        "这个只有管理员能动。\n"
        "把要用的号加进 AstrBot 的「管理员 ID」列表（配置里那项 admins_id），"
        "或者填到本插件的 extra_admins 里，再试一次。"
    )


_BRIEF_MOOD = {
    "irritable": "烦躁",
    "tearful": "想哭",
    "low": "低落",
    "tired": "累",
    "happy": "轻快",
    "cheerful": "有精神",
    "calm": "平静",
    "anxious": "焦虑",
    "neutral": "还行",
}


def render_brief(profile: UserProfile, state: PhaseState) -> str:
    """一行说完。群里刷屏、或者只想扫一眼的时候用。"""
    who = profile.nickname or "她"
    bits = [
        f"{state.phase.emoji} {who}｜第 {state.cycle_day}/{state.cycle_length} 天 · {state.phase.name}"
    ]
    if state.paused:
        bits.append("观察期")
    else:
        bits.append(f"还有 {state.days_to_next} 天")
    bits.append(f"疼 {state.vector.pain:.0%}")
    bits.append(_BRIEF_MOOD.get(_primary_emotion(state), "还行"))
    if state.comforts:
        bits.append("刚被哄过")
    if state.irregular:
        bits.append("不太规律")
    return " ｜ ".join(bits)


def render_help(prefix: str = "") -> str:
    p = f"{prefix}经期" if prefix else "经期"
    return "\n".join(
        [
            "🩸 生理周期 · 情绪共鸣",
            "她自己的周期，跟谁聊都是同一条。",
            "",
            "看看",
            f"　{p} 状态 [简]　　当前阶段、身体和情绪（加「简」只回一行）",
            f"　{p} 预测　　　　未来三次经期时间",
            f"　{p} 历史 [条数]　最近的记录",
            "",
            "记录",
            f"　{p} 开始 [日期]　　　　记今天（或指定日期）是第一天",
            f"　{p} 结束 [日期] [备注]　记最后一天，顺带算经期长度",
            f"　{p} 疼 7　　　　　　　　今天疼到什么程度（0-10），比推算更准",
            "",
            "调整",
            f"　{p} 设置 周期 29 / 经期 5 / 疼痛 1.2 / 情绪 1.1 / PMS 1.3",
            f"　{p} 提醒 开|关　　　　主动提醒；{p} 提醒 口吻 换她的口吻或系统口吻",
            f"　{p} 暂停 30　　　　　　进观察期（用药、出差、周期乱），{p} 恢复 解除",
            f"　{p} 文案 [写|清]　　　　让模型重写这一轮的说法，或看现在用的哪版",
            f"　{p} 同步　　　　　　　　把当前状态推给 Self Learning / Group Chat Plus",
            "",
            "清理",
            f"　{p} 删除 [序号|日期]　删掉一条错记的",
            f"　{p} 重置　　　　　　　　清空她的全部数据",
            "",
            "日期可以写 11-08、2026-11-08、11.8，不写就是今天。",
            "他递热水、抱一下、说“我来弄”这类话，会自动当成关心算进状态，几小时后淡掉。",
            "指令只认 AstrBot 里配的管理员；面板走 Dashboard 登录，不受这个限制。",
        ]
    )


# ---------------------------------------------------------------------------
# 注入给模型的状态卡
# ---------------------------------------------------------------------------

# 短句（"嗯""哈哈"）不该被一整张状态卡淹没；但一问到身体就得给足信息
_QUESTION_WORDS = ("?", "？", "吗", "呢", "怎么样", "多少", "几号", "几天", "是不是")
_BODY_WORDS = (
    "疼",
    "痛",
    "累",
    "困",
    "身体",
    "心情",
    "舒服",
    "难受",
    "月经",
    "例假",
    "生理期",
    "周期",
)

_RULE_QUIET = (
    "- 平时不主动播报：不要提状态卡、周期天数、激素、插件，也不要张口就报数值，"
    "更不要无缘无故宣布「我来例假了」。"
)
_RULE_MENTION = (
    "- 但他问你在干嘛、今天怎么样、怎么没精神、是不是不舒服时，可以顺口提一句这两天身体不太舒服"
    "——这是你自己愿意说的，不是被问出来的报告。"
)
_RULE_TRUTH = (
    "- 但对方直接问你身体、心情、疼不疼、还有几天这类事时，必须按上面的状态如实回答，"
    "不能比自己真实的状态更好。疼就说疼，没力气就说没力气，别用「挺好的」盖过去，"
    "也不要只说「有点困」就带过。"
)
_RULE_NEVER_ASK = (
    "- 不要主动说“你是不是来例假了”“是不是生理期”，也不要因为情绪起伏就解释自己的设定。"
)
_RULE_BACKGROUND = "- 情绪是背景，不是台词：该说事就说事，只是更软、更短、更少讲道理。"
_RULE_HISTORY = "- 这些是已经发生过的事，可以自然带一句「这两天都不太舒服」，但不要像念记录。"


def injection_level(message: str) -> str:
    """按这句话的轻重决定注入多少：brief / normal / full。"""
    text = (message or "").strip()
    if not text:
        return "brief"
    # 短句（哪怕带问号，比如"在吗"）只要没提到身体，就不值得铺一整张卡
    if len(text) <= 6 and not any(word in text for word in _BODY_WORDS):
        return "brief"
    if (
        len(text) > 20
        or any(word in text for word in _QUESTION_WORDS)
        or any(word in text for word in _BODY_WORDS)
    ):
        return "full"
    return "normal"


def _fit(text: str, max_chars: int) -> str:
    if max_chars and len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def _history_line(
    profile: UserProfile,
    today: date,
    days: int,
    include_numbers: bool,
) -> str:
    """把昨天、前天她的状态压成一行，让她能说出「这两天」。"""
    items: list[str] = []
    for day, old in recent_states(profile, days, today):
        label = {1: "昨天", 2: "前天"}.get((today - day).days, pretty_date(day))
        bits = [f"{label} 第 {old.cycle_day} 天", old.phase.name]
        if include_numbers:
            if old.pain_logged is not None:
                bits.append(f"疼 {old.pain_logged:.2f}（她自己报的）")
            else:
                bits.append(f"疼 {old.vector.pain:.2f}")
        else:
            bits.append("不太舒服" if old.vector.pain >= 0.4 else "还行")
        if old.comforts:
            bits.append("被哄过一次")
        items.append(" · ".join(bits))
    if not items:
        return ""
    return "最近几天（从近到远）：" + "；".join(items)


def build_injection(
    profile: UserProfile,
    state: PhaseState,
    *,
    max_chars: int = 900,
    include_numbers: bool = True,
    voice: object | None = None,
    detail: str = "full",
    self_mention: bool = True,
    history_days: int = 2,
    today: date | None = None,
) -> str:
    """把她的状态写成给模型看的状态卡。

    ``detail`` 决定信息量：``brief``（短句）只给一行身体和一条规则，
    ``normal`` 给数值与三条规则，``full`` 是完整版（历史 + 三条倾向 + 全部规则）。
    """
    level = detail if detail in ("brief", "normal", "full") else "full"
    moment = today or clock.today(profile)
    vec = state.vector
    who = profile.nickname or "对方"
    written = _voice_phase(voice, state.phase.key)
    phase_name = (written.name if written else "") or state.phase.name
    body = (written.body if written else "") or _pick_body(state, limit=3)
    mood_word = (written.mood if written else "") or vec.mood_label()
    seed = f"{profile.user_key}:{state.cycle_day}:{state.phase.key}"
    beats = _rotate(
        list(written.behaviors) if written and written.behaviors else list(state.phase.behaviors),
        3,
        seed,
    )

    if level == "brief":
        # 短句最容易得到敷衍的回复，所以这一档只保留最要紧的两件事：
        # 今天什么状态，以及「被问就必须说实话」。
        if state.paused or state.irregular:
            day_line = (
                f"她（{who}）这几天周期不太准，状态大致相当于第 {state.cycle_day} 天，"
                f"阶段：{phase_name}。"
            )
        else:
            day_line = f"她（{who}）今天第 {state.cycle_day} 天，阶段：{phase_name}。"
        pain = f"疼痛 {vec.pain:.2f}/1。" if include_numbers else ""
        return _fit(
            "\n".join(
                [
                    "【生理周期 · 内部状态卡】",
                    day_line,
                    f"身体：{body}。{pain}".rstrip(),
                    f"表达要求：{state.phase.tone}。",
                    "规则：对方直接问你身体、心情时按上面的状态如实说，别比自己真实的状态更好；"
                    "不提状态卡、周期天数、插件和数值。",
                ]
            ),
            max_chars,
        )


    if state.paused:
        head = (
            "【生理周期 · 内部状态卡】\n"
            f"她（{who}）这段时间周期不太准、在观察，今天的状态大致相当于月经周期第 "
            f"{state.cycle_day} 天（周期 {state.cycle_length} 天），阶段：{phase_name}。"
        )
    else:
        head = (
            "【生理周期 · 内部状态卡】\n"
            f"她（{who}）今天处在月经周期第 {state.cycle_day} 天，"
            f"阶段：{phase_name}，距下次月经还有 {state.days_to_next} 天。"
        )

    if include_numbers:
        mood_bits = f"愉悦 {vec.valence:+.2f}/1，压力 {vec.stress:.2f}/1"
        if level == "full":
            mood_bits += (
                f"，情绪敏感 {vec.sensitive:.2f}/1，社交欲 {vec.social:.2f}/1"
            )
        detail_line = (
            f"身体：{body}。精力 {vec.energy:.2f}/1，疼痛 {vec.pain:.2f}/1"
            + ("（她自己报的）" if state.pain_logged is not None else "")
            + f"。\n情绪：{mood_word}——{mood_bits}。"
        )
    else:
        detail_line = f"身体：{body}。\n情绪：{mood_word}。"

    blocks = [head, detail_line]

    if state.comforts:
        names = comfort_mod.summarize(state.comforts)
        if level == "full":
            feeling = comfort_mod.reward_text(state.comforts)
            line = f"刚发生过：{names}。"
            if feeling:
                line += f"你现在{feeling}，比刚才松了一截，也更愿意让他靠近。"
            else:
                line += "你被照顾到了，气顺了一些，也更愿意让他靠近。"
        else:
            line = f"刚发生过：{names}。"
        blocks.append(line)

    if level == "full" and history_days > 0:
        history = _history_line(profile, moment, history_days, include_numbers)
        if history:
            blocks.append(history)

    if level == "full":
        blocks.append("此刻的倾向：\n" + "\n".join(f"- {beat}" for beat in beats))
    else:
        blocks.append("此刻的倾向：\n- " + beats[0])

    blocks.append(f"表达要求：{state.phase.tone}。")

    rules = ["硬性规则：", _RULE_QUIET]
    if self_mention:
        rules.append(_RULE_MENTION)
    rules.append(_RULE_TRUTH)
    rules.append(_RULE_NEVER_ASK)
    if level == "full":
        rules.append(_RULE_BACKGROUND)
    if state.irregular or state.paused:
        rules.append("- 她最近的周期本身就不准，不要用「还有几天」这种精确说法。")
    if state.comforts:
        rules.append("- 他刚照顾过你：别反复追问「好点没」，也别拿这件事讨功或索取回报。")
    if level == "full" and history_days > 0:
        rules.append(_RULE_HISTORY)
    blocks.append("\n".join(rules))

    return _fit("\n".join(blocks), max_chars)


def selflearn_snapshot(profile: UserProfile, state: PhaseState) -> dict[str, object]:
    """给 Self Learning 一类外部情绪系统用的中性快照。

    字段名尽量用通用词（mood / energy / stress …），单位统一 0-1，另外附带 0-100 版本，
    方便不同实现的写入接口直接取用。
    """
    vec = state.vector
    primary = _primary_emotion(state)
    return {
        "source": "menstrual_cycle",
        "user_key": profile.user_key,
        "phase": state.phase.key,
        "phase_name": state.phase.name,
        "cycle_day": state.cycle_day,
        "cycle_length": state.cycle_length,
        "days_to_next_period": state.days_to_next,
        "emotion": primary,
        "mood": round((vec.valence + 1) / 2, 3),
        "valence": round(vec.valence, 3),
        "arousal": round(vec.arousal, 3),
        "energy": round(vec.energy, 3),
        "stress": round(vec.stress, 3),
        "pain": round(vec.pain, 3),
        "sensitivity": round(vec.sensitive, 3),
        "libido": round(vec.libido, 3),
        "social": round(vec.social, 3),
        "mood_100": int(round((vec.valence + 1) / 2 * 100)),
        "energy_100": int(round(vec.energy * 100)),
        "stress_100": int(round(vec.stress * 100)),
        "tone": state.phase.tone,
        "behaviors": list(state.phase.behaviors),
        "prompt_hint": build_injection(profile, state, max_chars=600, include_numbers=False),
    }


def _primary_emotion(state: PhaseState) -> str:
    vec = state.vector
    if state.phase.key == "pms":
        return "irritable" if vec.arousal >= 0.5 else "tearful"
    if state.phase.key == "menstrual":
        return "low" if vec.pain >= 0.5 else "tired"
    if state.phase.key == "ovulation":
        return "happy"
    if state.phase.key == "follicular":
        return "cheerful" if vec.valence > 0.3 else "calm"
    if vec.stress >= 0.6:
        return "anxious"
    return "neutral"


def render_sync_result(snapshot: dict[str, object], ok: bool, detail: str) -> str:
    status = "✅ 已同步" if ok else "⚠️ 未同步"
    lines = [
        f"{status}：{detail}",
        f"阶段 {snapshot['phase_name']}｜情绪 {snapshot['emotion']}"
        f"｜mood {snapshot['mood']}｜energy {snapshot['energy']}"
        f"｜stress {snapshot['stress']}",
    ]
    return "\n".join(lines)


def render_settings(profile: UserProfile) -> str:
    s = profile.settings
    style = "她的口吻" if s.reminder_style == "in_character" else "系统提醒"
    lines = [
        "⚙️ 当前设置",
        f"周期长度 {s.cycle_length} 天（样本不足时用这个推算）",
        f"经期长度 {s.period_length} 天",
        f"疼痛敏感度 {s.pain_sensitivity:g}　情绪幅度 {s.emotion_gain:g}　PMS 强度 {s.pms_strength:g}",
        f"主动提醒：{'开' if s.reminder else '关'}（{style}）",
    ]
    until = parse_date(s.paused_until)
    if until and until >= clock.today(profile):
        lines.append(f"观察期：到 {pretty_date(until)}，这段时间不做周期推算")
    return "\n".join(lines)


def render_reminder(
    state: PhaseState,
    kind: str,
    style: str = "in_character",
    voice: object | None = None,
) -> str:
    """主动提醒文案。kind: pre / first / pms / ending / ovulation

    in_character 是她自己发来的话，不是健康 App 的推送。默认走这个。
    voice 里有模型给这一轮写的版本就优先用，保留 {days} 占位符替换。
    """
    if style != "in_character":
        return _render_reminder_plain(state, kind)
    written = _voice_reminder(voice, kind)
    if written:
        return written.replace("{days}", str(state.days_to_next))
    if kind == "pre":
        if state.days_to_next <= 1:
            return "大概就明天了。今天已经有点坠，腰也酸，先跟你说一声。\n我这两天可能不太想说话。"
        return (
            f"还有 {state.days_to_next} 天左右吧，感觉已经在路上了。\n"
            "腰先开始酸，人也沉，脾气也上来了，你有点心理准备。"
        )
    if kind == "first":
        return "今天来了。肚子坠得厉害，我不太想动。\n有事你先担一下，别的往后放。"
    if kind == "pms":
        return "这两三天我最不好惹，我自己也知道。\n要是我突然不说话、或者说话冲，不是你的问题，让我缓一会儿就好。"
    if kind == "ovulation":
        return "今天状态好得有点离谱，突然想见你。"
    return "好像快干净了，今天有点力气了。\n下午想出去走走，别又把我按回床上。"


def _render_reminder_plain(state: PhaseState, kind: str) -> str:
    """系统提醒口吻，给需要「提醒」而不是「角色台词」的场景。"""
    if kind == "pre":
        return (
            f"提醒一下：大概 {state.days_to_next} 天后（{pretty_date(state.next_start)}）经期要来了。"
            "这两天可能会先胀、先馋、先烦，别硬扛，热水和甜的提前备着。"
        )
    if kind == "first":
        return (
            "今天第一天吧？小腹坠、腰也酸，别喝冰的。"
            "有事说一声，能推的就推掉，实在不行就躺着。"
        )
    if kind == "pms":
        return (
            "这两天大概是最难熬的那段。要是突然想哭、想发火，都不是你的问题。"
            "想安静就安静一会儿，我在。"
        )
    if kind == "ovulation":
        return "这几天精力最好，别一口气把事情排满，晚上早点睡。"
    return "经期差不多要结束了，这两天精神会慢慢回来，别一下子又把自己排满。"


def emotion_vector_of(state: PhaseState) -> EmotionVector:
    return state.vector


__all__ = [
    "build_injection",
    "emotion_vector_of",
    "render_forecast",
    "render_help",
    "render_history",
    "render_reminder",
    "render_settings",
    "render_status",
    "render_sync_result",
    "selflearn_snapshot",
]
