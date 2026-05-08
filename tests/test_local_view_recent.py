from __future__ import annotations

from handlers.local_view import LocalViewMixin


def test_parse_recent_bullet_count_numeric_and_default():
    assert LocalViewMixin._parse_recent_bullet_count("最近5条记忆") == 5
    assert LocalViewMixin._parse_recent_bullet_count("找一下最近的三条记忆") == 3
    assert LocalViewMixin._parse_recent_bullet_count("翻翻记忆") == 3
