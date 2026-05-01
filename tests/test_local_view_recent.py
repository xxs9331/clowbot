from __future__ import annotations

from handlers.local_view import LocalViewMixin


def test_wants_record_recent_snippet_typical_memory_query():
    assert LocalViewMixin._wants_record_recent_snippet("找一下最近的三条记忆") is True


def test_wants_record_recent_snippet_full_record_view_not_forced():
    # 仅「查看记录」走整节，不命中「最近几条」snippet
    assert LocalViewMixin._wants_record_recent_snippet("查看记录") is False


def test_parse_recent_bullet_count_numeric_and_default():
    assert LocalViewMixin._parse_recent_bullet_count("最近5条记忆") == 5
    assert LocalViewMixin._parse_recent_bullet_count("找一下最近的三条记忆") == 3
    assert LocalViewMixin._parse_recent_bullet_count("翻翻记忆") == 3
