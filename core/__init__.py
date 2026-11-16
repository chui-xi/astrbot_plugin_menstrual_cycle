"""核心逻辑：数据模型、周期引擎、存储、文案、跨插件联动。

这里刻意不在包级别 re-export：各子模块互相独立，方便脱离 AstrBot 环境做纯函数单测。
"""

__all__ = ["engine", "models", "selflearn", "store", "text"]
