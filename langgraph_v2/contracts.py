from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class Decision:
    tool: str
    payload: dict
    reply: str


class LLMProvider(Protocol):
    async def structured_decide(
        self, *, user_id: str, text: str, queue_snapshot: list[str]
    ) -> Decision:
        ...


class ImageLLMProvider(Protocol):
    async def describe_image(self, *, image_base64: str, image_mime: str) -> str:
        ...


class IntentClassifier(Protocol):
    async def classify(
        self, *, user_id: str, text: str, queue_snapshot: list[str]
    ) -> dict[str, Any]:
        ...


class VaultRepository(Protocol):
    async def append_record(self, *, text: str, category: str, event_date: str) -> str:
        ...

    async def append_reminder(self, *, text: str, hhmm: str, event_date: str) -> str:
        ...

    async def upsert_timeline_slot(self, *, slot: str, text: str) -> str:
        ...

    async def read_view(self, *, kind: str) -> str:
        ...


class TodoRepository(Protocol):
    async def merge(self, *, user_id: str, tasks: list[str]) -> str:
        ...

    async def done_current(self, *, user_id: str) -> tuple[str, str]:
        ...

    async def next_task(self, *, user_id: str) -> str:
        ...

    async def not_done(self, *, user_id: str) -> str:
        ...

    async def reorder(self, *, user_id: str, order: list[str]) -> str:
        ...

    async def reorder_confirm(self, *, user_id: str) -> str:
        ...

    async def skip_current(self, *, user_id: str) -> tuple[str, str]:
        ...

    async def abandon_current(self, *, user_id: str) -> tuple[str, str]:
        ...


@dataclass(frozen=True)
class ACPSessionPool:
    unified: str
    todo: str
    record: str
    remind: str
    agent: str
    debug: str


class ACLProvider(Protocol):
    async def is_agent_user(self, *, user_id: str) -> bool:
        ...
