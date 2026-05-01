from __future__ import annotations

from handlers.base import Handler


def test_extract_json_object_pure_json():
    s = '{"tool":"none","payload":{},"reply":"ok"}'
    out = Handler._extract_json_object(s)
    assert out.get("tool") == "none"
    assert out.get("reply") == "ok"


def test_extract_json_object_with_prefix_suffix_noise():
    s = '好的，结果如下：{"tool":"none","payload":{},"reply":"ok"}谢谢'
    out = Handler._extract_json_object(s)
    assert out.get("tool") == "none"
    assert out.get("payload") == {}


def test_extract_json_object_invalid_returns_empty_dict():
    assert Handler._extract_json_object("not json") == {}


def test_extract_json_object_recovers_malformed_unified_reply():
    s = '{"tool": "none", "payload": {}, "reply": "困了就眯一会儿呗\n'
    out = Handler._extract_json_object(s)
    assert out.get("tool") == "none"
    assert out.get("payload") == {}
    assert "困了" in out.get("reply", "")
