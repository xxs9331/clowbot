"""在 EventSource (SSE) 中安全传输含换行的 Markdown 正文。

SSE 规定 ``data:`` 帧以 ``\\n\\n`` 结束；若 LLM 流式正文里也出现连续换行，
会被客户端误解析为「多条消息」，从而丢掉段落/代码块结构。对策：在写入 ``data:``
行之前把正文里的换行编成占位符，前端渲染前再还原。

参考：赵英杰《解决 LLM 流式响应中的 Markdown 换行符问题》
https://yingjiezhao.com/zh/articles/Solving-Markdown-Newline-Issues-in-LLM-Stream-Responses/

Vault 剪藏：``0-Inbox/解决LLM流式响应中的Markdown换行符问题  赵英杰的博客.md``

说明：ClawBot 主链路为 OpenCode ACP（JSON-RPC over stdio），换行在 JSON 字符串内
已按规范转义，**不**存在上述分帧冲突。本模块供 HTTP+SSE 桥（如 routa）或自建
流式 API 复用；**不要**用于微信 ``send_text`` 等与 SSE 无关的通道。
"""

from __future__ import annotations

# 与原文一致，避免与常见 Markdown 语法冲突；若模型正文含同字面串需另议策略。
NEWLINE_PLACEHOLDER = "<|newline|>"


def escape_data_for_sse_event_body(text: str) -> str:
    """将正文中的换行替换为占位符，便于作为单条 SSE ``data:`` 载荷发送。"""
    if not isinstance(text, str):
        return ""
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    return s.replace("\n", NEWLINE_PLACEHOLDER)


def unescape_data_from_sse_event_body(text: str) -> str:
    """前端收到 ``event.data`` 后，在 Markdown 渲染前还原换行。"""
    if not isinstance(text, str):
        return ""
    return text.replace(NEWLINE_PLACEHOLDER, "\n")
