"""后台事件注入存储：单用户、内存态、带 TTL。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import time
import uuid


Priority = str  # immediate | deferred | silent


@dataclass
class InjectedEvent:
    id: str
    kind: str
    summary: str
    priority: Priority
    source: str
    created_at: float
    expires_at: float | None
    consumed: bool = False


class InjectedContextStore:
    """单用户后台事件池。"""

    def __init__(self):
        self._events: list[InjectedEvent] = []

    @staticmethod
    def _default_ttl_sec(kind: str) -> float:
        k = str(kind or "").strip().lower()
        if k == "disk_write_done":
            return 180.0
        if k.startswith("checkin_slot_") or k == "todo_nudge_due":
            return 1800.0
        if k == "reminder_due":
            now = datetime.now()
            end = datetime.combine(now.date(), datetime.max.time()).replace(microsecond=0)
            return max((end - now).total_seconds(), 60.0)
        return 1800.0

    def add_event(
        self,
        *,
        kind: str,
        summary: str,
        priority: Priority = "deferred",
        ttl_sec: float | None = None,
        source: str = "",
    ) -> str:
        now = time.time()
        ttl = float(ttl_sec) if ttl_sec is not None else self._default_ttl_sec(kind)
        exp = (now + ttl) if ttl > 0 else None
        eid = uuid.uuid4().hex[:12]
        self._events.append(
            InjectedEvent(
                id=eid,
                kind=str(kind or "").strip(),
                summary=str(summary or "").strip(),
                priority=str(priority or "deferred").strip(),
                source=str(source or "").strip(),
                created_at=now,
                expires_at=exp,
                consumed=False,
            )
        )
        return eid

    def prune_expired(self) -> int:
        now = time.time()
        kept: list[InjectedEvent] = []
        removed = 0
        for ev in self._events:
            if ev.consumed:
                removed += 1
                continue
            if ev.expires_at is not None and ev.expires_at <= now:
                removed += 1
                continue
            kept.append(ev)
        self._events = kept
        return removed

    def peek_events(self, *, priority: Priority | None = None) -> list[InjectedEvent]:
        now = time.time()
        out: list[InjectedEvent] = []
        for ev in self._events:
            if ev.consumed:
                continue
            if ev.expires_at is not None and ev.expires_at <= now:
                continue
            if priority and ev.priority != priority:
                continue
            out.append(ev)
        out.sort(key=lambda x: x.created_at)
        return out

    def has_immediate(self) -> bool:
        return any(True for _ in self.peek_events(priority="immediate"))

    def mark_consumed(self, event_ids: list[str]) -> int:
        if not event_ids:
            return 0
        wanted = set(str(x).strip() for x in event_ids if str(x).strip())
        cnt = 0
        for ev in self._events:
            if ev.id in wanted and not ev.consumed:
                ev.consumed = True
                cnt += 1
        return cnt

    def peek_compose_summary(self, *, max_items: int = 3) -> tuple[str, list[str]]:
        events = [
            ev
            for ev in self.peek_events()
            if ev.priority in ("deferred", "silent")
            and ev.summary
        ][: max(1, int(max_items or 1))]
        if not events:
            return "", []
        lines = ["【可顺带提及的后台事件】这些不是用户说的，是系统事件。"]
        for ev in events:
            lines.append(f"- {ev.summary}")
        lines.append("若和当前对话相关可自然带一句，最多 1 条；不要生硬罗列。")
        return "\n".join(lines) + "\n", [ev.id for ev in events]
