"""待办清单模块 — 持久化存储 + 微信命令交互

支持：
  /todo add 买菜               → 添加待办
  /todo done 1                 → 完成待办
  /todo undo 1                 → 取消完成
  /todo del 1                  → 删除待办
  /todo list                   → 查看待办列表
  /todo clean                  → 清理已完成项
  /todo 买菜                   → 简写，等同 /todo add 买菜

存储: .clawbot/todos.json
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

TODOS_FILE = Path(__file__).parent / "todos.json"


class TodoItem:
    def __init__(self, text: str, tid: int = 0, done: bool = False,
                 created_at: str = "", priority: str = ""):
        self.id = tid
        self.text = text
        self.done = done
        self.created_at = created_at or datetime.now().strftime("%Y-%m-%d %H:%M")
        self.priority = priority  # "" | "🔴" | "🟡" | "🟢"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "done": self.done,
            "created_at": self.created_at,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TodoItem":
        return cls(
            text=d["text"],
            tid=d.get("id", 0),
            done=d.get("done", False),
            created_at=d.get("created_at", ""),
            priority=d.get("priority", ""),
        )


def load_todos() -> list[TodoItem]:
    if not TODOS_FILE.exists():
        return []
    try:
        data = json.loads(TODOS_FILE.read_text(encoding="utf-8"))
        return [TodoItem.from_dict(d) for d in data]
    except Exception:
        return []


def save_todos(todos: list[TodoItem]):
    # 重新编号
    for i, t in enumerate(todos, 1):
        t.id = i
    TODOS_FILE.write_text(
        json.dumps([t.to_dict() for t in todos], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def format_todo_list(todos: list[TodoItem]) -> str:
    if not todos:
        return "📭 待办清单为空"

    pending = [t for t in todos if not t.done]
    done = [t for t in todos if t.done]

    lines = []
    if pending:
        lines.append("📋 待办：")
        for t in pending:
            pri = f"{t.priority} " if t.priority else ""
            lines.append(f"  [{t.id}] {pri}{t.text}")
    if done:
        lines.append(f"\n✅ 已完成（{len(done)}项）：")
        for t in done[:5]:  # 最多显示5项
            lines.append(f"  [{t.id}] {t.text}")
        if len(done) > 5:
            lines.append(f"  ...还有{len(done)-5}项")

    return "\n".join(lines)