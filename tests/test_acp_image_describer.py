from __future__ import annotations

import asyncio

from langgraph_v2.adapters.acp_image import ACPImageDescriber


class _FakeACP:
    def __init__(self, replies: list[tuple[str, str]]):
        self._replies = list(replies)
        self.create_calls = 0
        self.prompt_calls = 0

    async def create_session(self, model: str) -> str:
        self.create_calls += 1
        return f"sid-{self.create_calls}"

    async def prompt_with_image(
        self,
        session_id: str,
        text: str,
        image_bytes: bytes,
        mime_type: str,
        *,
        trace_tag: str,
        log_model: str,
        timeout_sec: float = 180,
    ) -> tuple[str, str]:
        _ = session_id, text, image_bytes, mime_type, trace_tag, log_model, timeout_sec
        self.prompt_calls += 1
        if self._replies:
            return self._replies.pop(0)
        return "", ""


def _run(coro):
    return asyncio.run(coro)


def test_acp_image_describer_retry_on_prompt_leak():
    acp = _FakeACP(
        replies=[
            ("", "Behavior_Instructions: No Status Updates"),
            ("图片里是一页日志清单", ""),
        ]
    )
    d = ACPImageDescriber(acp, "alibaba-cn/qwen3-vl-plus")

    out = _run(d.describe_image(image_base64="aGVsbG8=", image_mime="image/jpeg"))
    assert "日志清单" in out
    assert acp.create_calls == 2
    assert acp.prompt_calls == 2


def test_acp_image_describer_keep_reasoning_when_not_leak():
    acp = _FakeACP(
        replies=[
            ("", "图片里是一张待办便签，写着今天要做三件事。"),
        ]
    )
    d = ACPImageDescriber(acp, "alibaba-cn/qwen3-vl-plus")

    out = _run(d.describe_image(image_base64="aGVsbG8=", image_mime="image/jpeg"))
    assert "待办便签" in out
    assert acp.create_calls == 1
    assert acp.prompt_calls == 1

