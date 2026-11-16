"""在「影子 AstrBot 4.29.3」里导入插件，验证跨版本导入面。

4.29 重构了内部结构：`astrbot/core/message/` 下只剩 components.py 与
message_event_result.py，`message_chain.py` 已经不存在。这个脚本按 4.29.3
真实的模块布局搭一个最小 astrbot 包（故意**不提供** message_chain 模块），
然后 import 插件，能提前发现"装上去但加载失败"这类问题。
"""

from __future__ import annotations

import importlib
import sys
import tempfile
import types
from pathlib import Path

PLUGIN_PARENT = Path(__file__).resolve().parent.parent
PLUGIN_NAME = "astrbot_plugin_menstrual_cycle"

FILES: dict[str, str] = {
    "astrbot/__init__.py": "import logging\nlogger = logging.getLogger('astrbot')\n",
    "astrbot/api/__init__.py": (
        "import logging\n"
        "from astrbot.core.config.astrbot_config import AstrBotConfig\n"
        "logger = logging.getLogger('astrbot')\n"
        "__all__ = ['AstrBotConfig', 'logger']\n"
    ),
    "astrbot/api/event/__init__.py": (
        "from astrbot.core.message.message_event_result import MessageChain\n"
        "from astrbot.core.platform import AstrMessageEvent\n"
        "__all__ = ['AstrMessageEvent', 'MessageChain']\n"
    ),
    "astrbot/api/event/filter/__init__.py": (
        "def _passthrough(*a, **k):\n"
        "    def deco(fn):\n"
        "        return fn\n"
        "    return deco\n"
        "command = event_message_type = regex = llm_tool = on_llm_request = _passthrough\n"
        "on_llm_response = on_decorating_result = after_message_sent = _passthrough\n"
    ),
    "astrbot/api/star/__init__.py": (
        "from astrbot.core.star import Context, Star\n"
        "__all__ = ['Context', 'Star']\n"
    ),
    "astrbot/api/provider/__init__.py": (
        "class ProviderRequest:\n"
        "    def __init__(self):\n"
        "        self.system_prompt = ''\n"
        "__all__ = ['ProviderRequest']\n"
    ),
    "astrbot/core/__init__.py": "",
    "astrbot/core/config/__init__.py": "",
    "astrbot/core/config/astrbot_config.py": (
        "class AstrBotConfig(dict):\n"
        "    pass\n"
    ),
    "astrbot/core/platform/__init__.py": (
        "class AstrMessageEvent:\n"
        "    unified_msg_origin = 'aiocqhttp:GroupMessage:1'\n"
        "    message_str = ''\n"
        "    def plain_result(self, text):\n"
        "        return text\n"
        "__all__ = ['AstrMessageEvent']\n"
    ),
    "astrbot/core/star/__init__.py": (
        "class Star:\n"
        "    def __init__(self, context=None):\n"
        "        self.context = context\n"
        "class Context:\n"
        "    pass\n"
        "__all__ = ['Context', 'Star']\n"
    ),
    "astrbot/core/message/__init__.py": "",
    # 4.29.3 的真实位置：MessageChain 在 message_event_result 里
    "astrbot/core/message/message_event_result.py": (
        "class MessageChain:\n"
        "    def __init__(self, chain=None, type=None):\n"
        "        self.chain = chain or []\n"
        "    def message(self, text):\n"
        "        self.chain.append(text)\n"
        "        return self\n"
        "class MessageEventResult(MessageChain):\n"
        "    pass\n"
    ),
    "astrbot/core/utils/__init__.py": "",
    "astrbot/core/utils/astrbot_path.py": (
        "import os\n"
        "def get_astrbot_data_path() -> str:\n"
        "    return os.environ.get('SHADOW_DATA', os.getcwd())\n"
    ),
}


def build(root: Path) -> None:
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="shadow-astrbot-"))
    build(tmp)
    sys.path.insert(0, str(PLUGIN_PARENT))
    sys.path.insert(0, str(tmp))

    problems: list[str] = []
    for stale in (
        "astrbot.api.message_chain",
        "astrbot.core.message.message_chain",
    ):
        try:
            importlib.import_module(stale)
        except Exception:
            continue
        problems.append(f"影子环境不该有 {stale}")

    try:
        module = importlib.import_module(f"{PLUGIN_NAME}.main")
    except Exception as exc:
        print(f"FAIL 导入失败：{type(exc).__name__}: {exc}")
        return 1

    chain = getattr(module, "MessageChain", None)
    print(f"PASS 插件顶层导入通过，MessageChain = {getattr(chain, '__name__', chain)}")
    print(f"PASS 找到消息链并可用：{chain().message('测试') is not None}")

    plugin_cls = getattr(module, "MenstrualCyclePlugin", None)
    print(f"PASS 插件类：{plugin_cls.__name__ if plugin_cls else '缺失'}")
    if plugin_cls is None:
        return 1

    # 配置实体是 dict 子类；直接构造会走 _load_config，用最小 config 试一次
    try:
        instance = plugin_cls.__new__(plugin_cls)
        instance.__dict__["_cfg_data"] = {}
        names = ("_cfg", "_realtime", "_new_voice")
        missing = [n for n in names if not hasattr(plugin_cls, n)]
        if missing:
            problems.append(f"缺少方法：{missing}")
    except Exception as exc:
        problems.append(f"实例化失败：{type(exc).__name__}: {exc}")

    if problems:
        for item in problems:
            print(f"FAIL {item}")
        return 1
    print("PASS 跨版本导入面检查完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
