"""Web 控制台后端。

这里只做三件事：读数据、改数据、把能直接画出来的结构吐给前端。
所有业务判断仍然走 engine / text / voice，面板不是第二套实现。
"""

from __future__ import annotations

import contextlib
import time
import uuid
from datetime import date, datetime, timedelta
from urllib.parse import unquote
from typing import Any

from . import comfort as cm
from . import text as tt
from .engine import (
    compute_state,
    cycle_stats,
    phase_baseline,
    predict_next_periods,
    pretty_date,
)
from .models import (
    CONSTITUTIONS,
    DT_FMT,
    VECTOR_FIELDS,
    PeriodRecord,
    PhaseState,
    UserProfile,
    _clean_overrides,
    parse_date,
)

PLUGIN_NAME = "astrbot_plugin_menstrual_cycle"

try:  # 4.29+ 才有 astrbot.api.web；老版本整体降级
    from astrbot.api.web import error_response, json_response  # type: ignore
    from astrbot.api.web import request as web_request  # type: ignore

    WEB_READY = True
except Exception:  # pragma: no cover - 仅老版本走到
    error_response = json_response = web_request = None  # type: ignore
    WEB_READY = False


# 面板能改的设置项 -> (最小, 最大, 取整)
NUMERIC_SETTINGS: dict[str, tuple[float, float, bool]] = {
    "cycle_length": (15, 90, True),
    "period_length": (1, 12, True),
    "pain_sensitivity": (0.2, 2.5, False),
    "emotion_gain": (0.2, 2.5, False),
    "pms_strength": (0.0, 3.0, False),
}

LABELS = {
    "cycle_length": "周期长度",
    "period_length": "经期长度",
    "pain_sensitivity": "疼痛敏感度",
    "emotion_gain": "情绪幅度",
    "pms_strength": "PMS 强度",
    "reminder": "主动提醒",
    "reminder_style": "提醒口吻",
    "paused_until": "观察期到",
}


def _iso(day: date | None) -> str:
    return day.isoformat() if day else ""


def _round(value: float | None, digits: int = 3) -> float:
    return round(float(value or 0.0), digits)


def _stamp(value: Any) -> str:
    """'2026-11-16T19:02:11' 或 datetime -> '11-16 19:02'。"""
    if isinstance(value, datetime):
        return value.strftime("%m-%d %H:%M")
    text = str(value or "")
    if len(text) >= 16 and text[10] in "T ":
        return f"{text[5:10]} {text[11:16]}"
    return text


def _new_id() -> str:
    """记录的 id：不用毫秒时间戳，面板连着点也不会撞。"""
    return f"p{uuid.uuid4().hex[:8]}"


def _now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _vector_dict(state: PhaseState) -> dict[str, Any]:
    vec = state.vector
    return {
        "valence": _round(vec.valence),
        "arousal": _round(vec.arousal),
        "energy": _round(vec.energy),
        "stress": _round(vec.stress),
        "pain": _round(vec.pain),
        "sensitive": _round(vec.sensitive),
        "libido": _round(vec.libido),
        "social": _round(vec.social),
        "mood": vec.mood_label(),
    }


def state_payload(state: PhaseState | None) -> dict[str, Any] | None:
    """PhaseState -> 前端直接可用的小字典。"""
    if state is None:
        return None
    phase = state.phase
    payload: dict[str, Any] = {
        "cycle_day": state.cycle_day,
        "cycle_length": state.cycle_length,
        "period_length": state.period_length,
        "days_to_next": state.days_to_next,
        "next_start": _iso(state.next_start),
        "progress": _round(state.progress, 4),
        "phase": phase.key,
        "phase_name": phase.name,
        "emoji": phase.emoji,
        "summary": phase.summary,
        "body": list(phase.body),
        "behaviors": list(phase.behaviors),
        "tone": phase.tone,
        "irregular": state.irregular,
        "variability": _round(state.variability, 4),
        "paused": state.paused,
        "stale": state.stale,
        "underway": state.underway,
        "pain_logged": None
        if state.pain_logged is None
        else _round(state.pain_logged),
        "comforts": [
            {
                "kind": item.kind,
                "label": item.label,
                "minutes": item.remaining_minutes(),
                "at": _stamp(item.at),
            }
            for item in state.comforts
        ],
        "summary_line": state.summary_line(),
    }
    vector = _vector_dict(state)
    payload.update(vector)
    # 前端按 state.vector[key] 读，两套并存互不干扰
    payload["vector"] = vector
    return payload


def record_payload(record: PeriodRecord) -> dict[str, Any]:
    length = record.length
    return {
        "id": record.id,
        "start": _iso(record.start),
        "end": _iso(record.end),
        "length": length,
        "open": record.end is None,
        "note": record.note or "",
        "start_label": pretty_date(record.start),
    }


def settings_payload(profile: UserProfile) -> dict[str, Any]:
    settings = profile.settings
    latest = max(profile.periods, key=lambda r: r.start) if profile.periods else None
    return {
        "nickname": profile.nickname or "",
        "start_date": _iso(latest.start) if latest else "",
        "cycle_length": settings.cycle_length,
        "period_length": settings.period_length,
        "pain_sensitivity": _round(settings.pain_sensitivity, 2),
        "emotion_gain": _round(settings.emotion_gain, 2),
        "pms_strength": _round(settings.pms_strength, 2),
        "reminder": bool(settings.reminder),
        "reminder_style": settings.reminder_style,
        "paused_until": settings.paused_until,
        "constitution": settings.constitution,
        "phase_learning": bool(settings.phase_learning),
        "phase_weight": _round(settings.phase_weight, 2),
    }


class DashboardAPI:
    """把插件数据暴露成一组 JSON 接口。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def routes(self) -> list[tuple[str, Any, list[str], str]]:
        base = f"/{PLUGIN_NAME}"
        return [
            (f"{base}/overview", self.overview, ["GET"], "周期插件总览"),
            (f"{base}/user", self.create_user, ["POST"], "新建档案"),
            (f"{base}/user/<path:user_key>", self.user_detail, ["GET"], "单个用户详情"),
            (f"{base}/user/<path:user_key>/record", self.record_action, ["POST"], "记录增删改"),
            (f"{base}/user/<path:user_key>/state", self.state_action, ["POST"], "改当下的状态"),
            (f"{base}/user/<path:user_key>/settings", self.save_settings, ["POST"], "保存设置"),
            (f"{base}/user/<path:user_key>/reset", self.reset_user, ["POST"], "重置档案"),
            (f"{base}/user/<path:user_key>/voice", self.rewrite_voice, ["POST"], "重写文案"),
            # 下面两条把 user_key 放进 query/body，避免路径里的冒号被网关吃掉
            (f"{base}/detail", self.detail_query, ["GET"], "单个用户详情（query 版）"),
            (f"{base}/action", self.action, ["POST"], "统一写操作（body 版）"),
            (f"{base}/export", self.export_user, ["GET"], "导出一个档案"),
            (f"{base}/import", self.import_user, ["POST"], "导入一个档案"),
        ]

    def register(self) -> bool:
        if not WEB_READY:
            return False
        context = getattr(self.plugin, "context", None)
        register = getattr(context, "register_web_api", None)
        if register is None:
            return False
        ok = False
        for route, handler, methods, desc in self.routes():
            try:
                register(route, handler, methods, desc)
                ok = True
            except Exception as exc:  # 一个失败不该拖垮整组
                self._log().warning("[menstrual] 注册 %s 失败：%s", route, exc)
        return ok

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    def _log(self) -> Any:
        return self.plugin._log

    def _store(self) -> Any:
        return self.plugin.store

    async def _profiles(self) -> list[UserProfile]:
        profiles = await self._store().all_profiles()
        return sorted(profiles, key=lambda p: p.updated_at or "", reverse=True)

    async def _profile(self, user_key: str) -> UserProfile | None:
        # 有些版本的 Dashboard 会把路径参数原样转发，先解码再查
        key = unquote(str(user_key or "")).strip()
        profile = await self._store().get(key)
        if profile is None and key != user_key:
            profile = await self._store().get(str(user_key or "").strip())
        return profile

    def _voice_mode(self) -> str:
        mode = str(self.plugin._cfg("voice_mode", "cycle") or "cycle")
        if not self.plugin.voice.enabled:
            return "off"
        return mode

    # ------------------------------------------------------------------
    # GET /detail?key=...
    # ------------------------------------------------------------------
    def _query_key(self) -> str:
        """从 query 里取 key。不同版本的 request 代理字段不完全一样，都试一遍。"""
        query = getattr(web_request, "query", None) if web_request is not None else None
        if query is None:
            return ""
        getter = getattr(query, "get", None)
        if not callable(getter):
            return ""
        try:
            return str(getter("key", "") or "")
        except Exception:
            return ""

    async def detail_query(self) -> Any:
        """和老路由等价，但 user_key 走 query，躲开路径编码。"""
        try:
            key = self._query_key()
            if not key:
                return self._fail("缺少 key")
            return await self.user_detail(key)
        except Exception as exc:
            self._log().exception("[menstrual] detail 出错")
            return self._fail(f"读取失败：{exc}", status_code=500)

    async def export_user(self) -> Any:
        """整份档案导成 JSON：换机器、备份、或者拿去改都能用。"""
        try:
            key = self._query_key()
            if not key:
                return self._fail("缺少 key")
            profile = await self._profile(key)
            if profile is None:
                return self._fail("没有这个用户", status_code=404)
            return self._ok(
                {
                    "format": PLUGIN_NAME,
                    "version": getattr(self.plugin, "version", "") or "",
                    "exported_at": _now_stamp(),
                    "profile": profile.as_dict(),
                }
            )
        except Exception as exc:
            self._log().exception("[menstrual] export 出错")
            return self._fail(f"导出失败：{exc}", status_code=500)

    async def import_user(self) -> Any:
        """把导出的 JSON 灌回去，覆盖同 key 的档案。"""
        try:
            body = await self._body()
            key = str(body.get("key") or "").strip()
            if not key:
                return self._fail("缺少 key")
            data = body.get("data")
            if isinstance(data, dict) and isinstance(data.get("profile"), dict):
                data = data["profile"]
            if not isinstance(data, dict):
                return self._fail("要导入的数据格式不对：应该选择导出的那份 JSON")
            payload = dict(data)
            payload["user_key"] = key
            profile = UserProfile.from_dict(payload)
            profile.trim_pain_logs()
            profile.updated_at = _now_stamp()
            await self._store().upsert(profile)
            with contextlib.suppress(Exception):
                self.plugin.voice.invalidate(profile)
            with contextlib.suppress(Exception):
                await self.plugin._auto_sync(profile, compute_state(profile))
            return self._ok(
                {
                    "ok": True,
                    "message": (
                        f"已导入 {len(profile.periods)} 条记录、"
                        f"{len(profile.pain_logs)} 天疼痛记录"
                    ),
                    "detail": await self._detail_payload(profile),
                }
            )
        except Exception as exc:
            self._log().exception("[menstrual] import 出错")
            return self._fail(f"导入失败：{exc}", status_code=500)

    # ------------------------------------------------------------------
    # POST /action  {"key": ..., "op": "record|state|settings|voice|reset"}
    # ------------------------------------------------------------------
    async def action(self) -> Any:
        try:
            body = await self._body()
            key = str(body.get("key") or "").strip()
            op = str(body.get("op") or "").strip().lower()
            if not key:
                return self._fail("缺少 key")
            table = {
                "record": self.record_action,
                "state": self.state_action,
                "settings": self.save_settings,
                "voice": self.rewrite_voice,
                "reset": self.reset_user,
            }
            handler = table.get(op)
            if handler is None:
                return self._fail("不认识的操作")
            return await handler(key)
        except Exception as exc:
            self._log().exception("[menstrual] action 出错")
            return self._fail(f"操作失败：{exc}", status_code=500)

    @staticmethod
    def _ok(payload: Any) -> Any:
        if json_response is None:
            return payload
        return json_response(payload)

    @staticmethod
    def _fail(message: str, status_code: int = 400, data: Any = None) -> Any:
        if error_response is None:
            return {"ok": False, "message": message, "data": data}
        return error_response(message, status_code=status_code, data=data)

    # ------------------------------------------------------------------
    # GET /overview
    # ------------------------------------------------------------------
    async def overview(self) -> Any:
        try:
            if self.plugin._scope() == "character":
                await self.plugin.ensure_character_profile()
            profiles = await self._profiles()
            today = date.today()
            users: list[dict[str, Any]] = []
            for profile in profiles:
                state = compute_state(profile, today)
                latest = profile.latest()
                item: dict[str, Any] = {
                    "user_key": profile.user_key,
                    "nickname": profile.nickname or "",
                    "platform": profile.platform or "",
                    "umo": profile.last_umo or "",
                    "records": len(profile.periods),
                    "updated_at": profile.updated_at or "",
                    "paused": profile.paused(today),
                    "paused_until": profile.settings.paused_until,
                    "present": state is not None,
                }
                if state is not None:
                    payload = state_payload(state) or {}
                    item.update(
                        {
                            "cycle_day": payload["cycle_day"],
                            "cycle_length": payload["cycle_length"],
                            "phase": payload["phase"],
                            "phase_name": payload["phase_name"],
                            "emoji": payload["emoji"],
                            "days_to_next": payload["days_to_next"],
                            "next_start": payload["next_start"],
                            "pain": payload["pain"],
                            "valence": payload["valence"],
                            "mood": payload["mood"],
                            "irregular": payload["irregular"],
                            "variability": payload["variability"],
                            "comforts": len(payload["comforts"]),
                            "summary_line": payload["summary_line"],
                        }
                    )
                if latest is not None:
                    item["last_start"] = _iso(latest.start)
                users.append(item)

            stats_total = {
                "users": len(users),
                "active": sum(1 for u in users if u.get("present")),
                "irregular": sum(1 for u in users if u.get("irregular")),
                "records": sum(u.get("records", 0) for u in users),
            }
            return self._ok(
                {
                    "ok": True,
                    "today": _iso(today),
                    "plugin": {
                        "name": PLUGIN_NAME,
                        "scope": self.plugin._scope(),
                        "version": getattr(self.plugin, "version", "") or "",
                        "voice_mode": self._voice_mode(),
                        "llm_voice": bool(self.plugin.voice.enabled),
                        "comfort": bool(self.plugin._cfg("enable_comfort_buff", True)),
                        "injection": bool(self.plugin._cfg("enable_llm_injection", True)),
                        "reminder": bool(self.plugin._cfg("enable_reminder", False)),
                    },
                    "links": {
                        "selflearn": self.plugin.bridge.status_text(),
                        "chatplus": self.plugin.chatplus.status_text(),
                    },
                    "stats": stats_total,
                    "platforms": self._platforms(),
                    "perf": self._perf_payload(),
                    "users": users,
                }
            )
        except Exception as exc:
            self._log().exception("[menstrual] overview 出错")
            return self._fail(f"读取失败：{exc}", status_code=500)

    def _platforms(self) -> list[dict[str, str]]:
        """能列出来的平台，给「新建档案」的下拉用。"""
        out: list[dict[str, str]] = []
        with contextlib.suppress(Exception):
            manager = getattr(self.plugin.context, "platform_manager", None)
            insts = getattr(manager, "platform_insts", None) or []
            for inst in insts:
                pid = str(getattr(inst, "id", "") or "")
                if not pid or any(item["id"] == pid for item in out):
                    continue
                kind = ""
                meta = getattr(inst, "meta", None)
                if callable(meta):
                    with contextlib.suppress(Exception):
                        kind = str(getattr(meta(), "type", "") or "")
                out.append({"id": pid, "type": kind})
        return out

    # ------------------------------------------------------------------
    # POST /user （新建档案）
    # ------------------------------------------------------------------
    async def create_user(self) -> Any:
        try:
            body = await self._body()
            platform = str(body.get("platform") or "").strip()
            sender = str(body.get("sender") or "").strip()
            raw_key = str(body.get("user_key") or "").strip()
            if not sender and not raw_key:
                return self._fail("要填她的用户 ID")
            if raw_key and ":" in raw_key:
                key = raw_key
                platform = platform or raw_key.split(":", 1)[0]
            else:
                value = sender or raw_key
                if not platform:
                    guessed = self._platforms()
                    if len(guessed) == 1:
                        platform = guessed[0]["id"]
                if not platform:
                    return self._fail("先选一个平台，或者填完整的会话键（平台:用户ID）")
                key = f"{platform}:{value}"
            if " " in key or len(key) > 120:
                return self._fail("这个用户 ID 里不该有空格")
            if await self._profile(key) is not None:
                return self._fail("这个档案已经在了，直接点左边的名字就行")

            start = parse_date(body.get("start"))
            today = date.today()
            if start is not None and start > today:
                return self._fail("开始日不能是未来")

            profile = UserProfile(
                user_key=key,
                nickname=str(body.get("nickname") or "").strip()[:40],
                platform=platform,
            )
            self.plugin._apply_default_settings(profile)
            if start is not None:
                profile.periods.append(
                    PeriodRecord(
                        id=_new_id(),
                        start=start,
                        end=None,
                        note="",
                        created_at=_now_stamp(),
                    )
                )
            else:
                # 没填开始日就按默认周期先接上一段，别让新档案是空白的
                self.plugin._seed_default_cycle(profile)
            profile.updated_at = _now_stamp()
            await self._store().upsert(profile)
            tail = "" if start is None else "，已经带上第一条记录"
            return self._ok(
                {
                    "ok": True,
                    "message": f"建好了 {key}{tail}",
                    "detail": await self._detail_payload(profile),
                }
            )
        except Exception as exc:
            self._log().exception("[menstrual] create_user 出错")
            return self._fail(f"创建失败：{exc}", status_code=500)

    # ------------------------------------------------------------------
    # GET /user/<user_key>
    # ------------------------------------------------------------------
    async def user_detail(self, user_key: str = "") -> Any:
        try:
            profile = await self._profile(user_key)
            if profile is None:
                return self._fail("没有这个用户", status_code=404)
            payload = await self._detail_payload(profile)
            return self._ok({"ok": True, "today": _iso(date.today()), **payload})
        except Exception as exc:
            self._log().exception("[menstrual] user_detail 出错")
            return self._fail(f"读取失败：{exc}", status_code=500)

    # ------------------------------------------------------------------
    # POST /user/<user_key>/record
    # ------------------------------------------------------------------
    async def record_action(self, user_key: str = "") -> Any:
        try:
            body = await self._body()
            action = str(body.get("action") or "").strip().lower()
            profile = await self._profile(user_key)
            if profile is None:
                return self._fail("没有这个用户", status_code=404)
            today = date.today()

            if action in ("start", "begin", "log_start"):
                day = parse_date(body.get("date")) or today
                if day > today:
                    return self._fail("不能记未来的开始日")
                created = await self.plugin._record_start(profile, day)
                if not created:
                    return self._fail(f"{pretty_date(day)} 已经在记录里了")
                await self._after_change(profile)
                return self._ok(
                    {
                        "ok": True,
                        "message": f"记下了：{pretty_date(day)} 开始",
                        "detail": await self._detail_payload(profile),
                    }
                )

            if action in ("end", "finish", "log_end"):
                day = parse_date(body.get("date")) or today
                ok, message = await self.plugin._record_end(profile, day, "")
                if not ok:
                    return self._fail(message)
                await self._after_change(profile)
                return self._ok(
                    {
                        "ok": True,
                        "message": message,
                        "detail": await self._detail_payload(profile),
                    }
                )

            if action in ("delete", "remove", "del"):
                rid = str(body.get("id") or "").strip()
                if not rid:
                    return self._fail("要删哪一条？")
                target = next((r for r in profile.periods if r.id == rid), None)
                if target is None:
                    return self._fail("没找到这条记录")
                profile.periods = [r for r in profile.periods if r.id != rid]
                await self._store().upsert(profile)
                await self._after_change(profile)
                return self._ok(
                    {
                        "ok": True,
                        "message": f"已删除 {pretty_date(target.start)} 开始的记录",
                        "detail": await self._detail_payload(profile),
                    }
                )

            if action in ("update", "edit", "modify"):
                rid = str(body.get("id") or "").strip()
                if not rid:
                    return self._fail("要改哪一条？")
                target = next((r for r in profile.periods if r.id == rid), None)
                if target is None:
                    return self._fail("没找到这条记录")
                start, end, error = self._parse_range(body, today)
                if error:
                    return self._fail(error)
                clash = self._find_clash(profile.periods, start, end, skip=rid)
                if clash:
                    return self._fail(clash)
                target.start = start
                target.end = end
                if "note" in body:
                    target.note = str(body.get("note") or "").strip()[:60]
                profile.periods.sort(key=lambda r: r.start)
                profile.updated_at = _now_stamp()
                await self._store().upsert(profile)
                await self._after_change(profile)
                return self._ok(
                    {
                        "ok": True,
                        "message": f"已更新 {pretty_date(start)} 这条记录",
                        "detail": await self._detail_payload(profile),
                    }
                )

            if action in ("create", "add", "insert", "new"):
                start, end, error = self._parse_range(body, today)
                if error:
                    return self._fail(error)
                clash = self._find_clash(profile.periods, start, end)
                if clash:
                    return self._fail(clash)
                record = PeriodRecord(
                    id=_new_id(),
                    start=start,
                    end=end,
                    note=str(body.get("note") or "").strip()[:60],
                    created_at=_now_stamp(),
                )
                profile.periods.append(record)
                profile.periods.sort(key=lambda r: r.start)
                profile.updated_at = _now_stamp()
                await self._store().upsert(profile)
                await self._after_change(profile)
                length = record.length
                tail = f"，共 {length} 天" if length else "，还没结束"
                return self._ok(
                    {
                        "ok": True,
                        "message": f"补录了 {pretty_date(start)} 开始的一次{tail}",
                        "detail": await self._detail_payload(profile),
                    }
                )

            return self._fail(f"不认识的动作：{action or '空'}")
        except Exception as exc:
            self._log().exception("[menstrual] record_action 出错")
            return self._fail(f"操作失败：{exc}", status_code=500)

    # ------------------------------------------------------------------
    # POST /user/<user_key>/state
    # ------------------------------------------------------------------
    async def state_action(self, user_key: str = "") -> Any:
        """直接改她当下的状态：自报疼痛、被哄、周期日、观察期。"""
        try:
            body = await self._body()
            profile = await self._profile(user_key)
            if profile is None:
                return self._fail("没有这个用户", status_code=404)
            today = date.today()
            moment = datetime.now()
            changes: list[str] = []

            if "pain" in body:
                raw = body.get("pain")
                key = today.isoformat()
                if raw in (None, "", "null", "None", "clear"):
                    if profile.pain_logs.pop(key, None) is not None:
                        changes.append("清掉了今天的自报疼痛")
                    else:
                        changes.append("今天本来就没自报过疼痛")
                else:
                    try:
                        level = float(raw)
                    except (TypeError, ValueError):
                        return self._fail("疼痛要填 0～10 的数字")
                    if not 0 <= level <= 10:
                        return self._fail("疼痛应该在 0～10 之间")
                    profile.pain_logs[key] = round(level / 10, 4)
                    profile.trim_pain_logs()
                    state = compute_state(profile, today)
                    if state is not None:
                        profile.remember_phase_pain(state.phase.key, level / 10.0)
                        changes.append(
                            f"今天的疼痛记成 {level:g}/10（{state.phase.name}样本 +1）"
                        )
                    else:
                        changes.append(f"今天的疼痛记成 {level:g}/10")

            if "comfort" in body:
                kind = str(body.get("comfort") or "").strip().lower()
                if not kind:
                    count = len(profile.comforts)
                    profile.comforts = []
                    changes.append(f"清掉了 {count} 条被哄记录" if count else "本来就没有被哄记录")
                else:
                    rule = next((item for item in cm.RULES if item.kind == kind), None)
                    if rule is None:
                        options = "、".join(f"{item.kind}={item.label}" for item in cm.RULES)
                        return self._fail(f"不认识这个类别，可选：{options}")
                    raw_hours = body.get("comfort_hours")
                    hours: float | None = None
                    if raw_hours not in (None, ""):
                        try:
                            hours = float(raw_hours)
                        except (TypeError, ValueError):
                            return self._fail("时长要填数字")
                        if not 0.5 <= hours <= 24:
                            return self._fail("时长在 0.5～24 小时之间")
                    entry = cm.build(rule, rule.words[0], moment, hours)
                    alive = [item for item in profile.comforts if item.active(moment)]
                    profile.comforts = alive[-11:] + [entry]
                    changes.append(f"记下「{rule.label}」，缓到 {_stamp(entry.expires_at)}")

            if "cycle_day" in body:
                raw = body.get("cycle_day")
                if raw in (None, "", "null"):
                    return self._fail("要填第几天")
                try:
                    want = int(float(raw))
                except (TypeError, ValueError):
                    return self._fail("第几天要填数字")
                periods = sorted(profile.periods, key=lambda r: r.start)
                if not periods:
                    return self._fail("还没有记录，先记一次开始")
                limit = int(profile.settings.cycle_length or 28)
                if not 1 <= want <= limit:
                    return self._fail(f"第几天应该在 1～{limit} 之间")
                latest = periods[-1]
                new_start = today - timedelta(days=want - 1)
                if len(periods) >= 2 and new_start <= periods[-2].start:
                    return self._fail("这样会跨到上一条记录，先把上一条改掉")
                if latest.end is not None:
                    span = max(0, (latest.end - latest.start).days)
                    latest.end = new_start + timedelta(days=span)
                latest.start = new_start
                profile.periods.sort(key=lambda r: r.start)
                changes.append(f"今天校正成第 {want} 天（{pretty_date(new_start)} 起算）")

            if "pause" in body:
                raw = str(body.get("pause") or "").strip()
                if not raw or raw.lower() in ("0", "off", "clear", "none"):
                    profile.settings.paused_until = ""
                    changes.append("观察期已解除")
                else:
                    days: int | None = None
                    try:
                        days = int(float(raw))
                    except ValueError:
                        days = None
                    if days is not None:
                        if not 1 <= days <= 365:
                            return self._fail("观察期天数在 1～365 之间")
                        until = today + timedelta(days=days)
                    else:
                        until = parse_date(raw)
                        if until is None:
                            return self._fail("观察期填天数或日期都行")
                    profile.settings.paused_until = until.isoformat()
                    changes.append(f"观察期到 {pretty_date(until)}")

            if "overrides" in body or "override" in body:
                raw = body.get("overrides", body.get("override"))
                if raw in (None, "", {}, [], "clear", "off", "none"):
                    profile.overrides = {}
                    profile.override_until = ""
                    changes.append("手动数值已清除")
                else:
                    values = _clean_overrides(raw)
                    if not values:
                        return self._fail(
                            "要填至少一项数值：valence/arousal/energy/stress/pain/sensitive/libido/social"
                        )
                    raw_hours = body.get("override_hours")
                    if raw_hours in (None, ""):
                        hours = 12.0
                    else:
                        try:
                            hours = float(raw_hours)
                        except (TypeError, ValueError):
                            return self._fail("时长要填数字")
                    if not 0.5 <= hours <= 168:
                        return self._fail("时长在 0.5～168 小时之间")
                    profile.overrides = values
                    profile.override_until = (moment + timedelta(hours=hours)).strftime(
                        DT_FMT
                    )
                    detail = "，".join(f"{key} {value:.2f}" for key, value in values.items())
                    changes.append(f"手动指定 {detail}（{hours:g} 小时内有效）")

            if not changes:
                return self._fail("没有要改的东西")

            profile.updated_at = _now_stamp()
            await self._store().upsert(profile)
            with contextlib.suppress(Exception):
                self.plugin.voice.invalidate(user_key)
            await self._after_change(profile)
            return self._ok(
                {
                    "ok": True,
                    "message": "；".join(changes),
                    "changes": changes,
                    "detail": await self._detail_payload(profile),
                }
            )
        except Exception as exc:
            self._log().exception("[menstrual] state_action 出错")
            return self._fail(f"操作失败：{exc}", status_code=500)

    # ------------------------------------------------------------------
    # POST /user/<user_key>/settings
    # ------------------------------------------------------------------
    async def save_settings(self, user_key: str = "") -> Any:
        try:
            body = await self._body()
            profile = await self._profile(user_key)
            if profile is None:
                return self._fail("没有这个用户", status_code=404)
            settings = profile.settings
            changes: list[str] = []

            if "nickname" in body:
                raw = str(body.get("nickname") or "").strip()
                if len(raw) > 40:
                    return self._fail("名字最多 40 个字")
                profile.nickname = raw
                changes.append(f"名字：{raw or '她'}")

            if "start_date" in body:
                raw = str(body.get("start_date") or "").strip()
                if raw:
                    day = parse_date(raw)
                    if day is None:
                        return self._fail("开始日要填日期（YYYY-MM-DD）")
                    problem = self._set_latest_start(profile, day)
                    if problem:
                        return self._fail(problem)
                    changes.append(f"最近一次开始：{pretty_date(day)}")

            for field, (low, high, whole) in NUMERIC_SETTINGS.items():
                if field not in body:
                    continue
                raw = body.get(field)
                try:
                    number = float(raw)
                except (TypeError, ValueError):
                    return self._fail(f"{LABELS[field]}要填数字")
                if number < low or number > high:
                    return self._fail(
                        f"{LABELS[field]}应该在 {low:g}～{high:g} 之间，收到 {raw}"
                    )
                value = int(round(number)) if whole else round(number, 2)
                setattr(settings, field, value)
                changes.append(f"{LABELS[field]}：{value:g}")

            if "reminder" in body:
                settings.reminder = bool(body.get("reminder"))
                changes.append(f"主动提醒：{'开' if settings.reminder else '关'}")

            if "reminder_style" in body:
                style = str(body.get("reminder_style") or "").strip()
                if style not in ("in_character", "reminder"):
                    return self._fail("提醒口吻只能是「用她的口吻」或「系统提醒」")
                settings.reminder_style = style
                changes.append(
                    "提醒口吻：" + ("她来说" if style == "in_character" else "系统提醒")
                )

            if "paused_until" in body:
                raw = str(body.get("paused_until") or "").strip()
                if not raw:
                    settings.paused_until = ""
                    changes.append("观察期：已解除")
                else:
                    day = parse_date(raw)
                    if day is None:
                        return self._fail("观察期要填日期（YYYY-MM-DD）")
                    settings.paused_until = day.isoformat()
                    changes.append(f"观察期到：{pretty_date(day)}")

            if "constitution" in body:
                preset = str(body.get("constitution") or "").strip().lower()
                if preset not in CONSTITUTIONS:
                    return self._fail("体质只能是普通 / 敏感型 / 经期重 / 轻")
                settings.constitution = preset
                changes.append(f"体质：{CONSTITUTIONS[preset]['label']}")

            if "phase_learning" in body:
                settings.phase_learning = bool(body.get("phase_learning"))
                changes.append(f"按她的自报校准：{'开' if settings.phase_learning else '关'}")

            if "phase_weight" in body:
                raw = body.get("phase_weight")
                try:
                    number = float(raw)
                except (TypeError, ValueError):
                    return self._fail("校准权重要填数字")
                if not 0.0 <= number <= 0.8:
                    return self._fail("校准权重应该在 0～0.8 之间")
                settings.phase_weight = round(number, 2)
                changes.append(f"校准权重：{settings.phase_weight:g}")

            profile.updated_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            await self._store().upsert(profile)
            await self._after_change(profile)
            return self._ok(
                {
                    "ok": True,
                    "message": "；".join(changes) if changes else "没有改动",
                    "changes": changes,
                    "detail": await self._detail_payload(profile),
                }
            )
        except Exception as exc:
            self._log().exception("[menstrual] save_settings 出错")
            return self._fail(f"保存失败：{exc}", status_code=500)

    # ------------------------------------------------------------------
    # POST /user/<user_key>/reset
    # ------------------------------------------------------------------
    async def reset_user(self, user_key: str = "") -> Any:
        try:
            profile = await self._profile(user_key)
            if profile is None:
                return self._fail("没有这个用户", status_code=404)
            await self._store().delete(user_key)
            with contextlib.suppress(Exception):
                self.plugin.voice.invalidate(user_key)
            self._log().info("[menstrual] 面板重置档案 %s", user_key)
            return self._ok({"ok": True, "message": "档案已清空", "removed": user_key})
        except Exception as exc:
            self._log().exception("[menstrual] reset_user 出错")
            return self._fail(f"重置失败：{exc}", status_code=500)

    # ------------------------------------------------------------------
    # POST /user/<user_key>/voice
    # ------------------------------------------------------------------
    async def rewrite_voice(self, user_key: str = "") -> Any:
        try:
            body = await self._body()
            action = str(body.get("action") or "write").strip().lower()
            profile = await self._profile(user_key)
            if profile is None:
                return self._fail("没有这个用户", status_code=404)
            state = compute_state(profile)
            voice = self.plugin.voice

            if action in ("clear", "reset"):
                voice.invalidate(user_key)
                return self._ok(
                    {
                        "ok": True,
                        "message": "已清掉缓存，下次会用内置文案或重新生成",
                        "voice": {"mode": self._voice_mode(), "status": voice.status_text(profile, state)},
                    }
                )

            if state is None:
                return self._fail("还没有周期记录，先记一次开始")

            if not voice.enabled:
                return self._fail("模型文案总开关是关的，去插件配置里打开 enable_llm_voice")

            if self.plugin._realtime():
                pack = await voice.live(profile, state, force=True)
            else:
                pack = await voice.ensure(profile, state, wait=True)

            if pack is None:
                reason = getattr(voice, "last_error", "") or "模型没给出可用的结构"
                return self._fail(f"这次没写成：{reason}", status_code=502)

            detail = await self._detail_payload(profile)
            return self._ok(
                {
                    "ok": True,
                    "message": f"已重写这一轮的说法（覆盖 {int(pack.coverage() * 100)}%）",
                    "detail": detail,
                    "voice": {"mode": self._voice_mode(), "status": voice.status_text(profile, state)},
                }
            )
        except Exception as exc:
            self._log().exception("[menstrual] rewrite_voice 出错")
            return self._fail(f"生成失败：{exc}", status_code=500)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _set_latest_start(self, profile: UserProfile, day: date) -> str:
        """把最近一次经期的开始日挪到 day；还没有记录就补一条。返回错误文案或空串。"""
        today = date.today()
        if day > today:
            return "开始日不能是未来"
        periods = sorted(profile.periods, key=lambda r: r.start)
        if not periods:
            profile.periods.append(
                PeriodRecord(
                    id=_new_id(),
                    start=day,
                    end=None,
                    note="",
                    created_at=_now_stamp(),
                )
            )
            return ""
        if len(periods) > 1 and day <= periods[-2].start:
            return f"不能早于上一条（{pretty_date(periods[-2].start)}）"
        latest = periods[-1]
        span = (latest.end - latest.start).days if latest.end else 0
        latest.start = day
        if latest.end:
            latest.end = day + timedelta(days=span)
            if latest.end > today:
                latest.end = None
        profile.periods.sort(key=lambda r: r.start)
        return ""

    @staticmethod
    def _parse_range(
        body: dict[str, Any], today: date
    ) -> tuple[date | None, date | None, str]:
        """从 body 里取 start/end，返回 (start, end, 错误文案)。"""
        start = parse_date(body.get("start") or body.get("date"))
        if start is None:
            return None, None, "开始日要填日期（YYYY-MM-DD）"
        if start > today:
            return None, None, "开始日不能是未来"
        raw_end = body.get("end")
        if raw_end in (None, "", "null", "None"):
            return start, None, ""
        end = parse_date(raw_end)
        if end is None:
            return None, None, "结束日要填日期（YYYY-MM-DD）"
        if end > today:
            return None, None, "结束日不能是未来"
        if end < start:
            return None, None, "结束日不能早于开始日"
        if (end - start).days + 1 > 15:
            return None, None, "一次经期不会超过 15 天，检查一下日期"
        return start, end, ""

    @staticmethod
    def _find_clash(
        periods: list[PeriodRecord], start: date, end: date | None, skip: str = ""
    ) -> str:
        """时间段和已有记录撞了就给一句人话，没撞返回空串。"""
        last = end or start
        for item in periods:
            if skip and item.id == skip:
                continue
            their_start = item.start
            their_end = item.end or item.start
            if their_start <= last and start <= their_end:
                return f"和 {pretty_date(item.start)} 那条重叠了，先改那一条"
        return ""

    async def _body(self) -> dict[str, Any]:
        if web_request is None:
            return {}
        data = await web_request.json({})
        return data if isinstance(data, dict) else {}

    async def _detail_payload(self, profile: UserProfile) -> dict[str, Any]:
        """写完立刻回一份最新的详情，省一次往返。"""
        today = date.today()
        state = compute_state(profile, today)
        return {
            "user": {
                "user_key": profile.user_key,
                "nickname": profile.nickname or "",
                "platform": profile.platform or "",
                "umo": profile.last_umo or "",
                "timezone": profile.timezone or "",
                "paused": profile.paused(today),
                "updated_at": profile.updated_at or "",
            },
            "state": state_payload(state),
            "settings": settings_payload(profile),
            "records": [
                record_payload(item)
                for item in sorted(profile.periods, key=lambda r: r.start, reverse=True)
            ],
            "stats": cycle_stats(profile),
            "predictions": [
                {"start": _iso(begin), "end": _iso(end)}
                for begin, end in predict_next_periods(profile, 3)
            ],
            "pain_logs": [
                {"date": key, "level": _round(value)}
                for key, value in sorted(profile.pain_logs.items())
            ][-20:],
            "comfort_options": [
                {"kind": item.kind, "label": item.label, "hours": item.hours}
                for item in cm.RULES
            ],
            "constitution_options": [
                {"key": key, "label": item["label"], "hint": item["hint"]}
                for key, item in CONSTITUTIONS.items()
            ],
            "learned": self._learned(profile),
            "voice": {
                "mode": self._voice_mode(),
                "status": self.plugin.voice.status_text(profile, state),
            },
            "overrides": {
                "values": dict(state.overrides) if state else {},
                "until": state.override_until if state else "",
                "fields": list(VECTOR_FIELDS),
            },
            "trend": self._trend(profile, today),
            "calendar": self._calendar(profile, today),
            "injection": self._injection_text(profile, state),
        }

    def _learned(self, profile: UserProfile) -> dict[str, Any]:
        """她每个阶段自己报过的疼痛：够不够用来校准、现在校准到多少。"""
        phases: dict[str, Any] = {}
        for key in ("menstrual", "follicular", "ovulation", "luteal", "pms"):
            values = profile.phase_pain.get(key) or []
            baseline = phase_baseline(profile, key)
            phases[key] = {
                "samples": len(values),
                "median": _round(baseline) if baseline is not None else None,
                "active": baseline is not None,
            }
        return {
            "enabled": bool(profile.settings.phase_learning),
            "weight": _round(profile.settings.phase_weight, 2),
            "phases": phases,
        }

    def _calendar(
        self, profile: UserProfile, today: date, back: int = 45, ahead: int = 60
    ) -> list[dict[str, Any]]:
        """按天给她在日历上的样子：真经期、预测经期、当天有没有自报疼痛。"""
        records = profile.sorted_periods()
        if not records:
            return []
        span = max(2, min(12, profile.settings.period_length))
        actual: list[tuple[date, date]] = []
        for item in records:
            end = item.end or min(today, item.start + timedelta(days=span - 1))
            actual.append((item.start, max(item.start, end)))
        future = [
            (begin, end) for begin, end in predict_next_periods(profile, 4) if begin > today
        ]
        out: list[dict[str, Any]] = []
        for offset in range(-back, ahead + 1):
            day = today + timedelta(days=offset)
            kind = "none"
            for begin, end in actual:
                if begin <= day <= end:
                    kind = "period"
                    break
            if kind == "none":
                for begin, end in future:
                    if begin <= day <= end:
                        kind = "predicted"
                        break
            logged = profile.pain_logs.get(day.isoformat())
            out.append(
                {
                    "date": _iso(day),
                    "kind": kind,
                    "pain": _round(float(logged)) if logged is not None else None,
                    "today": day == today,
                }
            )
        return out

    def _perf_payload(self) -> dict[str, Any]:
        """插件自己花的时间：注入了几次、多慢、各档位用得多不多。"""
        perf = getattr(self.plugin, "_perf", None) or {}
        recent = [
            float(value)
            for value in (perf.get("recent") or [])
            if isinstance(value, (int, float))
        ]
        levels = dict(perf.get("levels") or {})
        base = {"count": int(perf.get("count") or 0), "levels": levels}
        if not recent:
            return {**base, "avg": None, "p50": None, "p95": None, "last": None, "recent": []}
        ordered = sorted(recent)

        def pick(ratio: float) -> float:
            index = int(round(ratio * (len(ordered) - 1)))
            return ordered[max(0, min(len(ordered) - 1, index))]

        return {
            **base,
            "avg": _round(sum(recent) / len(recent), 2),
            "p50": _round(pick(0.5), 2),
            "p95": _round(pick(0.95), 2),
            "last": _round(recent[-1], 2),
            "recent": [_round(value, 2) for value in recent[-30:]],
        }

    def _trend(
        self, profile: UserProfile, today: date, days: int = 7
    ) -> list[dict[str, Any]]:
        """最近这几天她怎么走过来的：给面板画小曲线。"""
        out: list[dict[str, Any]] = []
        for offset in range(days - 1, -1, -1):
            day = today - timedelta(days=offset)
            state = compute_state(profile, day, ignore_overrides=day != today)
            if state is None:
                out.append(
                    {
                        "date": _iso(day),
                        "pain": None,
                        "mood": None,
                        "phase": "",
                        "phase_name": "",
                        "cycle_day": None,
                    }
                )
                continue
            out.append(
                {
                    "date": _iso(day),
                    "pain": _round(state.vector.pain),
                    "mood": _round(state.vector.valence),
                    "phase": state.phase.key,
                    "phase_name": state.phase.name,
                    "cycle_day": state.cycle_day,
                }
            )
        return out

    def _injection_text(self, profile: UserProfile, state: Any) -> str:
        """面板上直接看「到模型眼里的那段话」。"""
        if not state:
            return ""
        with contextlib.suppress(Exception):
            return tt.build_injection(
                profile,
                state,
                max_chars=int(self.plugin._cfg("injection_max_chars", 900)),
                include_numbers=bool(self.plugin._cfg("injection_numbers", True)),
                voice=self.plugin.voice.cached(profile, state),
            )
        return ""

    async def _after_change(self, profile: UserProfile) -> None:
        """面板改完数据后，把情绪推给联动插件（如果开了自动同步）。"""
        with contextlib.suppress(Exception):
            state = compute_state(profile)
            await self.plugin._auto_sync(profile, state)


__all__ = ["DashboardAPI", "PLUGIN_NAME", "WEB_READY", "state_payload"]
