"""checkin 可选标准作息上下文路径。"""

from __future__ import annotations

from pathlib import Path

from scheduler.checkin import _resolved_checkin_context_path, _rhythm_tail


class _H:
    def __init__(self, cfg: dict):
        self.cfg = cfg


def test_resolved_none_when_unset() -> None:
    cfg = {"vault": {"root": "C:/vault"}, "timeline": {}}
    assert _resolved_checkin_context_path(_H(cfg)) is None


def test_resolved_none_when_path_escapes(tmp_path) -> None:
    root = tmp_path / "v"
    root.mkdir()
    cfg = {
        "vault": {"root": str(root)},
        "timeline": {"checkin_context_path": "../outside.md"},
    }
    assert _resolved_checkin_context_path(_H(cfg)) is None


def test_rhythm_tail_snips_end_of_file(tmp_path) -> None:
    root = tmp_path / "v"
    root.mkdir()
    rel = Path("mem") / "rhythm.md"
    p = root / rel
    p.parent.mkdir(parents=True)
    body = ("LINE\n" * 500) + "END_MARKER"
    p.write_text(body, encoding="utf-8")
    cfg = {
        "vault": {"root": str(root)},
        "timeline": {"checkin_context_path": str(rel).replace("\\", "/")},
    }
    tail = _rhythm_tail(_H(cfg))
    assert "END_MARKER" in tail
