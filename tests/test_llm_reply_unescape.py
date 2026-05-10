"""llm_reply_unescape：字面量 \\n → 真换行。"""

from __future__ import annotations

from utils.llm_reply_unescape import unescape_llm_visible_newlines


def test_literal_backslash_n_to_newline():
    assert unescape_llm_visible_newlines("你好\\n世界") == "你好\n世界"


def test_double_paragraph():
    assert unescape_llm_visible_newlines("a\\n\\nb") == "a\n\nb"


def test_crlf_literal():
    assert unescape_llm_visible_newlines("x\\r\\ny") == "x\ny"


def test_literal_tab_to_space():
    assert unescape_llm_visible_newlines("a\\tb") == "a b"


def test_real_newline_unchanged():
    assert unescape_llm_visible_newlines("第一行\n第二行") == "第一行\n第二行"


def test_empty():
    assert unescape_llm_visible_newlines("") == ""
    assert unescape_llm_visible_newlines(None) == ""  # type: ignore[arg-type]
