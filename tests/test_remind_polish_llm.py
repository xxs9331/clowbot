"""remind_polish_llm 单测：不连真实 ACP。"""

from __future__ import annotations

import asyncio

import pytest

from utils.remind_polish_llm import (
    build_reminder_wx_message,
    finalize_reminder_text,
)


def test_finalize_reminder_text_keeps_prefix_when_truncating():
    prefix = "⏰ 提醒（22:00）："
    body = "x" * 100
    out = finalize_reminder_text(prefix, body, max_total=len(prefix) + 10)
    assert out.startswith(prefix)
    assert len(out) == len(prefix) + 10
    assert out == prefix + "x" * 10


def test_finalize_reminder_text_normalizes_body_whitespace():
    prefix = "⏰ 提醒（07:30）："
    body = "  上班\n去  \n"
    out = finalize_reminder_text(prefix, body, max_total=500)
    assert out == prefix + "上班 去"


def _run(coro):
    return asyncio.run(coro)


class _StubACP:
    def __init__(self, reply: str, *, model: str = "stub-model"):
        self._reply = reply
        self.model = model
        self._created = 0
        self.prompt_calls = []

    async def create_session(self, model=None):
        self._created += 1
        return f"sid-{self._created}"

    async def prompt(self, sid, msg, *, trace_tag="x"):
        self.prompt_calls.append({"sid": sid, "msg": msg, "trace_tag": trace_tag})
        return self._reply, ""


def test_build_reminder_disabled_uses_plain_text():
    acp = _StubACP("不应调用润色")
    bot = {"reminder_polish_enabled": False, "max_reply_length": 2000}
    out = _run(
        build_reminder_wx_message(
            acp, bot, remind_time="22:00", remind_text="下班回家"
        )
    )
    assert out == "⏰ 提醒（22:00）：下班回家"
    assert acp._created == 0


def test_build_reminder_none_acp_when_enabled_falls_back():
    bot = {"reminder_polish_enabled": True, "max_reply_length": 2000}
    out = _run(build_reminder_wx_message(None, bot, remind_time="22:00", remind_text="a"))
    assert out == "⏰ 提醒（22:00）：a"


def test_build_reminder_polished_body():
    acp = _StubACP("该收工啦，路上注意安全～")
    bot = {
        "reminder_polish_enabled": True,
        "reminder_polish_timeout_sec": 5,
        "max_reply_length": 2000,
    }
    out = _run(
        build_reminder_wx_message(
            acp, bot, remind_time="22:00", remind_text="下班回家"
        )
    )
    assert out == "⏰ 提醒（22:00）：该收工啦，路上注意安全～"
    primes = [c for c in acp.prompt_calls if c["trace_tag"] == "remind_polish_prime"]
    polishes = [c for c in acp.prompt_calls if c["trace_tag"] == "remind_polish"]
    assert len(primes) == 1
    assert len(polishes) == 1
    assert "下班回家" in polishes[0]["msg"]


class _SlowACP(_StubACP):
    async def prompt(self, sid, msg, *, trace_tag="x"):
        await asyncio.sleep(1.0)
        return await super().prompt(sid, msg, trace_tag=trace_tag)


def test_build_reminder_timeout_falls_back_to_anchor():
    acp = _SlowACP("晚到了")
    bot = {
        "reminder_polish_enabled": True,
        "reminder_polish_timeout_sec": 0.05,
        "max_reply_length": 2000,
    }
    out = _run(
        build_reminder_wx_message(
            acp, bot, remind_time="22:00", remind_text="下班回家"
        )
    )
    assert out == "⏰ 提醒（22:00）：下班回家"


def test_polish_session_reused_on_same_acp():
    acp = _StubACP("润色A")
    bot = {"reminder_polish_enabled": True, "reminder_polish_timeout_sec": 5}

    async def _case():
        await build_reminder_wx_message(acp, bot, remind_time="09:00", remind_text="一")
        acp._reply = "润色B"
        await build_reminder_wx_message(acp, bot, remind_time="10:00", remind_text="二")

    _run(_case())
    assert acp._created == 1
    primes = [c for c in acp.prompt_calls if c["trace_tag"] == "remind_polish_prime"]
    assert len(primes) == 1


@pytest.mark.parametrize(
    "bad_reply, expect_suffix",
    [
        ("", "原文"),
        ("   \n\t  ", "原文"),
    ],
)
def test_build_reminder_empty_model_reply_falls_back(bad_reply, expect_suffix):
    acp = _StubACP(bad_reply)
    bot = {"reminder_polish_enabled": True, "reminder_polish_timeout_sec": 5}
    out = _run(
        build_reminder_wx_message(
            acp, bot, remind_time="12:00", remind_text="原文"
        )
    )
    assert out == f"⏰ 提醒（12:00）：{expect_suffix}"
