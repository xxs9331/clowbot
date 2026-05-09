from __future__ import annotations

from datetime import date

from .contracts import TodoRepository, VaultRepository


class DomainServices:
    def __init__(self, vault: VaultRepository, todo: TodoRepository):
        self.vault = vault
        self.todo = todo

    async def execute(self, *, user_id: str, tool: str, payload: dict) -> tuple[bool, str]:
        if tool == "none":
            return True, ""
        if tool == "record.add":
            text = str(payload.get("text") or "").strip()
            category = str(payload.get("category") or "事务").strip() or "事务"
            event_date = str(payload.get("event_date") or date.today().isoformat()).strip()
            if not text:
                return True, "我没读懂这条记录。"
            out = await self.vault.append_record(
                text=text, category=category, event_date=event_date
            )
            return True, out
        if tool == "remind.add":
            text = str(payload.get("text") or "").strip()
            hhmm = str(payload.get("hhmm") or "").strip()
            event_date = str(payload.get("event_date") or date.today().isoformat()).strip()
            if not text or not hhmm:
                return True, "几点叫你？请补一个时间。"
            out = await self.vault.append_reminder(
                text=text, hhmm=hhmm, event_date=event_date
            )
            return True, out
        if tool == "timeline.append":
            text = str(payload.get("text") or "").strip()
            slot = str(payload.get("slot") or "").strip()
            if not text:
                return True, "要写的内容是哪一句？"
            out = await self.vault.upsert_timeline_slot(slot=slot, text=text)
            return True, out
        if tool == "todo.merge_new_items":
            tasks = payload.get("tasks")
            if not isinstance(tasks, list):
                return True, "待办内容为空，请重发。"
            cleaned = [str(x).strip() for x in tasks if str(x).strip()]
            if not cleaned:
                return True, "待办内容为空，请重发。"
            out = await self.todo.merge(user_id=user_id, tasks=cleaned)
            return True, out
        if tool == "todo.done_current":
            done, nxt = await self.todo.done_current(user_id=user_id)
            if nxt:
                return True, f"{done}\n下一个：{nxt}"
            return True, f"{done}\n全部完成"
        if tool == "todo.next":
            nxt = await self.todo.next_task(user_id=user_id)
            if nxt:
                return True, f"你现在先做：{nxt}"
            return True, "当前没有进行中的短待办。"
        if tool == "todo.not_done":
            return True, await self.todo.not_done(user_id=user_id)
        if tool == "todo.reorder":
            order = payload.get("reorder")
            if not isinstance(order, list):
                return True, "顺序建议我收到了，但还不能安全改队列，请你再确认一次。"
            cleaned = [str(x).strip() for x in order if str(x).strip()]
            if not cleaned:
                return True, "顺序建议我收到了，但还不能安全改队列，请你再确认一次。"
            return True, await self.todo.reorder(user_id=user_id, order=cleaned)
        if tool == "todo.reorder_confirm":
            return True, await self.todo.reorder_confirm(user_id=user_id)
        if tool == "todo.skip_current":
            msg, nxt = await self.todo.skip_current(user_id=user_id)
            if nxt:
                return True, f"{msg}\n现在先做：{nxt}，做完了吗？"
            return True, msg
        if tool == "todo.abandon_current":
            msg, nxt = await self.todo.abandon_current(user_id=user_id)
            if nxt:
                return True, f"{msg}\n下一个：{nxt}"
            return True, msg
        return False, ""
