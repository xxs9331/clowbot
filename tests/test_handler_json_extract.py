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


def test_extract_json_object_strips_markdown_fence():
    s = '```json\n{"tool":"none","payload":{},"reply":"ok"}\n```'
    out = Handler._extract_json_object(s)
    assert out.get("tool") == "none"
    assert out.get("reply") == "ok"


def test_extract_json_object_invalid_returns_empty_dict():
    assert Handler._extract_json_object("not json") == {}


def test_extract_json_object_recovers_malformed_unified_reply():
    s = '{"tool": "none", "payload": {}, "reply": "困了就眯一会儿呗\n'
    out = Handler._extract_json_object(s)
    assert isinstance(out, dict)
    assert out.get("tool") == "none"
    assert "困了就眯一会儿呗" in (out.get("reply") or "")


def test_extract_json_object_does_not_swallow_payload_empty_object():
    """payload 里的 {} 曾被 raw_decode 当成整段 JSON 提前返回，导致无法走 reply 正则修复。"""
    s = (
        '{"tool":"none","payload":{},"reply":"好嘞爸爸！那铃铛给你念第一章开头吧～'
        "张云偷溜回家撞见爸妈和校长那啥的画面……要不要听详细的？"
    )
    out = Handler._extract_json_object(s)
    assert out.get("tool") == "none"
    assert out.get("payload") == {}
    assert "要不要听详细的" in (out.get("reply") or "")
