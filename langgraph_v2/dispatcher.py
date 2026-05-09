from __future__ import annotations

import asyncio
import hashlib
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from config import LOG_DIR
from utils.flow_log import log_flow_event


class DualPathDispatcher:
    """Legacy + LangGraph dual-path dispatcher with rollout and shadow mode."""

    def __init__(self, *, graph, config: dict):
        self._graph = graph
        self._cfg = (config.get("graph") or {}) if isinstance(config, dict) else {}
        self._shadow_tasks: set[asyncio.Task] = set()

    def _enabled(self) -> bool:
        return bool(self._cfg.get("enabled", False))

    def _shadow_mode(self) -> bool:
        return bool(self._cfg.get("shadow_mode", False))

    def _rollout_rate(self) -> float:
        try:
            rate = float(self._cfg.get("rollout_rate", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, rate))

    def _in_rollout_users(self, user_id: str) -> bool:
        users = self._cfg.get("rollout_users") or []
        if not isinstance(users, list):
            return False
        return user_id in {str(x).strip() for x in users if str(x).strip()}

    def _sample_hit(self, user_id: str) -> bool:
        if self._in_rollout_users(user_id):
            return True
        rate = self._rollout_rate()
        if rate <= 0:
            return False
        if rate >= 1:
            return True
        stable = bool(self._cfg.get("rollout_stable_bucket", False))
        if stable:
            h = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
            bucket = int(h[:8], 16) / 0xFFFFFFFF
            return bucket < rate
        return random.random() < rate

    async def ainvoke_graph(
        self,
        *,
        text: str,
        from_user: str,
        context_token: str,
        image_base64: str = "",
        image_mime: str = "",
        agent_mode: bool = False,
    ) -> dict:
        payload = {
            "text": text,
            "image_base64": image_base64,
            "image_mime": image_mime,
            "from_user": from_user,
            "context_token": context_token,
            "agent_mode": agent_mode,
        }
        out = await self._graph.ainvoke(payload)
        return out if isinstance(out, dict) else {}

    @staticmethod
    def _extract_reply(state: dict) -> str:
        wx_out = state.get("wx_out")
        if isinstance(wx_out, list) and wx_out:
            return str(wx_out[0] or "").strip()
        return str(state.get("reply") or "").strip()

    def _shadow_log_path(self) -> Path:
        raw = str(self._cfg.get("shadow_log_dir") or "").strip()
        if raw:
            p = Path(raw)
            if not p.is_absolute():
                p = Path.cwd() / p
            p.mkdir(parents=True, exist_ok=True)
            return p / f"shadow-{datetime.now().strftime('%Y-%m-%d')}.log"
        return LOG_DIR / f"shadow-{datetime.now().strftime('%Y-%m-%d')}.log"

    def _write_shadow_log(self, item: dict) -> None:
        p = self._shadow_log_path()
        line = json.dumps(item, ensure_ascii=False, default=str)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _spawn_shadow(
        self,
        *,
        text: str,
        from_user: str,
        context_token: str,
    ) -> None:
        async def _job():
            try:
                state = await self.ainvoke_graph(
                    text=text,
                    from_user=from_user,
                    context_token=context_token,
                )
                self._write_shadow_log(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "from_user_tail": from_user[-6:] if from_user else "",
                        "text": text[:500],
                        "decision": state.get("decision"),
                        "reply": self._extract_reply(state)[:500],
                        "error": state.get("error", ""),
                    }
                )
            except Exception as e:  # noqa: BLE001
                log_flow_event(
                    stage="graph",
                    route="shadow_failed",
                    user_text=text[:200],
                    from_user=from_user,
                    extra={"error": str(e)[:200]},
                )

        task = asyncio.create_task(_job())
        self._shadow_tasks.add(task)
        task.add_done_callback(self._shadow_tasks.discard)

    async def dispatch_text(
        self,
        *,
        text: str,
        from_user: str,
        context_token: str,
        legacy_runner: Callable[[], Awaitable[None]],
        send_text: Callable[[str, str, str], Awaitable[object]],
    ) -> None:
        if not self._enabled():
            await legacy_runner()
            return

        sample_hit = self._sample_hit(from_user)

        if self._shadow_mode():
            await legacy_runner()
            if sample_hit:
                self._spawn_shadow(
                    text=text,
                    from_user=from_user,
                    context_token=context_token,
                )
            return

        if not sample_hit:
            await legacy_runner()
            return

        try:
            state = await self.ainvoke_graph(
                text=text,
                from_user=from_user,
                context_token=context_token,
            )
            reply = self._extract_reply(state)
            if reply:
                await send_text(reply, from_user, context_token)
                return
            await legacy_runner()
        except Exception as e:  # noqa: BLE001
            log_flow_event(
                stage="graph",
                route="visible_failed_fallback_legacy",
                user_text=text[:200],
                from_user=from_user,
                extra={"error": str(e)[:200]},
            )
            await legacy_runner()
