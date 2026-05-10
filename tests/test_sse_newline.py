"""SSE 换行占位符（与 EventSource 分帧冲突的规避）。"""

from __future__ import annotations

from utils.sse_newline import (
    NEWLINE_PLACEHOLDER,
    escape_data_for_sse_event_body,
    unescape_data_from_sse_event_body,
)


def test_roundtrip_preserves_paragraphs():
    raw = "第一段\n\n第二段\n- 列表"
    esc = escape_data_for_sse_event_body(raw)
    assert "\n" not in esc
    assert NEWLINE_PLACEHOLDER in esc
    assert unescape_data_from_sse_event_body(esc) == raw


def test_crlf_normalized_before_escape():
    assert escape_data_for_sse_event_body("a\r\nb") == f"a{NEWLINE_PLACEHOLDER}b"


def test_empty_and_non_str():
    assert escape_data_for_sse_event_body("") == ""
    assert unescape_data_from_sse_event_body("") == ""
    assert escape_data_for_sse_event_body(None) == ""  # type: ignore[arg-type]
    assert unescape_data_from_sse_event_body(None) == ""  # type: ignore[arg-type]
