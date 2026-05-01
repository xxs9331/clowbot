"""三域 Coach Mixin。

每个 Coach 模块在被 import 时通过 dispatcher.register_tool_handler 把自己的方法
登记到 dispatcher 表，避免 dispatcher → coaches 的反向 import。
"""

from .record import RecordCoachMixin
from .remind import RemindCoachMixin
from .todo import TodoCoachMixin

__all__ = ["TodoCoachMixin", "RecordCoachMixin", "RemindCoachMixin"]
