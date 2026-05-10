"""出站微信文案规范化（换行 / 字面量 \\n）。"""

from __future__ import annotations

from wechat.client import normalize_wechat_outbound_text


def test_literal_backslash_n_becomes_separator():
    assert normalize_wechat_outbound_text("第一\\n第二") == "第一 · 第二"


def test_real_newline_joined_with_middle_dot():
    assert normalize_wechat_outbound_text("第一\n第二") == "第一 · 第二"


def test_crlf_normalized():
    assert normalize_wechat_outbound_text("第一\r\n第二") == "第一 · 第二"


def test_single_line_unchanged():
    assert normalize_wechat_outbound_text("只有一行") == "只有一行"


def test_empty_blank_lines_collapsed():
    assert normalize_wechat_outbound_text("a\n\nb") == "a · b"


def test_empty_string():
    assert normalize_wechat_outbound_text("") == ""


def test_literal_tab_to_space_in_line():
    assert normalize_wechat_outbound_text("a\\tb") == "a b"
