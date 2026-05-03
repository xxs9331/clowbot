"""评测 runner 冒烟：加载 example YAML 并断言。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.evals.support import run_yaml_file_sync

EXAMPLE = Path(__file__).resolve().parent / "evals" / "cases.example.yaml"
E2E = Path(__file__).resolve().parent / "evals" / "cases.e2e.yaml"


def test_eval_example_yaml_all_pass(tmp_path):
    report = run_yaml_file_sync(EXAMPLE, tmp_path=tmp_path, acp=None)
    assert report["failed"] == 0, report["failures"]


@pytest.mark.skipif(
    os.environ.get("CLAWBOT_EVAL_REAL") != "1",
    reason="真实 OpenCode E2E：设置 CLAWBOT_EVAL_REAL=1 且本机已配置 opencode + config.yaml",
)
def test_eval_e2e_yaml_real_acp(tmp_path):
    """可选：接真模型跑 20 条；耗时与费用自理。"""
    report = run_yaml_file_sync(E2E, tmp_path=tmp_path, use_real_acp=True)
    assert report["failed"] == 0, report["failures"]
