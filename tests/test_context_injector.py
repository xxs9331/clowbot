from __future__ import annotations

import time

from utils.context_injector import InjectedContextStore


def test_add_and_peek_events_ordered():
    st = InjectedContextStore()
    st.add_event(kind="checkin_slot_empty", summary="A", priority="deferred", ttl_sec=30)
    st.add_event(kind="todo_nudge_due", summary="B", priority="deferred", ttl_sec=30)
    out = st.peek_events()
    assert [x.summary for x in out] == ["A", "B"]


def test_mark_consumed_and_prune():
    st = InjectedContextStore()
    eid = st.add_event(kind="todo_nudge_due", summary="X", priority="deferred", ttl_sec=30)
    assert st.mark_consumed([eid]) == 1
    assert st.prune_expired() == 1
    assert st.peek_events() == []


def test_short_ttl_event_expires():
    st = InjectedContextStore()
    st.add_event(kind="disk_write_done", summary="S", priority="silent", ttl_sec=0.1)
    time.sleep(0.15)
    st.prune_expired()
    summary, ids = st.peek_compose_summary(max_items=3)
    assert summary == ""
    assert ids == []

