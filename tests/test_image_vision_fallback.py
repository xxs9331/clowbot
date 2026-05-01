from __future__ import annotations

from handlers.image import ImageMixin


def test_pick_vision_desc_prefers_reply():
    out = ImageMixin._pick_vision_desc("这是图片描述", "这是一段推理")
    assert out == "这是图片描述"


def test_pick_vision_desc_falls_back_to_reasoning():
    out = ImageMixin._pick_vision_desc("", "我们被要求描述图片。\n1. 这里是酒店页面")
    assert "酒店页面" in out


def test_pick_vision_desc_empty_both():
    out = ImageMixin._pick_vision_desc("", "")
    assert out == "无法识别图片内容"
