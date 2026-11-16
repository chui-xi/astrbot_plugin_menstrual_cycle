"""打印真实渲染结果，用来肉眼确认语气。"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import run_test as R  # noqa: E402  （复用其中的宿主 stub）

import astrbot_plugin_menstrual_cycle.core.engine as engine  # noqa: E402
import astrbot_plugin_menstrual_cycle.core.text as tt  # noqa: E402

profile = R.build_profile()
profile.nickname = "阿绫"

for label, day in [
    ("第一天", date(2026, 10, 15)),
    ("第 11 天", date(2026, 10, 25)),
    ("第 14 天", date(2026, 10, 28)),
    ("PMS", date(2026, 11, 10)),
]:
    state = engine.compute_state(profile, day)
    assert state is not None
    print("=" * 66)
    print(f"# {label}")
    print(tt.render_status(profile, state))
    print()

state = engine.compute_state(profile, date(2026, 11, 10))
assert state is not None
print("=" * 66)
print("# 注入给模型的内容（PMS，带数值）")
print(tt.build_injection(profile, state))
print()
print("=" * 66)
print("# 注入（关掉数值）")
print(tt.build_injection(profile, state, include_numbers=False))
print()
print("=" * 66)
print("# 预测")
print(tt.render_forecast(profile))
print()
print("=" * 66)
print("# 提醒文案")
for kind in ("pre", "first", "pms", "ending"):
    print(f"[{kind}] {tt.render_reminder(state, kind)}")
print()
print("=" * 66)
print("# 联动快照")
for key, value in tt.selflearn_snapshot(profile, state).items():
    print(f"{key}: {value}")

# ---------------------------------------------------------------------------
# 被哄之前 / 之后（状态真的会变）
# ---------------------------------------------------------------------------
from datetime import datetime  # noqa: E402

from astrbot_plugin_menstrual_cycle.core import comfort as cm  # noqa: E402

print()
print("=" * 66)
print("# 同一天：他说了什么，她的状态怎么变")
heat = R.build_profile()
heat.nickname = "阿绫"
day = date(2026, 11, 10)
moment = datetime(2026, 11, 10, 21, 0, 0)

before = engine.compute_state(heat, day, now=moment)
assert before is not None
print("[他说“多喝热水啊”之前]")
print(f"疼痛 {before.vector.pain:.2f}　压力 {before.vector.stress:.2f}　愉悦 {before.vector.valence:+.2f}")
print()

result = cm.register(heat, "我给你倒了杯热水，过来让我抱抱", now=moment)
print(f"[识别到关心] {result.kind}：{result.label}（{result.remaining_minutes(moment)} 分钟内有效）")
after = engine.compute_state(heat, day, now=moment)
assert after is not None
print(f"疼痛 {after.vector.pain:.2f}　压力 {after.vector.stress:.2f}　愉悦 {after.vector.valence:+.2f}")
print()
print(tt.build_injection(heat, after).split("表达要求")[0].rstrip())
print()

print("=" * 66)
print("# 她自己报“今天疼得厉害”（9/10）")
reported = R.build_profile()
reported.nickname = "阿绫"
reported.pain_logs["2026-11-10"] = 0.9
trusted = engine.compute_state(reported, day, now=moment)
assert trusted is not None
print(f"曲线原本算 {before.vector.pain:.2f}，她自报后 {trusted.vector.pain:.2f}")
print(tt.render_status(reported, trusted).split("她现在的样子")[0].rstrip())

print()
print("=" * 66)
print("# 周期不规律时")
wild = R.UserProfile(user_key="wild")
for index, start in enumerate(
    [
        date(2026, 6, 1),
        date(2026, 7, 5),
        date(2026, 8, 2),
        date(2026, 9, 20),
        date(2026, 10, 20),
    ]
):
    wild.periods.append(
        R.PeriodRecord(id=f"w{index}", start=start, end=start + R.timedelta(days=4))
    )
wild_state = engine.compute_state(wild, date(2026, 11, 16))
assert wild_state is not None
print(tt.render_status(wild, wild_state))

# ----------------------------------------------------------------------
# 模型自己写这一轮的文案
# ----------------------------------------------------------------------
import asyncio  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402

from astrbot_plugin_menstrual_cycle.core.voice import VoiceGenerator  # noqa: E402

PACK = {
    "menstrual": {
        "name": "来事那几天",
        "body": "小腹一阵一阵往下坠，腰后头像压了块石头，人懒得动",
        "mood": "没耐心，一句话说两遍就想发火",
        "behaviors": ["回消息变得很短", "不想被安排任何事", "只想缩在被子里不动"],
    },
    "follicular": {
        "name": "回血期",
        "body": "身上轻了，走路不自觉就快了，脸也有血色了",
        "mood": "松下来，愿意多说话",
        "behaviors": ["开始主动找话题", "想出门逛逛"],
    },
    "ovulation": {
        "name": "最亮的那几天",
        "body": "整个人是亮的，皮肤好，声音也比平时高一点",
        "mood": "看什么都顺眼，容易被夸到",
        "behaviors": ["会主动撩一句", "愿意答应本来想推掉的事"],
    },
    "luteal": {
        "name": "缓下来",
        "body": "胸口有点胀，晚上翻来覆去睡不实",
        "mood": "稳，但耐心薄了一层",
        "behaviors": ["话少了一点", "容易累，走两步就想坐"],
    },
    "pms": {
        "name": "最不好惹的那几天",
        "body": "小腹又开始坠，脑袋沉，特别馋甜的，眼睛也容易发酸",
        "mood": "一点就着，自己也知道过了",
        "behaviors": ["嘴上说没事", "被追问第二遍就会烦", "突然想确认你是不是还在"],
    },
    "reminders": {
        "pre": "还有{days}天吧。腰已经开始酸了，别在这时候跟我犟。",
        "first": "今天来了，别给我安排事。",
        "pms": "这两天别惹我，我自己也不好受。",
        "ovulation": "今天想见你，真的。",
        "ending": "快干净了，下午想出去走走。",
    },
}


async def show_model_voice() -> None:
    async def ask(prompt: str) -> str:
        return json.dumps(PACK, ensure_ascii=False)

    pms_state = engine.compute_state(profile, date(2026, 11, 10))
    assert pms_state is not None
    tmp = Path(tempfile.mkdtemp())
    gen = VoiceGenerator(path=tmp / "voice.json", ask=ask)
    pack = await gen.generate(profile, pms_state)
    assert pack is not None

    print("=" * 66)
    print("# 同一份数值，换成模型写的这一轮文案")
    print(tt.render_status(profile, pms_state, voice=pack))
    print()
    print("—— 她主动发的那句 ——")
    print(tt.render_reminder(pms_state, "pms", voice=pack))
    print()
    print("—— 注入长这样（节选） ——")
    block = tt.build_injection(profile, pms_state, voice=pack)
    print("\n".join(block.splitlines()[:6]))


asyncio.run(show_model_voice())


# ----------------------------------------------------------------------
# 实时生成：同一份数值，两次不同的说法
# ----------------------------------------------------------------------
LIVE_A = {
    "name": "坠着",
    "body": "小腹一直往下坠，坐久了站起来会有点发黑",
    "mood": "烦得很具体，但说不出在烦什么",
    "behaviors": ["回话只剩两三个字", "不想解释第二遍", "想找个没人的地方待着"],
    "reminder": "今天不太行，别问我怎么了。",
}
LIVE_B = {
    "name": "沉到底了",
    "body": "腰像被人从后面按着，肚子一阵一阵地绞，只想缩着",
    "mood": "委屈，明明没发生什么事",
    "behaviors": ["突然想被问一句", "被问第二遍又嫌烦", "眼泪来得没道理"],
    "reminder": "我没事，你先忙你的。",
}


async def show_realtime() -> None:
    from astrbot_plugin_menstrual_cycle.core.voice import VoiceGenerator

    seq = [LIVE_A, LIVE_B]
    seen: list[str] = []

    async def ask(prompt: str) -> str:
        seen.append(prompt)
        return json.dumps(seq.pop(0), ensure_ascii=False)

    state = engine.compute_state(profile, date(2026, 11, 10))
    assert state is not None
    gen = VoiceGenerator(ask=ask, mode="realtime", live_interval=0)

    for index in (1, 2):
        pack = await gen.live(profile, state)
        assert pack is not None
        print("=" * 66)
        print(f"# 实时第 {index} 次（同一天、同一份数值）")
        print(tt.render_status(profile, state, voice=pack))
        print()
        print("—— 她主动说的 ——")
        print(tt.render_reminder(state, "pms", voice=pack))

    print()
    print("—— 送给模型的那句话（节选） ——")
    print("\n".join(seen[0].splitlines()[:4]))


asyncio.run(show_realtime())
