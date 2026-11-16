"""「被哄」识别层。

真实的人不是只按激素走。有人递热水、有人揉两下、有人把话接过去，
当下的难受会真的轻一截，而且这种松动会持续几个小时再淡掉。
这个模块只做两件事：从一句话里认出「关心」，把它变成一组可叠加的修正量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import DT_FMT, Comfort


@dataclass(frozen=True, slots=True)
class Rule:
    kind: str
    label: str
    words: tuple[str, ...]
    deltas: dict[str, float] = field(default_factory=dict)
    hours: float = 6.0


# 顺序就是优先级：止痛药 > 热饮 > 揉 > 抱 > 接手事情 > 吃的
RULES: tuple[Rule, ...] = (
    Rule(
        kind="medicine",
        label="有人给你拿了止痛药、贴了暖宝宝",
        words=("布洛芬", "止痛药", "止痛", "吃药", "药", "暖宝宝", "暖宫贴", "贴上"),
        deltas={"pain": -0.26, "stress": -0.06, "energy": 0.05},
        hours=8.0,
    ),
    Rule(
        kind="hot",
        label="有人给你倒了杯热的",
        words=("热水", "红糖", "姜茶", "热的", "热牛奶", "暖水袋", "热水袋", "热敷", "捂捂"),
        deltas={"pain": -0.16, "stress": -0.13, "valence": 0.10, "energy": 0.04},
        hours=6.0,
    ),
    Rule(
        kind="rub",
        label="有人帮你揉了肚子",
        words=("揉揉", "揉一揉", "揉一下", "揉", "按按", "捶", "捏捏", "摸摸头", "捋"),
        deltas={"pain": -0.15, "stress": -0.11, "valence": 0.09, "social": 0.09},
        hours=5.0,
    ),
    Rule(
        kind="hug",
        label="有人抱住了你",
        words=("抱抱", "抱住", "抱一下", "抱你", "搂", "贴贴", "靠着", "蹭蹭"),
        deltas={"pain": -0.06, "stress": -0.17, "valence": 0.15, "social": 0.13},
        hours=5.0,
    ),
    Rule(
        kind="care",
        label="有人把事情接过去了，让你别再撑",
        words=("别动", "我来", "交给我", "你休息", "你躺着", "不用管", "我陪你", "不吵你", "早点睡", "请假"),
        deltas={"stress": -0.19, "valence": 0.12, "social": 0.07, "energy": 0.04},
        hours=7.0,
    ),
    Rule(
        kind="food",
        label="有人给你弄了点吃的",
        words=("吃饭", "吃点", "煮", "做饭", "外卖", "甜", "巧克力", "蛋糕", "粥", "宵夜", "水果"),
        deltas={"valence": 0.10, "energy": 0.07, "stress": -0.06},
        hours=5.0,
    ),
)

NEGATIONS = ("不", "别", "没", "免", "懒", "拒绝", "甭", "勿")

# 同一类关心在窗口内不重复记，避免「抱抱抱抱抱抱」把状态刷满
MIN_INTERVAL_MINUTES = 90


def detect(text: str) -> tuple[Rule, str] | None:
    """从一句话里认出关心；返回命中的规则和原词。"""
    content = (text or "").strip()
    if not content:
        return None
    for rule in RULES:
        for word in rule.words:
            index = content.find(word)
            while index != -1:
                window = content[max(0, index - 4) : index]
                if not any(bad in window for bad in NEGATIONS):
                    return rule, word
                index = content.find(word, index + 1)
    return None


def build(
    rule: Rule, word: str, now: datetime | None = None, hours: float | None = None
) -> Comfort:
    moment = now or datetime.now()
    # 止痛药按自身时长走，其余听用户配置
    span = rule.hours if rule.kind == "medicine" else (hours or rule.hours)
    return Comfort(
        kind=rule.kind,
        label=rule.label,
        at=moment.strftime(DT_FMT),
        expires_at=(moment + timedelta(hours=span)).strftime(DT_FMT),
        pain=rule.deltas.get("pain", 0.0),
        stress=rule.deltas.get("stress", 0.0),
        valence=rule.deltas.get("valence", 0.0),
        social=rule.deltas.get("social", 0.0),
        energy=rule.deltas.get("energy", 0.0),
    )


def register(
    profile,
    text: str,
    *,
    now: datetime | None = None,
    hours: float | None = None,
    limit: int = 12,
) -> Comfort | None:
    """识别并写入档案；同类太密就跳过。返回新记下的那条（或 None）。"""
    hit = detect(text)
    if hit is None:
        return None
    rule, word = hit
    moment = now or datetime.now()
    for item in reversed(profile.comforts[-limit:]):
        if item.kind != rule.kind:
            continue
        try:
            last = datetime.strptime(item.at, DT_FMT)
        except ValueError:
            continue
        if (moment - last).total_seconds() < MIN_INTERVAL_MINUTES * 60:
            return None
        break
    entry = build(rule, word, moment, hours)
    profile.comforts = [item for item in profile.comforts if item.active(moment)][-limit:]
    profile.comforts.append(entry)
    return entry


def summarize(comforts: list[Comfort], limit: int = 3) -> str:
    """把生效中的关心拼成一句人话，用于状态卡。"""
    if not comforts:
        return ""
    labels: list[str] = []
    for item in reversed(comforts):
        if item.label and item.label not in labels:
            labels.append(item.label)
        if len(labels) >= limit:
            break
    return "；".join(reversed(labels))


def reward_text(comforts: list[Comfort]) -> str:
    """她自己的体感：被照顾之后身体确实松了。"""
    if not comforts:
        return ""
    kinds = {item.kind for item in comforts}
    pieces: list[str] = []
    if "medicine" in kinds:
        pieces.append("痛感钝下来了")
    if "hot" in kinds:
        pieces.append("小腹没那么绷")
    if "rub" in kinds:
        pieces.append("坠胀散掉一些")
    if "hug" in kinds:
        pieces.append("人被抱住的时候软了")
    if "care" in kinds:
        pieces.append("不用再一个人撑着")
    if "food" in kinds:
        pieces.append("吃了点东西，心气回来一点")
    return "，".join(pieces) if pieces else ""


__all__ = [
    "MIN_INTERVAL_MINUTES",
    "RULES",
    "Rule",
    "build",
    "detect",
    "register",
    "reward_text",
    "summarize",
]
