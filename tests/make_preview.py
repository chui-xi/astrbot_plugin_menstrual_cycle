"""生成控制台的静态预览：真实后端输出 + 真前端渲染，用来看样式有没有崩。

产物：E:\\项目\\插件\\_menstrual_selftest\\dashboard_preview.html
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import types
from datetime import date, timedelta
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
ROOT = PLUGIN.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="menstrual-preview-"))


def make_module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _Filter:
    def __getattr__(self, _name):
        def deco(*_a, **_kw):
            def wrap(fn):
                return fn

            return wrap

        return deco


class AstrMessageEvent:
    message_str = ""
    unified_msg_origin = "aiocqhttp:FriendMessage:10001"


class Star:
    logger = __import__("logging").getLogger("preview")

    def __init__(self, context=None):
        self.context = context


class Context:
    def __init__(self):
        self.sent = []
        self.stars = {}
        self.web_apis = []

    def register_web_api(self, route, view_handler, methods, desc):
        self.web_apis.append((route, view_handler, methods, desc))

    def get_all_stars(self):
        return []

    def get_registered_star(self, name):
        return None

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))


class MessageChain:
    def message(self, text):
        return self

    def at(self, *_a, **_kw):
        return self


class ProviderRequest:
    def __init__(self):
        self.system_prompt = ""


class AstrBotConfig(dict):
    pass


class _FakeWebRequest:
    payload: dict = {}

    async def json(self, default=None):
        return dict(self.payload)


WEB_REQUEST = _FakeWebRequest()

make_module("astrbot")
make_module("astrbot.api", AstrBotConfig=AstrBotConfig, logger=Star.logger)
make_module("astrbot.api.event", AstrMessageEvent=AstrMessageEvent, filter=_Filter())
make_module("astrbot.api.star", Context=Context, Star=Star)
make_module("astrbot.api.provider", ProviderRequest=ProviderRequest)
make_module("astrbot.api.message_chain", MessageChain=MessageChain)
make_module(
    "astrbot.api.web",
    request=WEB_REQUEST,
    json_response=lambda data=None, **kw: data,
    error_response=lambda message, **kw: {"ok": False, "message": message},
)
make_module(
    "astrbot.core.utils.astrbot_path",
    get_astrbot_data_path=lambda: str(TMP),
)

from astrbot_plugin_menstrual_cycle.core.engine import compute_state  # noqa: E402
from astrbot_plugin_menstrual_cycle.core.models import (  # noqa: E402
    PeriodRecord,
    UserProfile,
)
from astrbot_plugin_menstrual_cycle.core.store import CycleStore  # noqa: E402
from astrbot_plugin_menstrual_cycle.core.webapi import DashboardAPI  # noqa: E402
from astrbot_plugin_menstrual_cycle.main import MenstrualCyclePlugin  # noqa: E402


def build() -> UserProfile:
    profile = UserProfile(
        user_key="character:self",
        nickname="小鹿",
        platform="",
    )
    for index, start in enumerate(
        [date(2026, 8, 20), date(2026, 9, 17), date(2026, 10, 15)]
    ):
        profile.periods.append(
            PeriodRecord(
                id=f"rec{index}",
                start=start,
                end=start + timedelta(days=4),
                note="第二天最疼" if index == 2 else "",
            )
        )
    profile.pain_logs[date(2026, 11, 14).isoformat()] = 0.8
    return profile


async def main() -> None:
    import asyncio

    context = Context()
    plugin = MenstrualCyclePlugin(context, AstrBotConfig())
    plugin.data_dir = TMP
    plugin.store = CycleStore(TMP / "cycles.json")
    await plugin.store.load()

    profile = build()
    await plugin.store.upsert(profile)

    # 造一个「刚被哄过」的状态：塞一条还在有效期内的关心记录
    from astrbot_plugin_menstrual_cycle.core import comfort as cm

    state = compute_state(profile)
    today = date.today()
    if state is not None:
        cm.register(profile, "我给你倒了杯热水，过来抱抱", hours=6)
        await plugin.store.upsert(profile)

    api = DashboardAPI(plugin)
    # 管理员手动钉住两项数值，让面板上的「直接钉住情绪数值」有内容可看
    WEB_REQUEST.payload = {"overrides": {"pain": 0.86, "libido": 0.9}, "override_hours": 10}
    await api.state_action(profile.user_key)
    overview = await api.overview()
    detail = await api.user_detail(profile.user_key)

    html = io.open(PLUGIN / "views" / "dashboard" / "index.html", encoding="utf-8").read()
    css = io.open(PLUGIN / "views" / "dashboard" / "style.css", encoding="utf-8").read()
    js = io.open(PLUGIN / "views" / "dashboard" / "app.js", encoding="utf-8").read()

    html = html.replace(
        '<link rel="stylesheet" href="./style.css" />',
        f"<style>\n{css}\n</style>",
    )

    mock = f"""
<script>
  window.__MOCK__ = {{
    overview: {json.dumps(overview, ensure_ascii=False)},
    detail: {json.dumps(detail, ensure_ascii=False)}
  }};
  window.AstrBotPluginView = {{
    ready: () => Promise.resolve({{ isDark: %DARK%, locale: "zh-CN" }}),
    onContext: () => () => {{}},
    apiGet: async (endpoint) => {{
      if (endpoint === "overview") return window.__MOCK__.overview;
      if (String(endpoint).startsWith("detail")) return window.__MOCK__.detail;
      if (String(endpoint).startsWith("user/")) return window.__MOCK__.detail;
      throw new Error("mock 没有这个接口: " + endpoint);
    }},
    apiPost: async () => ({{ ok: true, message: "这是静态预览，不会写数据" }})
  }};
</script>
"""

    for name, dark in (("preview_dark", "true"), ("preview_light", "false")):
        page = html.replace(
            '<script src="./app.js" type="module"></script>',
            mock.replace("%DARK%", dark) + f'<script type="module">\n{js}\n</script>',
        )
        out = PLUGIN / "tests" / f"dashboard_{name}.html"
        io.open(out, "w", encoding="utf-8", newline="\n").write(page)
        print("written", out, out.stat().st_size, "bytes")

    # 编辑态：dark 版 + 自动点开第一行的「编辑」
    click = """
<script type="module">
  const timer = setInterval(() => {
    const btn = document.querySelector('[data-act="edit"]');
    if (btn) {
      btn.click();
      clearInterval(timer);
    }
  }, 200);
</script>
"""
    page = html.replace(
        '<script src="./app.js" type="module"></script>',
        mock.replace("%DARK%", "true") + f'<script type="module">\n{js}\n</script>' + click,
    )
    out = PLUGIN / "tests" / "dashboard_preview_edit.html"
    io.open(out, "w", encoding="utf-8", newline="\n").write(page)
    print("written", out, out.stat().st_size, "bytes")

    # 空面板 + 打开「新建档案」表单
    ov = dict(overview.get("data", overview))
    empty = dict(ov)
    empty["users"] = []
    empty["stats"] = {"users": 0, "active": 0, "irregular": 0, "records": 0}
    if not empty.get("platforms"):
        empty["platforms"] = [{"id": "aiocqhttp", "type": "aiocqhttp"}]
    mock_empty = f"""
<script>
  window.__MOCK__ = {{
    overview: {json.dumps(empty, ensure_ascii=False)},
    detail: null
  }};
  window.AstrBotPluginView = {{
    ready: () => Promise.resolve({{ isDark: true, locale: "zh-CN" }}),
    onContext: () => () => {{}},
    apiGet: async (endpoint) => {{
      if (endpoint === "overview") return window.__MOCK__.overview;
      throw new Error("空面板不该请求 " + endpoint);
    }},
    apiPost: async () => ({{ ok: true, message: "这是静态预览，不会写数据" }})
  }};
</script>
"""
    open_new = """
<script type="module">
  const timer = setInterval(() => {
    const btn = document.querySelector('[data-act="new-user"]');
    if (btn) {
      btn.click();
      clearInterval(timer);
    }
  }, 200);
</script>
"""
    page = html.replace(
        '<script src="./app.js" type="module"></script>',
        mock_empty + f'<script type="module">\n{js}\n</script>' + open_new,
    )
    out = PLUGIN / "tests" / "dashboard_preview_new.html"
    io.open(out, "w", encoding="utf-8", newline="\n").write(page)
    print("written", out, out.stat().st_size, "bytes")

    print("today", today, "phase", state.phase.key if state else "none")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
