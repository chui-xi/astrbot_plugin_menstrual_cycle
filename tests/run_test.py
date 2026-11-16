"""脱离 AstrBot 的本地自测：stub 宿主模块，跑纯逻辑。"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import types
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

LOG = logging.getLogger("stub")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

TMP = Path(tempfile.mkdtemp(prefix="menstrual-test-"))


def make_module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _Filter:
    def command(self, *args, **kwargs):
        def deco(fn):
            return fn

        return deco

    def on_llm_request(self, *args, **kwargs):
        def deco(fn):
            return fn

        return deco

    def on_llm_response(self, *args, **kwargs):
        def deco(fn):
            return fn

        return deco

    def llm_tool(self, *args, **kwargs):
        def deco(fn):
            return fn

        return deco


class AstrMessageEvent:
    def __init__(self, text="", sender="10001", platform="aiocqhttp", umo="aiocqhttp:FriendMessage:10001", role="admin"):
        self.message_str = text
        self.unified_msg_origin = umo
        self._sender = sender
        self._platform = platform
        self.role = role

    def is_admin(self):
        return str(self.role).strip().lower() in ("admin", "owner")

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return "小鹿"

    def get_platform_id(self):
        return self._platform

    def plain_result(self, text):
        return ("plain", text)


class Star:
    logger = LOG

    def __init__(self, context=None):
        self.context = context


class Context:
    def __init__(self):
        self.sent = []
        self.stars = {}
        self.web_apis = []
        self.config = {"admins_id": ["astrbot"]}

    def get_config(self, umo=None):
        return self.config

    def register_web_api(self, route, view_handler, methods, desc):
        self.web_apis = [
            item
            for item in self.web_apis
            if not (item[0] == route and item[2] == methods)
        ]
        self.web_apis.append((route, view_handler, methods, desc))

    def get_all_stars(self):
        return [types.SimpleNamespace(name=name, star_cls=cls, activated=True) for name, cls in self.stars.items()]

    def get_registered_star(self, name):
        cls = self.stars.get(name)
        if cls is None:
            return None
        return types.SimpleNamespace(name=name, star_cls=cls, activated=True)

    async def send_message(self, umo, chain):
        self.sent.append((umo, getattr(chain, "text", str(chain))))


class MessageChain:
    def __init__(self):
        self.text = ""

    def message(self, text):
        self.text = text
        return self


class ProviderRequest:
    def __init__(self):
        self.system_prompt = ""


class AstrBotConfig(dict):
    pass


make_module("astrbot")
make_module("astrbot.api", AstrBotConfig=AstrBotConfig, logger=LOG)
make_module("astrbot.api.event", AstrMessageEvent=AstrMessageEvent, filter=_Filter())
make_module("astrbot.api.star", Context=Context, Star=Star)
make_module("astrbot.api.provider", ProviderRequest=ProviderRequest)
make_module("astrbot.api.message_chain", MessageChain=MessageChain)



class _FakeWebRequest:
    """测试里手动摆 payload，模拟 handler 里的 request.json()。"""

    def __init__(self) -> None:
        self.payload: dict = {}
        self.raw: dict = {}
        self.query: dict = {}

    async def json(self, default=None):
        return dict(self.payload) if isinstance(self.payload, dict) else default

    async def form(self):
        return dict(self.payload)


WEB_REQUEST = _FakeWebRequest()


def _json_response(data=None, *, status_code=200, headers=None):
    return {"__status__": status_code, "data": data}


def _error_response(message, *, status_code=400, data=None, headers=None):
    return {"__status__": status_code, "message": message, "data": data}


make_module(
    "astrbot.api.web",
    request=WEB_REQUEST,
    json_response=_json_response,
    error_response=_error_response,
)
make_module(
    "astrbot.core.utils.astrbot_path",
    get_astrbot_data_path=lambda: str(TMP),
)

import astrbot_plugin_menstrual_cycle.core.engine as engine  # noqa: E402
import astrbot_plugin_menstrual_cycle.core.text as tt  # noqa: E402
from astrbot_plugin_menstrual_cycle.core import comfort as cm  # noqa: E402
from astrbot_plugin_menstrual_cycle.core.models import PeriodRecord, UserProfile  # noqa: E402
from astrbot_plugin_menstrual_cycle.core.selflearn import SelfLearningBridge  # noqa: E402
from astrbot_plugin_menstrual_cycle.core.store import CycleStore  # noqa: E402
from astrbot_plugin_menstrual_cycle.main import MenstrualCyclePlugin  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {extra}")
        FAILS.append(name)


def build_profile() -> UserProfile:
    profile = UserProfile(user_key="aiocqhttp:10001")
    for index, start in enumerate(
        [date(2026, 8, 20), date(2026, 9, 17), date(2026, 10, 15)]
    ):
        profile.periods.append(
            PeriodRecord(id=f"r{index}", start=start, end=start + timedelta(days=4))
        )
    return profile


def phase_matrix() -> None:
    print("\n[1] 五阶段映射")
    profile = build_profile()
    cases = {
        "menstrual": date(2026, 10, 16),
        "follicular": date(2026, 10, 25),
        "ovulation": date(2026, 10, 28),
        "luteal": date(2026, 11, 5),
        "pms": date(2026, 11, 10),
    }
    for expected, day in cases.items():
        state = engine.compute_state(profile, day)
        assert state is not None
        check(
            f"{expected} @ {day}",
            state.phase.key == expected,
            f"-> {state.phase.key} (cycle_day {state.cycle_day}, ref ok)",
        )

    state = engine.compute_state(profile, date(2026, 10, 15))
    assert state is not None
    check("月经第一天 underway", state.underway and state.days_since_start == 0)
    check("月经期疼痛偏高", state.vector.pain > 0.5, f"pain={state.vector.pain}")
    check("月经期精力偏低", state.vector.energy < 0.55, f"energy={state.vector.energy}")


def emotion_curve() -> None:
    print("\n[2] 情绪曲线")
    profile = build_profile()
    pains, libidos = [], []
    for offset in range(28):
        day = date(2026, 10, 15) + timedelta(days=offset)
        state = engine.compute_state(profile, day)
        assert state is not None
        pains.append(state.vector.pain)
        libidos.append(state.vector.libido)
    check("疼痛峰值在经期", max(pains[:6]) > 0.6, f"max={max(pains[:6]):.2f}")
    check("经期后疼痛回落", min(pains[8:20]) < 0.3, f"min={min(pains[8:20]):.2f}")
    check("排卵前后亲密意愿最高", max(libidos[10:18]) >= 0.7, f"max={max(libidos[10:18]):.2f}")
    check("全程都在合法区间", all(0 <= value <= 1 for value in pains + libidos))


def stability() -> None:
    print("\n[3] 同日一致 / 跨日变化")
    profile = build_profile()
    a = engine.compute_state(profile, date(2026, 11, 5))
    b = engine.compute_state(profile, date(2026, 11, 5))
    c = engine.compute_state(profile, date(2026, 11, 6))
    assert a and b and c
    check("同一天两次调用完全一致", a.vector.as_dict() == b.vector.as_dict())
    check("换一天数值有微调", a.vector.as_dict() != c.vector.as_dict())


def forecasting() -> None:
    print("\n[4] 预测与统计")
    profile = build_profile()
    stats = engine.cycle_stats(profile)
    check("中位周期 28", stats["median_cycle"] == 28, str(stats["median_cycle"]))
    check("记录数 3", stats["records"] == 3)
    preds = engine.predict_next_periods(profile, 3)
    check("预测三组", len(preds) == 3)
    check("首组在未来", preds[0][0] > date(2026, 11, 16), str(preds[0][0]))
    check("每组间隔 28 天", (preds[1][0] - preds[0][0]).days == 28)
    check("经期长度 5 天", (preds[0][1] - preds[0][0]).days == 4)

    short = build_profile()
    short.settings.cycle_length = 21
    resolved = engine.cycle_stats(short)["cycle_length"]
    check("差值不足 3 个时与设置值混合", 21 <= resolved <= 28, str(resolved))


def rendering() -> None:
    print("\n[5] 文案")
    profile = build_profile()
    state = engine.compute_state(profile, date(2026, 11, 10))
    assert state is not None

    status = tt.render_status(profile, state)
    check("状态卡含阶段名", state.phase.name in status)
    check("状态卡含下次预计", "下次预计" in status)
    check("状态卡含情绪行", "情绪：" in status)

    forecast = tt.render_forecast(profile)
    check("预测卡含中位数", "中位周期" in forecast, forecast[:60])

    history = tt.render_history(profile)
    check("历史卡带序号", "1. " in history and "3. " in history)

    injection = tt.build_injection(profile, state)
    check("注入含周期天数", "周期第" in injection)
    check("注入含硬性规则", "硬性规则" in injection)
    check("注入禁止提插件", "不要" in injection)
    check("注入长度受限", len(injection) <= 900, f"len={len(injection)}")

    brief = tt.build_injection(profile, state, include_numbers=False)
    check("关闭数值后没有小数", "0." not in brief or "/1" not in brief)

    snapshot = tt.selflearn_snapshot(profile, state)
    check("快照 mood 在 0..1", 0 <= snapshot["mood"] <= 1)
    check("快照含 phase", snapshot["phase"] == state.phase.key)
    check("快照含 prompt_hint", bool(snapshot["prompt_hint"]))

    for kind in ("pre", "first", "pms", "ending"):
        text = tt.render_reminder(state, kind)
        check(f"提醒文案 {kind}", len(text) > 10)


def settings_parsing() -> None:
    print("\n[6] 设置解析")
    plugin = MenstrualCyclePlugin.__new__(MenstrualCyclePlugin)
    plugin.config = {"cycle_scope": "user"}
    profile = build_profile()
    changes, errors = plugin._apply_settings(
        profile, "周期 29 经期 6 疼痛 1.4 情绪 1.2 pms 1.5 提醒 开"
    )
    check("六项全部生效", len(changes) == 6, str(changes))
    check("周期=29", profile.settings.cycle_length == 29)
    check("经期=6", profile.settings.period_length == 6)
    check("疼痛=1.4", abs(profile.settings.pain_sensitivity - 1.4) < 1e-9)
    check("提醒=开", profile.settings.reminder is True)
    check("没有报错", not errors, str(errors))

    changes, errors = plugin._apply_settings(profile, "周期 99 情绪 abc")
    check("超范围被拦下", not changes and len(errors) >= 1, f"{changes} {errors}")

    changes, errors = plugin._apply_settings(profile, "提醒 关")
    check("提醒可关闭", profile.settings.reminder is False)


def command_parsing() -> None:
    print("\n[7] 命令解析")
    plugin = MenstrualCyclePlugin.__new__(MenstrualCyclePlugin)
    plugin.config = {"cycle_scope": "user"}
    cases = {
        "经期 状态": ["状态"],
        "/经期 开始 11-08": ["开始", "11-08"],
        "月经 设置 周期 30": ["设置", "周期", "30"],
        "period forecast": ["forecast"],
        "经期": [],
    }
    for raw, expected in cases.items():
        got = plugin._tokens(AstrMessageEvent(raw))
        check(f"{raw!r} -> {expected}", got == expected, f"got={got}")


async def storage_flow() -> None:
    print("\n[8] 存储与记录流程")
    store = CycleStore(TMP / "cycles.json")
    await store.load()
    plugin = MenstrualCyclePlugin.__new__(MenstrualCyclePlugin)
    plugin.config = {"cycle_scope": "user"}
    plugin.store = store
    plugin.data_dir = TMP
    plugin._sent = {}

    event = AstrMessageEvent("经期 开始")
    profile = await plugin._profile_for(event)
    assert profile is not None
    await plugin._record_start(profile, date(2026, 9, 17))
    await plugin._record_start(profile, date(2026, 10, 15))
    check("两条未结束记录被自动收尾", len([r for r in profile.periods if r.end]) == 1)
    check("第一条收尾到前一天", profile.periods[0].end == date(2026, 10, 14))
    check("重复记同一天被拒绝", await plugin._record_start(profile, date(2026, 10, 15)) is False)
    check("重复记不新增记录", len(profile.periods) == 2)
    check("过期区间内的日期被拒绝", await plugin._record_start(profile, date(2026, 9, 18)) is False)

    state = engine.compute_state(profile, date(2026, 10, 15))
    check("存档后能算状态", state is not None and state.phase.key == "menstrual")

    ok, message = await plugin._record_end(profile, date(2026, 10, 19), "量大")
    check("结束记录成功", ok, message)
    reloaded = await CycleStore(TMP / "cycles.json").load()
    saved = reloaded.get(profile.user_key)
    check("落盘可读回", saved is not None and len(saved.periods) == 2)
    check("备注保留", saved is not None and saved.periods[-1].note == "量大")

    ok, message = await plugin._delete_record(profile, "1")
    check("按序号删除", ok and len(profile.periods) == 1, message)


async def bridge_behaviour() -> None:
    print("\n[9] Self Learning 联动降级")
    plugin = MenstrualCyclePlugin.__new__(MenstrualCyclePlugin)
    plugin.config = {"cycle_scope": "user"}
    plugin.context = Context()
    plugin.data_dir = TMP
    bridge = SelfLearningBridge(plugin, enabled=True)
    profile = build_profile()
    state = engine.compute_state(profile, date(2026, 11, 10))
    assert state is not None
    ok, detail = await bridge.push(tt.selflearn_snapshot(profile, state), group_id="g1")
    check("没装对方时不报错", ok is False and "affection_manager" in detail, detail)
    check("仍写出本地快照", (TMP / "emotion_snapshot.json").exists())
    check("状态文案可读", "未检测到" in bridge.status_text())
    name, intensity, desc = bridge._recipe(tt.selflearn_snapshot(profile, state))
    check("PMS 映射到 ANGRY 或 SAD", name in ("ANGRY", "SAD"), name)
    check("强度合法", 0.25 <= intensity <= 0.95, str(intensity))
    check("描述非空", bool(desc))


async def chatplus_behaviour() -> None:
    print("\n[10] Group Chat Plus 群情绪联动")
    from astrbot_plugin_menstrual_cycle.core.chatplus import ChatPlusBridge

    class FakeTracker:
        def __init__(self):
            self.moods = {}

        def get_current_mood(self, chat_id):
            return self.moods.get(chat_id, {}).get("mood", "平静")

        def get_mood_description(self, chat_id):
            return f"情绪: {self.get_current_mood(chat_id)}"

    class FakePlus:
        def __init__(self):
            self.mood_tracker = FakeTracker()

    plugin = MenstrualCyclePlugin.__new__(MenstrualCyclePlugin)
    plugin.config = {"cycle_scope": "user"}

    lonely = ChatPlusBridge(plugin, enabled=True)
    plugin.context = Context()
    check("没装对方时不报错", lonely.push({"phase": "pms"}, "g1")[0] is False)
    check("状态文案提示未检测到", "未检测到" in lonely.status_text())

    fake = FakePlus()
    plugin.context = Context()
    plugin.context.stars["astrbot_plugin_group_chat_plus"] = fake
    bridge = ChatPlusBridge(plugin, enabled=True)

    profile = build_profile()
    pms = engine.compute_state(profile, date(2026, 11, 10))
    assert pms is not None
    snapshot = tt.selflearn_snapshot(profile, pms)
    ok, detail = bridge.push(snapshot, "g1")
    check("写入成功", ok, detail)
    check("PMS 写成生气", fake.mood_tracker.moods["g1"]["mood"] == "生气", detail)
    check("强度已写入", 0.2 <= fake.mood_tracker.moods["g1"]["intensity"] <= 0.9)
    check("带 last_update", fake.mood_tracker.moods["g1"]["last_update"] > 0)
    check("读回来一致", bridge.pull("g1")["mood"] == "生气")
    check("状态文案已连接", "已连接" in bridge.status_text())

    follicular = engine.compute_state(profile, date(2026, 10, 25))
    assert follicular is not None
    bridge.push(tt.selflearn_snapshot(profile, follicular), "g2")
    check("卵泡期写成开心", fake.mood_tracker.moods["g2"]["mood"] == "开心")

    off = ChatPlusBridge(plugin, enabled=False)
    check("关闭后不写", off.push(snapshot, "g3")[0] is False)
    check("关闭状态文案", "关闭" in off.status_text())


async def injection_hook() -> None:
    print("\n[11] 提示词注入钩子")
    store = CycleStore(TMP / "hook.json")
    await store.load()
    plugin = MenstrualCyclePlugin.__new__(MenstrualCyclePlugin)
    plugin.config = {"cycle_scope": "user"}
    plugin.store = store
    plugin.data_dir = TMP
    plugin.context = Context()
    plugin._sent = {}

    event = AstrMessageEvent("经期 开始")
    profile = await plugin._profile_for(event)
    assert profile is not None
    await plugin._record_start(profile, date.today())

    req = ProviderRequest()
    req.system_prompt = "你是阿绫。"
    await plugin.inject_cycle_state(event, req)
    check("注入追加到原提示词", req.system_prompt.startswith("你是阿绫。"))
    check("注入含状态卡", "内部状态卡" in req.system_prompt)
    check("umo 已落盘", profile.last_umo == event.unified_msg_origin)

    empty_req = ProviderRequest()
    await plugin.inject_cycle_state(AstrMessageEvent("你好", sender="99999"), empty_req)
    check("无档案用户不注入", empty_req.system_prompt == "")

    plugin.config = {"enable_llm_injection": False}
    off_req = ProviderRequest()
    await plugin.inject_cycle_state(event, off_req)
    check("开关关闭后不注入", off_req.system_prompt == "")


async def reminder_logic() -> None:
    print("\n[12] 提醒判定")
    plugin = MenstrualCyclePlugin.__new__(MenstrualCyclePlugin)
    plugin.config = {"cycle_scope": "user"}
    profile = build_profile()
    cases = {
        "first": date(2026, 10, 15),
        "pre": date(2026, 11, 11),
        "pms": date(2026, 11, 10),
        "ending": date(2026, 10, 19),
    }
    for expected, day in cases.items():
        state = engine.compute_state(profile, day)
        assert state is not None
        got = plugin._decide_reminder(state)
        check(f"{day} -> {expected}", got == expected, f"got={got}")

    quiet = engine.compute_state(profile, date(2026, 10, 25))
    assert quiet is not None
    check("普通日子不提醒", plugin._decide_reminder(quiet) is None)


async def comfort_layer() -> None:
    print("\n[13] 关心 / 自报疼痛 / 规律度 / 观察期")
    now = datetime(2026, 11, 16, 20, 0, 0)

    # --- 词表识别 ---
    hot = cm.detect("我给你倒了杯热水，早点睡")
    check("识别热水", hot is not None and hot[0].kind == "hot", f"{hot}")
    hug = cm.detect("过来让我抱抱")
    check("识别拥抱", hug is not None and hug[0].kind == "hug", f"{hug}")
    pill = cm.detect("先吃颗布洛芬吧")
    check("识别止痛药", pill is not None and pill[0].kind == "medicine", f"{pill}")
    check("否定句不算关心", cm.detect("我不想喝热水") is None)
    check("普通闲聊不算", cm.detect("今天天气挺好的") is None)
    check("别喝水不算关心", cm.detect("别喝热水了") is None)

    # --- 状态真的变好 ---
    profile = build_profile()
    before = engine.compute_state(profile, date(2026, 11, 16), now=now)
    assert before is not None
    entry = cm.register(profile, "我给你倒了热水，再抱一下", now=now, hours=6)
    check("记下一条关心", entry is not None)
    after = engine.compute_state(profile, date(2026, 11, 16), now=now)
    assert after is not None
    check(
        "被哄后疼痛下降",
        after.vector.pain < before.vector.pain - 0.03,
        f"{before.vector.pain:.2f} -> {after.vector.pain:.2f}",
    )
    check(
        "被哄后压力下降",
        after.vector.stress < before.vector.stress - 0.03,
        f"{before.vector.stress:.2f} -> {after.vector.stress:.2f}",
    )
    check(
        "被哄后愉悦上升",
        after.vector.valence > before.vector.valence + 0.02,
        f"{before.vector.valence:.2f} -> {after.vector.valence:.2f}",
    )
    check("状态里带上关心", bool(after.comforts) and len(after.comforts) == 1)
    card = tt.build_injection(profile, after)
    check("注入提到刚发生的事", "刚发生过" in card)
    check("注入提醒别讨功", "别拿这件事讨功" in card)

    # --- 同类冷却与过期 ---
    again = cm.register(profile, "再喝点热水", now=now + timedelta(minutes=30))
    check("同类关心有冷却", again is None)
    other = cm.register(profile, "我抱抱你", now=now + timedelta(minutes=30))
    check("不同类可以叠加", other is not None)
    faded = engine.compute_state(profile, date(2026, 11, 16), now=now + timedelta(hours=9))
    assert faded is not None
    check("过期后自动失效", not faded.comforts)

    # --- 自报疼痛覆盖曲线 ---
    logged_profile = build_profile()
    plain = engine.compute_state(logged_profile, date(2026, 11, 16), now=now)
    assert plain is not None
    logged_profile.pain_logs["2026-11-16"] = 0.95
    logged = engine.compute_state(logged_profile, date(2026, 11, 16), now=now)
    assert logged is not None
    check(
        "自报疼痛覆盖曲线",
        logged.vector.pain > plain.vector.pain + 0.25,
        f"{plain.vector.pain:.2f} -> {logged.vector.pain:.2f}",
    )
    check("自报疼痛被记住", abs((logged.pain_logged or 0) - 0.95) < 1e-6)
    check("状态卡标出她自己报的", "她自己报的" in tt.build_injection(logged_profile, logged))
    quiet_pain = build_profile()
    quiet_pain.pain_logs["2026-11-16"] = 0.0
    calm = engine.compute_state(quiet_pain, date(2026, 11, 16), now=now)
    assert calm is not None
    check("说不疼就真的不疼", calm.vector.pain < 0.2, f"pain={calm.vector.pain:.2f}")

    # --- 规律度 ---
    cv, irregular = engine.cycle_variability(build_profile())
    check("规律周期不报警", not irregular, f"cv={cv}")
    wild = UserProfile(user_key="wild")
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
            PeriodRecord(id=f"w{index}", start=start, end=start + timedelta(days=4))
        )
    wild_cv, wild_flag = engine.cycle_variability(wild)
    check("乱周期会报警", wild_flag, f"cv={wild_cv}")
    wild_state = engine.compute_state(wild, date(2026, 11, 16), now=now)
    assert wild_state is not None
    check("乱周期写进状态", wild_state.irregular)
    check("乱周期注入别说死", "不能当铁律" in tt.build_injection(wild, wild_state) or "本身就不准" in tt.build_injection(wild, wild_state))
    wild_card = tt.render_status(wild, wild_state)
    check("乱周期不写死日期", "下次预计" not in wild_card and "下次大概" in wild_card)

    # --- 观察期 ---
    paused = build_profile()
    paused.settings.paused_until = (date(2026, 11, 16) + timedelta(days=30)).isoformat()
    pstate = engine.compute_state(paused, date(2026, 11, 16), now=now)
    assert pstate is not None
    check("观察期标记生效", pstate.paused)
    paused_card = tt.build_injection(paused, pstate)
    check("观察期不报距下次", "距下次月经还有" not in paused_card)
    check("观察期写明在观察", "在观察" in paused_card)
    expired_pause = build_profile()
    expired_pause.settings.paused_until = "2026-11-01"
    check(
        "观察期到期自动结束",
        not engine.compute_state(expired_pause, date(2026, 11, 16), now=now).paused,
    )

    # --- 提醒口吻 ---
    state = engine.compute_state(build_profile(), date(2026, 11, 11), now=now)
    assert state is not None
    soft = tt.render_reminder(state, "pre")
    hard = tt.render_reminder(state, "pre", "reminder")
    check("默认用她的口吻", "大概就明天了" in soft, soft[:24])
    check("系统口吻仍可用", "提醒一下" in hard, hard[:24])
    check("两种口吻不一样", soft != hard)
    first_soft = tt.render_reminder(state, "first")
    check("第一天的口吻像她说话", "不太想动" in first_soft, first_soft[:24])


async def behaviour_rotation() -> None:
    print("\n[14] 文案轮换")
    profile = build_profile()
    day = date(2026, 11, 5)
    state = engine.compute_state(profile, day)
    assert state is not None
    first = tt.render_status(profile, state)
    second = tt.render_status(profile, state)
    check("同一天两次渲染一致", first == second)
    seen = set()
    for offset in range(14):
        probe = engine.compute_state(profile, day + timedelta(days=offset))
        assert probe is not None
        seen.add(tt.render_status(profile, probe))
    check("十四天里说法有变化", len(seen) >= 10, f"distinct={len(seen)}")
    beats = [b for b in engine.PHASE_SPECS["pms"].behaviors]
    check("每阶段至少六条素材", len(beats) >= 6, f"n={len(beats)}")


async def llm_voice() -> None:
    print("\n[15] 模型自己写文案")
    import json as _json
    import tempfile
    from pathlib import Path

    from astrbot_plugin_menstrual_cycle.core.voice import VoiceGenerator

    payload = {
        "menstrual": {
            "name": "经期",
            "body": "小腹坠着，腰像被人从后面往后拽",
            "mood": "烦得没耐心，但还没到发火",
            "behaviors": ["说话变短，能一个字说完就不用两个", "不想被安排事情", "想被人盖好被子就走开"],
        },
        "follicular": {
            "name": "回血中",
            "body": "身上轻了，走路都比前几天快",
            "mood": "松下来了，愿意搭理人",
            "behaviors": ["开始主动找话", "想出门"],
        },
        "ovulation": {
            "name": "高峰",
            "body": "整个人亮着，声音都比平时高",
            "mood": "看什么都顺眼",
            "behaviors": ["想被夸", "容易答应事情"],
        },
        "luteal": {
            "name": "缓坡",
            "body": "胸有点胀，晚上睡不实",
            "mood": "稳，但耐心薄了一层",
            "behaviors": ["话少了一点", "容易累"],
        },
        "pms": {
            "name": "最难受那几天",
            "body": "小腹又开始坠，头也沉，特别馋甜的",
            "mood": "一点就着，自己也知道",
            "behaviors": ["嘴上说没事", "被追问会烦"],
        },
        "reminders": {
            "pre": "还有{days}天吧，腰已经开始酸了。",
            "first": "今天来了，别给我安排事。",
            "pms": "这两天别惹我，我自己也不好受。",
            "ovulation": "今天想见你，真的。",
            "ending": "快干净了，下午想出去走走。",
        },
    }
    raw = "好的，这是文案：\n```json\n" + _json.dumps(payload, ensure_ascii=False) + "\n```"
    calls = {"n": 0}

    async def fake_ask(prompt: str) -> str:
        calls["n"] += 1
        assert "JSON" in prompt or "json" in prompt
        return raw

    profile = build_profile()
    state = engine.compute_state(profile, date(2026, 10, 16))
    assert state is not None
    assert state.phase.key == "menstrual"

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "voice_cache.json"
        gen = VoiceGenerator(path=path, ask=fake_ask, timeout=5)
        check("没有缓存时不冒充有", gen.cached(profile, state) is None)

        pack = await gen.generate(profile, state)
        check("一次生成拿到整套", pack is not None and pack.coverage() == 1.0, f"{pack}")
        assert pack is not None
        check("模型调用只发生一次", calls["n"] == 1)
        check("有缓存后能命中", gen.cached(profile, state) is pack)

        card = tt.render_status(profile, state, voice=pack)
        check("状态卡用了模型写的身体感受", "腰像被人从后面往后拽" in card)
        check("状态卡用了模型写的情绪", "烦得没耐心" in card)
        check("状态卡不再念内置那句", "小腹坠胀、隐隐绞痛" not in card)

        block = tt.build_injection(profile, state, voice=pack)
        check("注入用了模型写的文案", "腰像被人从后面往后拽" in block)
        check("注入的阶段名也换了", "经期" in block)

        line = tt.render_reminder(state, "first", voice=pack)
        check("提醒换成模型写的", line.startswith("今天来了，别给我安排事"), line)
        pre = tt.render_reminder(state, "pre", voice=pack)
        check("占位符被换成真实天数", "{days}" not in pre and str(state.days_to_next) in pre, pre)

        # 落盘：换个实例还能读到
        again = VoiceGenerator(path=path, ask=fake_ask, timeout=5)
        reloaded = again.cached(profile, state)
        check("文案能落盘读回", reloaded is not None and reloaded.phase("pms") is not None)

        # 换周期 → 作废
        shifted = build_profile()
        shifted.periods.append(
            PeriodRecord(
                id="next",
                start=date(2026, 11, 4),
                end=date(2026, 11, 8),
            )
        )
        new_state = engine.compute_state(shifted, date(2026, 11, 9))
        assert new_state is not None
        check("换周期后旧文案失效", again.cached(shifted, new_state) is None)

        # 坏输出 → 回落
        async def broken(prompt: str) -> str:
            return "我写不出来"

        bad = VoiceGenerator(path=Path(tmp) / "b.json", ask=broken, timeout=5)
        check("模型乱答时返回空", await bad.generate(profile, state) is None)
        fallback = tt.render_status(profile, state, voice=bad.cached(profile, state))
        check("失败后回落内置文案", "小腹" in fallback)

        # 只有一个阶段也算失败
        thin = {"menstrual": payload["menstrual"], "reminders": {"first": "来了"}}

        async def partial(prompt: str) -> str:
            return _json.dumps(thin, ensure_ascii=False)

        half = VoiceGenerator(path=Path(tmp) / "c.json", ask=partial, timeout=5)
        check("阶段太少当失败", await half.generate(profile, state) is None)

        # 清洗：超长截断 + 去掉医学词
        dirty = {
            "menstrual": {
                "body": "激素" + "小腹很坠" * 40,
                "mood": "烦",
                "behaviors": ["说话很短"],
            },
            "pms": {"body": "沉的", "mood": "烦", "behaviors": ["不想说话"]},
            "reminders": {"first": "来了"},
        }

        async def messy(prompt: str) -> str:
            return _json.dumps(dirty, ensure_ascii=False)

        clean = VoiceGenerator(path=Path(tmp) / "d.json", ask=messy, timeout=5)
        dirty_pack = await clean.generate(profile, state)
        assert dirty_pack is not None
        body = dirty_pack.phase("menstrual").body
        check("超长被截断", len(body) <= 72, f"len={len(body)}")
        check("医学词被清掉", "激素" not in body)
        check("部分覆盖也算可用", 0 < dirty_pack.coverage() < 1.0)

        # 关掉就不生成
        off = VoiceGenerator(path=Path(tmp) / "e.json", enabled=False, ask=fake_ask)
        check("关闭时不生成", await off.generate(profile, state) is None)
        check("关闭时报告写明", "关闭" in off.status_text(profile, state))

        # 不阻塞：只排后台
        queued = VoiceGenerator(path=Path(tmp) / "f.json", ask=fake_ask, timeout=5)
        note = queued.status_text(profile, state)
        check("没写之前报告说没写", "还没写" in note or "正在写" in note, note)


async def realtime_voice() -> None:
    print("\n[16] 实时生成（调用对话模型现写）")
    import json as _json
    import tempfile
    from pathlib import Path

    from astrbot_plugin_menstrual_cycle.core.voice import VoiceGenerator

    calls = {"n": 0}

    async def ask(prompt: str) -> str:
        calls["n"] += 1
        assert "第一人称" in prompt
        return _json.dumps(
            {
                "name": "坠着那几天",
                "body": "小腹一直往下坠，坐久了站起来会眼前发黑",
                "mood": "烦，但说不出在烦什么",
                "behaviors": ["回话只剩两三个字", "不想解释", "想找个没人的地方待着"],
                "reminder": "今天不太行，别问我怎么了。",
            },
            ensure_ascii=False,
        )

    profile = build_profile()
    state = engine.compute_state(profile, date(2026, 10, 16))
    assert state is not None and state.phase.key == "menstrual"

    with tempfile.TemporaryDirectory() as tmp:
        gen = VoiceGenerator(
            path=Path(tmp) / "v.json", ask=ask, mode="realtime", live_interval=25
        )
        check("实时模式自报身份", gen.mode() == "realtime")

        pack = await gen.live(profile, state)
        check("现场写出一套", pack is not None and pack.phase("menstrual") is not None)
        assert pack is not None
        check("写的是当前阶段", "坠着那几天" == pack.phase("menstrual").name)
        check(
            "带上了此刻那句话",
            (pack.reminder("menstrual") or "").startswith("今天不太行"),
            str(pack.reminder("menstrual")),
        )
        check("只调了一次模型", calls["n"] == 1)

        again = await gen.live(profile, state)
        check("窗口内复用不重复调", calls["n"] == 1 and again is pack, f"n={calls['n']}")

        forced = await gen.live(profile, state, force=True)
        check("强制刷新会再调", calls["n"] == 2 and forced is not None)

        card = tt.render_status(profile, state, voice=pack)
        check("状态卡用实时文案", "站起来会眼前发黑" in card)
        check("状态卡不再念内置", "小腹坠胀、隐隐绞痛" not in card)

        hint_calls: list[str] = []

        async def hint_ask(prompt: str) -> str:
            hint_calls.append(prompt)
            return _json.dumps({"body": "不太想说话", "mood": "烦", "reminder": "嗯。"}, ensure_ascii=False)

        hint_gen = VoiceGenerator(path=Path(tmp) / "h.json", ask=hint_ask, mode="realtime")
        await hint_gen.live(profile, state, hint="我回来啦，今天怎么样")
        check("把对方那句话带上", "我回来啦" in hint_calls[0])

        # 提醒：要的那一类没有时，用通用那句兜底
        kind_pack = await gen.live(profile, state, kind="pms", force=True)
        assert kind_pack is not None
        check("提醒的类别兜底", bool(kind_pack.reminder("pms")))
        line = await gen.live_reminder(profile, state, "first")
        check("live_reminder 给出一句", bool(line), str(line))

        # 失败：模型空答 / 答非所问
        async def empty(prompt: str) -> str:
            return ""

        broken = VoiceGenerator(path=Path(tmp) / "b.json", ask=empty, mode="realtime")
        check("空答返回 None", await broken.live(profile, state) is None)
        check("失败后状态卡回落内置", "小腹" in tt.render_status(profile, state, voice=None))

        # 关掉 / 间隔 0
        off = VoiceGenerator(path=Path(tmp) / "o.json", ask=ask, mode="off")
        check("关闭模式不生成", off.mode() == "off" and await off.live(profile, state) is None)

        zero_calls = {"n": 0}

        async def counting(prompt: str) -> str:
            zero_calls["n"] += 1
            return _json.dumps({"body": "坠", "mood": "烦", "reminder": "嗯"}, ensure_ascii=False)

        zero = VoiceGenerator(
            path=Path(tmp) / "z.json", ask=counting, mode="realtime", live_interval=0
        )
        await zero.live(profile, state)
        await zero.live(profile, state)
        check("间隔 0 时每次都写", zero_calls["n"] == 2)

        # 插件层的开关：injection 场景默认不实时
        plugin = MenstrualCyclePlugin.__new__(MenstrualCyclePlugin)
        plugin.__dict__["config"] = {"voice_mode": "realtime"}
        plugin.__dict__["_voice"] = gen
        check("实时模式下提醒/查询实时", plugin._realtime() is True)
        check("注入默认不实时（省一次调用）", plugin._realtime("injection") is False)
        plugin.__dict__["config"] = {"voice_mode": "realtime", "realtime_injection": True}
        check("打开后注入也实时", plugin._realtime("injection") is True)
        plugin.__dict__["config"] = {"voice_mode": "cycle"}
        check("配置切回周期模式就关掉", plugin._realtime() is False)

        check("实时模式报告写明", "实时" in gen.status_text(profile, state))


async def dashboard_api() -> None:
    print("\n[17] Web 控制台接口")
    import json as _json
    import tempfile
    from pathlib import Path as _Path

    from astrbot_plugin_menstrual_cycle.core.store import CycleStore
    from astrbot_plugin_menstrual_cycle.core.voice import VoiceGenerator
    from astrbot_plugin_menstrual_cycle.core.webapi import DashboardAPI

    key = "aiocqhttp:10001"

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = _Path(tmp)
        context = Context()
        plugin = MenstrualCyclePlugin(context, AstrBotConfig({"cycle_scope": "user"}))
        plugin.data_dir = tmpdir
        plugin.store = CycleStore(tmpdir / "cycles.json")
        await plugin.store.load()
        await plugin.store.upsert(build_profile())

        api = DashboardAPI(plugin)

        # --- 路由注册
        routes = api.routes()
        check("注册 10 条路由", len(routes) == 10, str(len(routes)))
        check(
            "路由都带插件名前缀",
            all(route[0].startswith("/astrbot_plugin_menstrual_cycle/") for route in routes),
        )
        check("有动态路径解析", any("<path:user_key>" in route[0] for route in routes))
        registered = api.register()
        check("注册进 context", registered and len(context.web_apis) == 10, str(len(context.web_apis)))
        check(
            "方法列表正确",
            all(route[2] in (["GET"], ["POST"]) for route in context.web_apis),
        )

        check("有 query 版详情路由", any(r[0].endswith("/detail") for r in routes))
        check("有 body 版写操作路由", any(r[0].endswith("/action") for r in routes))

        # --- query 版详情：user_key 走 query，不碰路径编码
        WEB_REQUEST.query = {"key": key}
        q_detail = await api.detail_query()
        check("query 版能拿到详情", q_detail["__status__"] == 200 and q_detail["data"]["ok"] is True)
        check("query 版是同一个人", q_detail["data"]["user"]["user_key"] == key)
        WEB_REQUEST.query = {}
        q_missing = await api.detail_query()
        check("query 版缺 key 会被拦", q_missing["__status__"] == 400)

        # --- body 版写操作：op 决定走哪个 handler
        WEB_REQUEST.payload = {"key": key, "op": "state", "pain": 4}
        a_state = await api.action()
        check("body 版能写状态", a_state["__status__"] == 200 and a_state["data"]["ok"] is True)
        check("body 版回执带改动", any("疼" in item for item in a_state["data"]["changes"]))
        WEB_REQUEST.payload = {"key": key, "op": "state", "pain": ""}
        await api.action()
        WEB_REQUEST.payload = {"key": key, "op": "nope"}
        a_bad = await api.action()
        check("body 版未知操作被拦", a_bad["__status__"] == 400)
        WEB_REQUEST.payload = {"op": "state", "pain": 3}
        a_nokey = await api.action()
        check("body 版缺 key 被拦", a_nokey["__status__"] == 400)

        # --- 总览
        WEB_REQUEST.payload = {}
        res = await api.overview()
        payload = res["data"]
        check("总览成功", payload["ok"] is True)
        check("总览有档案", len(payload["users"]) >= 1, str(len(payload["users"])))
        user = payload["users"][0]
        check("总览带阶段信息", bool(user.get("phase")) and bool(user.get("emoji")))
        check("总览带记录数", user["records"] == 3, str(user["records"]))
        check("总览带联动状态", "selflearn" in payload["links"] and "chatplus" in payload["links"])
        check("总览带插件版本", payload["plugin"]["version"] == plugin.version, payload["plugin"]["version"])
        check("总览统计在周期内", payload["stats"]["active"] >= 1)
        check("总览带平台列表", isinstance(payload.get("platforms"), list), str(payload.get("platforms")))

        # --- 新建档案（空面板上什么都不能点，所以要有这个入口）
        WEB_REQUEST.payload = {
            "platform": "aiocqhttp",
            "sender": "88888",
            "nickname": "小雨",
            "start": "2026-10-02",
        }
        res = await api.create_user()
        check("面板能新建档案", res["__status__"] == 200 and res["data"]["ok"], str(res)[:160])
        made = await plugin.store.get("aiocqhttp:88888")
        check("新档案落盘", made is not None and made.nickname == "小雨")
        check(
            "带上第一条记录",
            made is not None
            and len(made.periods) == 1
            and made.periods[0].start.isoformat() == "2026-10-02",
            str(made.periods if made else None),
        )
        check("回执里有详情", isinstance(res["data"].get("detail"), dict))

        WEB_REQUEST.payload = {"platform": "aiocqhttp", "sender": "88888"}
        res = await api.create_user()
        check("重复建档被拦", res["__status__"] == 400 and "已经在了" in res["message"])

        WEB_REQUEST.payload = {"platform": "aiocqhttp"}
        res = await api.create_user()
        check("缺用户 ID 被拦", res["__status__"] == 400 and "用户 ID" in res["message"])

        WEB_REQUEST.payload = {"platform": "aiocqhttp", "sender": "77777", "start": "2099-01-01"}
        res = await api.create_user()
        check("未来开始日被拦", res["__status__"] == 400 and "未来" in res["message"])

        WEB_REQUEST.payload = {"user_key": "telegram:55555", "nickname": "阿遥"}
        res = await api.create_user()
        check("完整会话键能直接建", res["__status__"] == 200, str(res)[:160])
        made = await plugin.store.get("telegram:55555")
        check("平台从键里取", made is not None and made.platform == "telegram")
        check(
            "没填开始日就按默认起点",
            made is not None
            and len(made.periods) == 1
            and made.periods[0].start <= date.today(),
            str(len(made.periods) if made else None),
        )

        # 只有一个平台时，只填用户 ID 也能推断出来
        class _Meta:
            type = "aiocqhttp"

        class _Inst:
            id = "aiocqhttp"

            @staticmethod
            def meta():
                return _Meta()

        class _PM:
            platform_insts = [_Inst()]

        context.platform_manager = _PM()
        WEB_REQUEST.payload = {"sender": "66666"}
        res = await api.create_user()
        check(
            "只有一个平台时自动推断",
            res["__status__"] == 200,
            str(res)[:160],
        )
        check("推断出的键对", (await plugin.store.get("aiocqhttp:66666")) is not None)

        # --- 详情
        # 先制造一次「被哄过」，确认 comforts 能序列化（Comfort.at 是字符串，
        # 曾经因为按 datetime 调 strftime 把整个详情接口打挂）。
        profile_now = build_profile()
        cm.register(profile_now, "我给你倒了杯热水，过来抱抱", hours=6)
        await plugin.store.upsert(profile_now)

        res = await api.user_detail(key)
        detail = res["data"]
        check("详情成功", detail["ok"] is True)
        check("详情有状态向量", detail["state"] is not None and "pain" in detail["state"])
        check("详情有 3 条记录", len(detail["records"]) == 3, str(len(detail["records"])))
        check("记录带开始日", detail["records"][0]["start"] == "2026-10-15")
        check("详情有设置", detail["settings"]["cycle_length"] == 28)
        check("详情有统计", detail["stats"]["records"] == 3)
        check("详情有三次预测", len(detail["predictions"]) == 3, str(len(detail["predictions"])))
        check("详情有文案状态", "mode" in detail["voice"] and "status" in detail["voice"])
        comforts = detail["state"]["comforts"]
        check("详情带被哄记录", len(comforts) >= 1, str(comforts))
        check(
            "被哄时间是 mm-dd hh:mm",
            bool(comforts) and len(comforts[0]["at"]) == 11 and comforts[0]["at"][2] == "-",
            str(comforts[:1]),
        )
        check("被哄还剩分钟数", bool(comforts) and comforts[0]["minutes"] > 0)

        res = await api.user_detail("nobody:404")
        check("不存在的用户 404", res["__status__"] == 404, str(res))

        # --- 记一条开始
        WEB_REQUEST.payload = {"action": "start", "date": "2026-11-16"}
        res = await api.record_action(key)
        check("面板记开始", res["__status__"] == 200 and res["data"]["ok"], str(res)[:120])
        check("回执里有消息", "记下了" in res["data"]["message"])
        profile = await plugin.store.get(key)
        assert profile is not None
        check("记录变成 4 条", len(profile.periods) == 4, str(len(profile.periods)))
        check("回执自带最新详情", res["data"]["detail"]["records"][0]["start"] == "2026-11-16")

        # 同一天再记一次不该重复
        res = await api.record_action(key)
        check("同一天不重复记", res["__status__"] == 400 and "已经" in res["message"])

        # --- 记结束
        WEB_REQUEST.payload = {"action": "end", "date": "2026-11-16"}
        res = await api.record_action(key)
        check("面板记结束", res["__status__"] == 200 and res["data"]["ok"], str(res)[:120])
        profile = await plugin.store.get(key)
        assert profile is not None
        check("记录被收尾", profile.latest().end is not None)

        # --- 删除
        target_id = profile.latest().id
        WEB_REQUEST.payload = {"action": "delete", "id": target_id}
        res = await api.record_action(key)
        check("面板删记录", res["__status__"] == 200 and res["data"]["ok"], str(res)[:120])
        profile = await plugin.store.get(key)
        assert profile is not None
        check("记录回到 3 条", len(profile.periods) == 3, str(len(profile.periods)))

        WEB_REQUEST.payload = {"action": "delete", "id": "nope"}
        res = await api.record_action(key)
        check("删不存在的记录被拦", res["__status__"] == 400)

        WEB_REQUEST.payload = {"action": "wat"}
        res = await api.record_action(key)
        check("不认识的动作被拦", res["__status__"] == 400)

        # --- 补录/改写历史
        WEB_REQUEST.payload = {
            "action": "create",
            "start": "2026-07-23",
            "end": "2026-07-27",
            "note": "补的",
        }
        res = await api.record_action(key)
        check("补录历史成功", res["__status__"] == 200 and res["data"]["ok"], str(res)[:140])
        profile = await plugin.store.get(key)
        assert profile is not None
        check("补录后 4 条", len(profile.periods) == 4, str(len(profile.periods)))
        check("补录的排在最前", profile.periods[0].start.isoformat() == "2026-07-23")
        check("补录带备注", profile.periods[0].note == "补的")
        check("补录算出 5 天", profile.periods[0].length == 5, str(profile.periods[0].length))

        WEB_REQUEST.payload = {"action": "create", "start": "2026-12-01"}
        res = await api.record_action(key)
        check("补录未来日被拦", res["__status__"] == 400 and "未来" in res["message"])

        WEB_REQUEST.payload = {"action": "create", "start": "2026-10-14", "end": "2026-10-18"}
        res = await api.record_action(key)
        check("补录重叠被拦", res["__status__"] == 400 and "重叠" in res["message"])

        WEB_REQUEST.payload = {"action": "create", "start": "2026-09-01", "end": "2026-08-30"}
        res = await api.record_action(key)
        check("结束早于开始被拦", res["__status__"] == 400 and "早于" in res["message"])

        WEB_REQUEST.payload = {"action": "create", "start": "2026-06-01", "end": "2026-06-30"}
        res = await api.record_action(key)
        check("跨度过长被拦", res["__status__"] == 400 and "15 天" in res["message"])

        target = profile.periods[0]
        WEB_REQUEST.payload = {
            "action": "update",
            "id": target.id,
            "start": "2026-07-24",
            "end": "2026-07-28",
            "note": "改过了",
        }
        res = await api.record_action(key)
        check("改记录成功", res["__status__"] == 200 and res["data"]["ok"], str(res)[:140])
        profile = await plugin.store.get(key)
        assert profile is not None
        changed = next((r for r in profile.periods if r.id == target.id), None)
        check("起始日改了", changed is not None and changed.start.isoformat() == "2026-07-24")
        check("备注改了", changed is not None and changed.note == "改过了")

        WEB_REQUEST.payload = {"action": "update", "id": "nope", "start": "2026-07-24"}
        res = await api.record_action(key)
        check("改不存在的记录被拦", res["__status__"] == 400 and "没找到" in res["message"])

        WEB_REQUEST.payload = {
            "action": "update",
            "id": target.id,
            "start": "2026-10-16",
            "end": "2026-10-17",
        }
        res = await api.record_action(key)
        check("改成和别人重叠被拦", res["__status__"] == 400 and "重叠" in res["message"])

        # --- 直接在面板上改状态
        today = date.today().isoformat()
        WEB_REQUEST.payload = {"pain": 8}
        res = await api.state_action(key)
        check("面板写疼痛", res["__status__"] == 200 and res["data"]["ok"], str(res)[:140])
        profile = await plugin.store.get(key)
        assert profile is not None
        check("疼痛落盘 0.8", abs(profile.pain_logs.get(today, 0) - 0.8) < 1e-6)
        check("回执里有中文说明", any("疼痛" in item for item in res["data"]["changes"]))

        WEB_REQUEST.payload = {"pain": 11}
        res = await api.state_action(key)
        check("疼痛超范围被拦", res["__status__"] == 400 and "0～10" in res["message"])

        WEB_REQUEST.payload = {"pain": ""}
        res = await api.state_action(key)
        profile = await plugin.store.get(key)
        assert profile is not None
        check("疼痛被清掉", today not in profile.pain_logs)

        WEB_REQUEST.payload = {"comfort": "hug", "comfort_hours": 3}
        res = await api.state_action(key)
        check("面板加被哄", res["__status__"] == 200 and res["data"]["ok"], str(res)[:140])
        profile = await plugin.store.get(key)
        assert profile is not None
        check(
            "被哄记进去了",
            bool(profile.comforts) and profile.comforts[-1].kind == "hug",
            f"{len(profile.comforts)} {[c.kind for c in profile.comforts]} {res['data'].get('changes')}",
        )
        check(
            "详情带类别选项",
            any(item["kind"] == "medicine" for item in res["data"]["detail"]["comfort_options"]),
        )

        WEB_REQUEST.payload = {"comfort": "nope"}
        res = await api.state_action(key)
        check("不认识的类别被拦", res["__status__"] == 400 and "不认识" in res["message"])

        WEB_REQUEST.payload = {"comfort": ""}
        res = await api.state_action(key)
        profile = await plugin.store.get(key)
        assert profile is not None
        check("被哄记录清空", profile.comforts == [])

        WEB_REQUEST.payload = {"cycle_day": 3}
        res = await api.state_action(key)
        check("面板校正周期日", res["__status__"] == 200 and res["data"]["ok"], str(res)[:140])
        profile = await plugin.store.get(key)
        assert profile is not None
        latest = profile.latest()
        check("第 3 天对上今天", (date.today() - latest.start).days == 2, str(latest.start))

        WEB_REQUEST.payload = {"cycle_day": 99}
        res = await api.state_action(key)
        check("周期日超范围被拦", res["__status__"] == 400)

        WEB_REQUEST.payload = {"pause": 30}
        res = await api.state_action(key)
        check("面板设观察期", res["__status__"] == 200 and res["data"]["ok"], str(res)[:140])
        profile = await plugin.store.get(key)
        assert profile is not None
        check("观察期生效", profile.paused())

        WEB_REQUEST.payload = {"pause": ""}
        res = await api.state_action(key)
        profile = await plugin.store.get(key)
        assert profile is not None
        check("观察期解除", not profile.paused())

        WEB_REQUEST.payload = {}
        res = await api.state_action(key)
        check("空改动被拦", res["__status__"] == 400 and "没有要改" in res["message"])

        # --- 设置
        WEB_REQUEST.payload = {
            "cycle_length": 30,
            "period_length": 4,
            "pain_sensitivity": 1.4,
            "pms_strength": 1.6,
            "reminder": False,
            "reminder_style": "reminder",
        }
        res = await api.save_settings(key)
        check("保存设置成功", res["__status__"] == 200 and res["data"]["ok"], str(res)[:160])
        profile = await plugin.store.get(key)
        assert profile is not None
        check("周期长度写成 30", profile.settings.cycle_length == 30, str(profile.settings.cycle_length))
        check("经期长度写成 4", profile.settings.period_length == 4)
        check("疼痛敏感度写成 1.4", abs(profile.settings.pain_sensitivity - 1.4) < 1e-6)
        check("提醒被关掉", profile.settings.reminder is False)
        check("口吻换成系统", profile.settings.reminder_style == "reminder")
        check("改动列表非空", len(res["data"]["changes"]) >= 6, str(res["data"]["changes"]))

        WEB_REQUEST.payload = {"cycle_length": 99}
        res = await api.save_settings(key)
        check("超范围被拒绝", res["__status__"] == 400 and "15" in res["message"], str(res)[:120])

        WEB_REQUEST.payload = {"reminder_style": "robot"}
        res = await api.save_settings(key)
        check("非法口吻被拒绝", res["__status__"] == 400)

        WEB_REQUEST.payload = {"paused_until": "2026-12-05"}
        res = await api.save_settings(key)
        profile = await plugin.store.get(key)
        assert profile is not None
        check("观察期写进去", profile.settings.paused_until == "2026-12-05")
        check("观察期生效", profile.paused(date(2026, 11, 20)) is True)

        WEB_REQUEST.payload = {"paused_until": ""}
        res = await api.save_settings(key)
        profile = await plugin.store.get(key)
        assert profile is not None
        check("观察期能清掉", profile.settings.paused_until == "")

        WEB_REQUEST.payload = {"paused_until": "不是日期"}
        res = await api.save_settings(key)
        check("非法日期被拒绝", res["__status__"] == 400)

        # --- 文案
        plugin.__dict__["_voice"] = VoiceGenerator(
            plugin, enabled=False, path=tmpdir / "off.json"
        )
        WEB_REQUEST.payload = {"action": "write"}
        res = await api.rewrite_voice(key)
        check("关掉文案时拒绝并说明", res["__status__"] == 400 and "enable_llm_voice" in res["message"], str(res)[:160])

        async def ask(prompt: str) -> str:
            return _json.dumps(
                {
                    "menstrual": {
                        "name": "坠着",
                        "body": "小腹往下坠，腰也发酸",
                        "mood": "没什么力气",
                        "behaviors": ["不想说话", "想躺着"],
                    },
                    "pms": {
                        "name": "炸毛",
                        "body": "胸口闷，看什么都不顺眼",
                        "mood": "烦",
                        "behaviors": ["容易急"],
                    },
                    "reminders": {"pre": "这两天有点不对劲", "pms": "别惹我"},
                },
                ensure_ascii=False,
            )

        plugin.__dict__["_voice"] = VoiceGenerator(
            plugin, enabled=True, path=tmpdir / "on.json", ask=ask, mode="cycle"
        )
        WEB_REQUEST.payload = {"action": "write"}
        res = await api.rewrite_voice(key)
        check("重写文案成功", res["__status__"] == 200 and res["data"]["ok"], str(res)[:200])
        check("回执带覆盖率", "覆盖" in res["data"]["message"], res["data"].get("message", ""))

        WEB_REQUEST.payload = {"action": "clear"}
        res = await api.rewrite_voice(key)
        check("清缓存成功", res["__status__"] == 200 and res["data"]["ok"])

        # --- 重置
        res = await api.reset_user(key)
        check("重置档案成功", res["__status__"] == 200 and res["data"]["ok"])
        check("档案真的没了", await plugin.store.get(key) is None)

        res = await api.reset_user(key)
        check("重置不存在的档案 404", res["__status__"] == 404)

        # --- 老版本降级
        from astrbot_plugin_menstrual_cycle.core import webapi as webmod

        check("接口层有降级开关", isinstance(webmod.WEB_READY, bool))


async def character_scope() -> None:
    print("\n[18] 她自己一条周期（默认）")
    import tempfile
    from pathlib import Path as _Path

    from astrbot_plugin_menstrual_cycle.core.store import CycleStore
    from astrbot_plugin_menstrual_cycle.core.webapi import DashboardAPI
    from astrbot_plugin_menstrual_cycle.main import (
        CHARACTER_KEY,
        CHARACTER_SCOPE,
        USER_SCOPE,
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = _Path(tmp)
        context = Context()
        plugin = MenstrualCyclePlugin(context, AstrBotConfig())
        plugin.data_dir = tmpdir
        plugin.store = CycleStore(tmpdir / "cycles.json")
        await plugin.store.load()
        plugin._sent = {}

        check("默认就是她自己一条", plugin._scope() == CHARACTER_SCOPE, plugin._scope())
        alice = AstrMessageEvent("你好", sender="11111")
        bob = AstrMessageEvent("在吗", sender="22222")
        check("不同的人算同一个她", plugin._user_key(alice) == plugin._user_key(bob))
        check("用的固定键", plugin._user_key(alice) == CHARACTER_KEY)

        profile = await plugin._profile_for(alice)
        check("自动有档案", profile is not None and profile.periods, "没有自动建档")
        assert profile is not None
        offset = (date.today() - profile.periods[0].start).days
        check("一开始就在周期中间", 0 <= offset <= 27, str(offset))
        check("不记昵称", profile.nickname == "", profile.nickname)
        check("不记平台", profile.platform == "", profile.platform)

        await plugin._remember_umo(profile, alice)
        check("不记会话来源", profile.last_umo == "", profile.last_umo)

        saved = await plugin.store.get(CHARACTER_KEY)
        check("她自己那条落盘", saved is not None)

        # 再拿一次还是同一条，不会每天重建
        again = await plugin._profile_for(bob)
        assert again is not None
        check("同一条不会重建", len(again.periods) == len(profile.periods), str(len(again.periods)))

        # 她说「开始」是改她自己那条（先清空，避免正好撞上同一天）
        again.periods = []
        await plugin.store.upsert(again)
        ok = await plugin._record_start(again, date.today())
        check("她自己记开始", ok is True)
        fresh = await plugin.store.get(CHARACTER_KEY)
        assert fresh is not None
        check("记录落进她自己那条", fresh.periods[-1].start == date.today())
        all_profiles = await plugin.store.all_profiles()
        check("只剩她一条档案", len(all_profiles) == 1, str(len(all_profiles)))

        # 面板里也一样：默认只有她一个，而且不用手点新建
        api = DashboardAPI(plugin)
        res = await api.overview()
        data = res["data"]
        check("总览标了归属", data["plugin"]["scope"] == CHARACTER_SCOPE, str(data["plugin"]))
        check("总览里就是她", len(data["users"]) == 1, str(len(data["users"])))
        check("默认不发提醒", bool(data["plugin"]["reminder"]) is False, str(data["plugin"]["reminder"]))

        # 切回按人分开，行为要回到旧样子
        plugin.config = {"cycle_scope": USER_SCOPE}
        check("能切回按人分开", plugin._scope() == USER_SCOPE)
        check(
            "切回后按人分开",
            plugin._user_key(alice) != plugin._user_key(bob),
            f"{plugin._user_key(alice)} / {plugin._user_key(bob)}",
        )

        plugin.config = {"cycle_scope": "乱写"}
        check("乱写的值回落到她自己", plugin._scope() == CHARACTER_SCOPE)


async def admin_only():
    print("\n[21] 只有管理员能下指令")
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        context = Context()
        plugin = MenstrualCyclePlugin(context, AstrBotConfig({"cycle_scope": "user"}))
        plugin.data_dir = tmpdir
        plugin.store = CycleStore(tmpdir / "cycles.json")
        await plugin.store.load()

        boss = AstrMessageEvent("经期 开始", sender="99999", role="admin")
        out = [item async for item in plugin._dispatch(boss)]
        check("管理员能下指令", bool(out) and out[0][0] == "plain" and "记下了" in out[0][1])

        member = AstrMessageEvent("经期 状态", sender="11111", role="member")
        denied = [item async for item in plugin._dispatch(member)]
        check("普通成员被拦住", bool(denied) and denied[0][0] == "plain")
        check("拦下时说清了怎么配", "只有管理员" in denied[0][1] and "admins_id" in denied[0][1])
        check("拦下时不动数据", len(await plugin.store.all_profiles()) == 1)

        # admins_id 里的人，就算平台角色是 member 也放行
        context.config["admins_id"] = ["22222"]
        in_list = AstrMessageEvent("经期 状态", sender="22222", role="member")
        allowed = [item async for item in plugin._dispatch(in_list)]
        check("admins_id 里的号能用", bool(allowed) and "只有管理员" not in allowed[0][1])

        # extra_admins 补的号
        context.config["admins_id"] = []
        plugin.config["extra_admins"] = "33333，44444"
        extra = AstrMessageEvent("经期 状态", sender="44444", role="member")
        extra_out = [item async for item in plugin._dispatch(extra)]
        check("extra_admins 里的号能用", bool(extra_out) and "只有管理员" not in extra_out[0][1])
        check("extra_admins 认中文逗号", len(plugin._admin_ids()) >= 2)

        # 关掉开关就谁都放行
        plugin.config["enable_admin_only"] = False
        open_event = AstrMessageEvent("经期 状态", sender="55555", role="member")
        open_out = [item async for item in plugin._dispatch(open_event)]
        check("关掉开关后普通人也能用", bool(open_out) and "只有管理员" not in open_out[0][1])

        # 静默模式：什么都不回
        plugin.config["enable_admin_only"] = True
        plugin.config["admin_deny_reply"] = False
        quiet = [item async for item in plugin._dispatch(AstrMessageEvent("经期 状态", sender="66666", role="member"))]
        check("静默模式不出声", quiet == [])

        # 工具：写要权限，读不要
        plugin.config["admin_deny_reply"] = True
        tool_member = AstrMessageEvent("帮我记一下", sender="77777", role="member")
        tool_deny = await plugin.period_cycle_tool(tool_member, "log_start", "")
        check("成员调工具写不进去", "管理员" in tool_deny and "记下" not in tool_deny)
        tool_read = await plugin.period_cycle_tool(tool_member, "status", "")
        check("成员还能读状态", "只有管理员" not in tool_read)
        tool_boss = AstrMessageEvent("帮我记一下", sender="99999", role="admin")
        tool_ok = await plugin.period_cycle_tool(tool_boss, "log_start", "")
        check("管理员能调工具记", "已经记过" in tool_ok or "第" in tool_ok or "周期" in tool_ok)


async def shared_across_sessions() -> None:
    print("\n[19] 群聊和私聊是同一个人")
    import tempfile
    from datetime import timedelta as _td
    from pathlib import Path as _Path

    from astrbot_plugin_menstrual_cycle.core import text as _tt
    from astrbot_plugin_menstrual_cycle.core.engine import compute_state
    from astrbot_plugin_menstrual_cycle.core.models import PeriodRecord
    from astrbot_plugin_menstrual_cycle.core.store import CycleStore
    from astrbot_plugin_menstrual_cycle.core.webapi import DashboardAPI

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = _Path(tmp)
        context = Context()
        plugin = MenstrualCyclePlugin(context, AstrBotConfig())
        plugin.data_dir = tmpdir
        plugin.store = CycleStore(tmpdir / "cycles.json")
        await plugin.store.load()

        group = AstrMessageEvent("在干嘛", sender="11111", umo="aiocqhttp:GroupMessage:20001")
        private = AstrMessageEvent("我回来了", sender="11111", umo="aiocqhttp:FriendMessage:90001")
        outsider = AstrMessageEvent("你们聊什么呢", sender="33333", umo="aiocqhttp:GroupMessage:20001")

        key_group = plugin._user_key(group)
        key_private = plugin._user_key(private)
        check("群聊和私聊同一个键", key_group == key_private, f"{key_group} / {key_private}")
        check("换了人也是同一条", plugin._user_key(outsider) == key_group)

        profile = await plugin._profile_for(group)
        assert profile is not None
        before = compute_state(profile)
        assert before is not None

        # 群聊里他说了句关心的
        await plugin._maybe_comfort(
            profile,
            AstrMessageEvent(
                "我给你倒了杯热水，过来抱抱",
                sender="11111",
                umo="aiocqhttp:GroupMessage:20001",
            ),
        )
        later = await plugin._profile_for(private)
        assert later is not None
        check("安抚记在同一条上", len(later.comforts) >= 1, str(len(later.comforts)))
        after = compute_state(later)
        assert after is not None
        check(
            "私聊里痛感更低",
            after.vector.pain < before.vector.pain,
            f"{before.vector.pain:.3f} -> {after.vector.pain:.3f}",
        )
        check(
            "私聊里压力更低",
            after.vector.stress < before.vector.stress,
            f"{before.vector.stress:.3f} -> {after.vector.stress:.3f}",
        )
        check("私聊也记得刚被哄过", len(after.comforts) >= 1)
        injected = _tt.build_injection(later, after)
        check("私聊注入带着这件事", "刚发生过" in injected or "哄" in injected, injected[:120])

        # 面板改的数据，聊天那边立刻跟着变
        api = DashboardAPI(plugin)
        moved_day = date.today() - _td(days=9)
        WEB_REQUEST.payload = {"nickname": "小鹿", "start_date": moved_day.isoformat()}
        res = await api.save_settings(key_group)
        check("面板能改她的名字和开始日", res["__status__"] == 200, str(res)[:160])
        check("名字进了详情", res["data"]["detail"]["user"]["nickname"] == "小鹿")
        moved = await plugin.store.get(key_group)
        assert moved is not None
        check("开始日挪动了", moved.periods[-1].start == moved_day, str(moved.periods[-1].start))
        state = compute_state(moved)
        check("聊天侧立刻是第 10 天", state is not None and state.cycle_day == 10, str(state.cycle_day if state else None))
        check(
            "设置里回读的是同一个开始日",
            res["data"]["detail"]["settings"]["start_date"] == moved_day.isoformat(),
            str(res["data"]["detail"]["settings"].get("start_date")),
        )

        own_settings = res["data"]["detail"]["settings"]
        check("设置里带名字", own_settings.get("nickname") == "小鹿", str(own_settings.get("nickname")))

        WEB_REQUEST.payload = {"start_date": (date.today() + _td(days=3)).isoformat()}
        res = await api.save_settings(key_group)
        check("未来开始日被拦", res["__status__"] == 400 and "未来" in res["message"], str(res)[:120])

        WEB_REQUEST.payload = {"nickname": "x" * 41}
        res = await api.save_settings(key_group)
        check("名字太长被拦", res["__status__"] == 400 and "40" in res["message"], str(res)[:120])

        # 再加一条更早的记录，验证不能越过前一条
        tail = await plugin.store.get(key_group)
        assert tail is not None
        tail.periods.insert(
            0,
            PeriodRecord(
                id="px",
                start=date.today() - _td(days=40),
                end=None,
                note="",
                created_at="",
            ),
        )
        await plugin.store.upsert(tail)
        WEB_REQUEST.payload = {"start_date": (date.today() - _td(days=50)).isoformat()}
        res = await api.save_settings(key_group)
        check("不能越过上一条", res["__status__"] == 400 and "上一条" in res["message"], str(res)[:120])


async def admin_control() -> None:
    print("\n[20] 默认周期 + 管理员全权调整")
    import tempfile
    from datetime import timedelta as _td
    from pathlib import Path as _Path

    from astrbot_plugin_menstrual_cycle.core.engine import compute_state
    from astrbot_plugin_menstrual_cycle.core.store import CycleStore
    from astrbot_plugin_menstrual_cycle.core.webapi import DashboardAPI

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = _Path(tmp)
        plugin = MenstrualCyclePlugin(
            Context(),
            AstrBotConfig(
                {
                    "cycle_scope": "user",
                    "default_pain_sensitivity": 1.4,
                    "default_emotion_gain": 1.2,
                    "default_pms_strength": 1.5,
                }
            ),
        )
        plugin.data_dir = tmpdir
        plugin.store = CycleStore(tmpdir / "cycles.json")
        await plugin.store.load()

        event = AstrMessageEvent("你好", sender="55555", umo="aiocqhttp:FriendMessage:55555")
        profile = await plugin._profile_for(event)
        assert profile is not None
        check("按人记时新档案不自动带记录", len(profile.periods) == 0, str(len(profile.periods)))
        check(
            "默认疼痛敏感度生效",
            abs(profile.settings.pain_sensitivity - 1.4) < 1e-6,
            str(profile.settings.pain_sensitivity),
        )
        check("默认情绪幅度生效", abs(profile.settings.emotion_gain - 1.2) < 1e-6)
        check("默认 PMS 强度生效", abs(profile.settings.pms_strength - 1.5) < 1e-6)

        key = profile.user_key
        await plugin.store.upsert(profile)
        await plugin._record_start(profile, date.today() - _td(days=4))
        saved = await plugin.store.get(key)
        assert saved is not None
        state = compute_state(saved)
        check(
            "记一次就在第 5 天",
            state is not None and state.cycle_day == 5,
            str(state.cycle_day if state else None),
        )

        api = DashboardAPI(plugin)
        res = await api.user_detail(key)
        det = res["data"]
        check(
            "详情带注入原文",
            isinstance(det.get("injection"), str) and "周期第" in det["injection"],
            str(det.get("injection"))[:60],
        )
        check(
            "详情带可覆盖字段",
            det["overrides"]["fields"][0] == "valence",
            str(det["overrides"]["fields"]),
        )
        check("一开始没有钉住的值", det["overrides"]["values"] == {}, str(det["overrides"]["values"]))

        WEB_REQUEST.payload = {"overrides": {"pain": 0.92, "valence": -0.8}, "override_hours": 6}
        res = await api.state_action(key)
        check("面板能钉住数值", res["__status__"] == 200, str(res)[:160])
        pinned = res["data"]["detail"]["state"]
        check("疼痛被钉住", abs(pinned["vector"]["pain"] - 0.92) < 0.001, str(pinned["vector"]["pain"]))
        check(
            "愉悦度被钉住",
            abs(pinned["vector"]["valence"] + 0.8) < 0.001,
            str(pinned["vector"]["valence"]),
        )
        check("其余维度仍按推算", abs(pinned["vector"]["energy"] - 0.92) > 0.001)
        check(
            "回执写明改了哪几项",
            "手动指定" in res["data"]["message"],
            res["data"]["message"][:80],
        )
        check(
            "详情里能看到钉住截止时间",
            bool(res["data"]["detail"]["overrides"]["until"]),
            res["data"]["detail"]["overrides"]["until"],
        )

        WEB_REQUEST.payload = {"overrides": {"pain": 5}}
        res = await api.state_action(key)
        check(
            "越界的值被夹住",
            res["__status__"] == 200
            and res["data"]["detail"]["state"]["vector"]["pain"] <= 1.0,
            str(res)[:120],
        )

        WEB_REQUEST.payload = {"overrides": {}}
        res = await api.state_action(key)
        check(
            "能清掉钉住的值",
            res["__status__"] == 200 and res["data"]["detail"]["overrides"]["values"] == {},
        )

        WEB_REQUEST.payload = {"overrides": {"什么": 1}}
        res = await api.state_action(key)
        check("乱写的字段被拦", res["__status__"] == 400, str(res)[:120])

        WEB_REQUEST.payload = {"overrides": {"pain": 0.5}, "override_hours": 0}
        res = await api.state_action(key)
        check("时长太短被拦", res["__status__"] == 400, str(res)[:120])

        WEB_REQUEST.payload = {"overrides": {"pain": 0.5}, "override_hours": 1}
        await api.state_action(key)
        fresh = await plugin.store.get(key)
        assert fresh is not None and fresh.override_until
        fresh.override_until = (datetime.now() - _td(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
        await plugin.store.upsert(fresh)
        again = await plugin.store.get(key)
        assert again is not None
        check("过期后不再钉住", again.active_overrides() == {}, str(again.active_overrides()))
        expired = compute_state(again)
        check("过期后状态回到推算", expired is not None and expired.overrides == {})

    with tempfile.TemporaryDirectory() as tmp2:
        tmpdir2 = _Path(tmp2)
        her_plugin = MenstrualCyclePlugin(
            Context(), AstrBotConfig({"default_cycle_day": 5})
        )
        her_plugin.data_dir = tmpdir2
        her_plugin.store = CycleStore(tmpdir2 / "cycles.json")
        await her_plugin.store.load()
        her = await her_plugin.ensure_character_profile()
        check("她一来就有默认周期", len(her.periods) == 1, str(len(her.periods)))
        offset = (date.today() - her.periods[0].start).days
        check("默认周期落在第 5 天", offset == 4, str(offset))
        her_state = compute_state(her)
        check(
            "她的状态立刻可用",
            her_state is not None and her_state.cycle_day == 5,
            str(her_state.cycle_day if her_state else None),
        )
        check("她的档案落了盘", (await her_plugin.store.get("character:self")) is not None)


async def polish() -> None:
    print("\n[22] 打磨：一行状态 / 提醒去重落盘 / 七天走势")
    import json as _json
    import tempfile
    from datetime import timedelta as _td
    from pathlib import Path as _Path

    from astrbot_plugin_menstrual_cycle.core import text as _t
    from astrbot_plugin_menstrual_cycle.core.engine import compute_state
    from astrbot_plugin_menstrual_cycle.core.store import CycleStore
    from astrbot_plugin_menstrual_cycle.core.webapi import DashboardAPI, _new_id

    help_text = _t.render_help()
    for head in ("看看", "记录", "调整", "清理"):
        check(f"帮助分成「{head}」", f"\n{head}\n" in help_text, head)
    check("帮助写了简版状态", "状态 [简]" in help_text)
    check("帮助提到管理员限制", "管理员" in help_text)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = _Path(tmp)
        plugin = MenstrualCyclePlugin(Context(), AstrBotConfig({"cycle_scope": "character"}))
        plugin.data_dir = tmpdir
        plugin.store = CycleStore(tmpdir / "cycles.json")
        plugin._sent_path = tmpdir / "reminders.json"
        await plugin.store.load()
        event = AstrMessageEvent("你好", umo="aiocqhttp:GroupMessage:10001")
        profile = await plugin._profile_for(event)
        assert profile is not None
        await plugin._record_start(profile, date.today() - _td(days=6))
        saved = await plugin.store.get(profile.user_key)
        assert saved is not None
        state = compute_state(saved)
        assert state is not None

        brief = _t.render_brief(saved, state)
        check("一行状态真的只有一行", "\n" not in brief, brief[:48])
        check("一行状态带阶段名", state.phase.name in brief, brief)
        check("一行状态带天数", f"第 {state.cycle_day}/{state.cycle_length} 天" in brief, brief)
        check("一行状态带疼痛百分比", "%" in brief, brief)
        check("一行状态不超过 90 字", len(brief) <= 90, str(len(brief)))

        full_chunks = [item[1] for item in await _collect(plugin._handle("状态", [], event))]
        brief_chunks = [item[1] for item in await _collect(plugin._handle("状态", ["简"], event))]
        check("完整状态是好几行", len(full_chunks[0].splitlines()) > 5, str(len(full_chunks[0].splitlines())))
        check("简版只回很短", len(brief_chunks[0]) < len(full_chunks[0]) / 2, str(len(brief_chunks[0])))

        plugin._sent = {"character:self": "2026-11-16"}
        plugin._phase_sent = {"character:self": "first:2026-12-10"}
        plugin._save_sent()
        plugin._sent = {}
        plugin._phase_sent = {}
        plugin._load_sent()
        check("提醒去重表能读回", plugin._sent.get("character:self") == "2026-11-16", str(plugin._sent))
        check(
            "本轮哪种提醒发过也能读回",
            plugin._phase_sent.get("character:self") == "first:2026-12-10",
            str(plugin._phase_sent),
        )

        plugin._sent_path.write_text(
            _json.dumps({"character:self": "2026-11-15"}), encoding="utf-8"
        )
        plugin._sent = {}
        plugin._phase_sent = {}
        plugin._load_sent()
        check("老格式的提醒文件还能读", plugin._sent.get("character:self") == "2026-11-15", str(plugin._sent))
        check("老格式读进来不炸", plugin._phase_sent == {}, str(plugin._phase_sent))

        api = DashboardAPI(plugin)
        detail = await api._detail_payload(saved)
        trend = detail.get("trend") or []
        check("详情带七天走势", len(trend) == 7, str(len(trend)))
        check("走势最后一天是今天", trend[-1]["date"] == date.today().isoformat(), trend[-1]["date"])
        check("走势每天都有阶段", all(item.get("phase") for item in trend), str(trend[0]))
        pains = [item["pain"] for item in trend if item.get("pain") is not None]
        moods = [item["mood"] for item in trend if item.get("mood") is not None]
        check("走势的疼痛在 0..1", bool(pains) and all(0.0 <= v <= 1.0 for v in pains), str(pains))
        check("走势的心情在 -1..1", bool(moods) and all(-1.0 <= v <= 1.0 for v in moods), str(moods))

        ids = {_new_id() for _ in range(200)}
        check("记录 id 不会撞号", len(ids) == 200, str(len(ids)))
        check("记录 id 带前缀", all(item.startswith("p") and len(item) == 9 for item in ids), list(ids)[:2])


async def _collect(gen):
    return [item async for item in gen]


async def main() -> None:
    phase_matrix()
    emotion_curve()
    stability()
    forecasting()
    rendering()
    settings_parsing()
    command_parsing()
    await storage_flow()
    await bridge_behaviour()
    await chatplus_behaviour()
    await injection_hook()
    await reminder_logic()
    await comfort_layer()
    await behaviour_rotation()
    await llm_voice()
    await realtime_voice()
    await dashboard_api()
    await character_scope()
    await shared_across_sessions()
    await admin_only()
    await admin_control()
    await polish()
    print("\n" + "=" * 50)
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
