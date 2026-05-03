"""命令行运行评测 YAML（默认 stub ACP；--real 接 OpenCode 真进程）。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.evals.support import run_yaml_file_sync  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Run ClawBot routing eval cases from YAML.")
    p.add_argument(
        "--cases",
        type=Path,
        default=None,
        help="Path to cases YAML（--real 时默认 tests/evals/cases.e2e.yaml）",
    )
    p.add_argument(
        "--tmp",
        type=Path,
        default=ROOT / ".clawbot_tmp" / "eval_runs",
        help="沙箱 vault 根目录（日记写入此 tmp，不写生产 vault）",
    )
    p.add_argument(
        "--real",
        action="store_true",
        help="使用 config.yaml 启动真实 OpenCodeACP，并 await init_session()",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config.yaml",
        help="--real 时读取的配置路径",
    )
    args = p.parse_args()
    cases_path = args.cases
    if cases_path is None:
        cases_path = (
            ROOT / "tests" / "evals" / "cases.e2e.yaml"
            if args.real
            else ROOT / "tests" / "evals" / "cases.example.yaml"
        )

    if args.real and os.environ.get("CLAWBOT_EVAL_REAL") != "1":
        print(
            "错误: 使用 --real 前请在 shell 中设置 CLAWBOT_EVAL_REAL=1，"
            "避免误连真模型产生费用。",
            file=sys.stderr,
        )
        sys.exit(2)

    args.tmp.mkdir(parents=True, exist_ok=True)
    report = run_yaml_file_sync(
        cases_path,
        tmp_path=args.tmp,
        use_real_acp=args.real,
        config_path=args.config if args.real else None,
    )
    print(f"cases_file={report['cases_file']}")
    print(f"use_real_acp={report.get('use_real_acp', False)}")
    print(f"total={report['total']} failed={report['failed']}")
    for line in report["failures"]:
        print(line)
    for row in report["per_case"]:
        st = "OK" if row["ok"] else "FAIL"
        print(f"  [{st}] {row['id']}")
        for f in row.get("failures") or []:
            print(f"       {f}")
    sys.exit(1 if report["failed"] else 0)


if __name__ == "__main__":
    main()
