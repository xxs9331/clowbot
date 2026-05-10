"""将模型偶发输出的字面量转义序列还原为真实空白符（仅用于助手 reply，勿用于本地读盘正文）。"""

from __future__ import annotations


def unescape_llm_visible_newlines(text: str) -> str:
    """把 JSON/流式里常见的「两字符」``\\n``、``\\r\\n`` 等转成真实换行。

    合法 JSON 经 ``json.loads`` 后一般已是真换行；本函数处理二次转义、raw 拼接等残留。
    仅应在 **LLM 生成的微信回复** 上调用；路径、代码片段若出现在回复里，极少数情况下
    可能被误改（与产品权衡一致）。
    """
    if not isinstance(text, str):
        return ""
    if not text:
        return text
    s = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    s = s.replace("\\t", " ")
    return s
