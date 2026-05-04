from __future__ import annotations

from pathlib import Path

from handlers.coaches.todo import TodoCoachMixin

from tests.helpers import minimal_timeline, minimal_vault


class _Dummy(TodoCoachMixin):
    def __init__(self, root: Path):
        r = str(root)
        self.cfg = {"vault": minimal_vault(r), "timeline": minimal_timeline(r)}


def _prepare_registry(tmp_path: Path) -> None:
    p = tmp_path / "3-Resources" / "模板库"
    p.mkdir(parents=True, exist_ok=True)
    (p / "待办模板总表.md").write_text(
        "# 待办模板总表\n\n"
        "## 上山流程模板\n"
        "- [ ] 早上种树\n"
        "- [ ] 穿鞋\n\n"
        "## 下山流程模板\n"
        "- [ ] 打开iPad\n"
        "- [ ] 刷牙\n",
        encoding="utf-8",
    )


def test_canonical_match_downhill_template(tmp_path: Path):
    _prepare_registry(tmp_path)
    d = _Dummy(tmp_path)
    expanded, hits = d._expand_template_tasks(["下山模板"])
    assert hits == ["下山流程模板"]
    assert expanded == ["打开iPad", "刷牙"]


def test_canonical_match_downhill_todo_template(tmp_path: Path):
    _prepare_registry(tmp_path)
    d = _Dummy(tmp_path)
    expanded, hits = d._expand_template_tasks(["下山待办模板"])
    assert hits == ["下山流程模板"]
    assert expanded == ["打开iPad", "刷牙"]


def test_non_template_task_should_not_be_forced_by_user_text(tmp_path: Path):
    _prepare_registry(tmp_path)
    d = _Dummy(tmp_path)
    expanded, hits = d._expand_template_tasks(["买牛奶"])
    assert hits == []
    assert expanded == ["买牛奶"]

