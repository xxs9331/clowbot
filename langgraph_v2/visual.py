"""LangGraph topology visualization.

Usage:
    python langgraph_v2/visual.py          # print Mermaid to stdout
    python langgraph_v2/visual.py --png    # export PNG to docs/
    python langgraph_v2/visual.py --mermaid  # explicit Mermaid output

> 输出的 Mermaid 代码贴到 https://mermaid.live 即时渲染。
> --png 需要 `pip install pyppeteer pillow`。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

# ── mock GraphDeps（图编译不执行节点，只需要类型签名） ──


class _Mock:
    async def structured_decide(self, **kw) -> None: ...
    async def describe_image(self, **kw) -> None: ...
    async def classify(self, **kw) -> None: ...
    async def append_record(self, **kw) -> None: ...
    async def append_reminder(self, **kw) -> None: ...
    async def upsert_timeline_slot(self, **kw) -> None: ...
    async def read_view(self, **kw) -> None: ...
    async def merge(self, **kw) -> None: ...
    async def done_current(self, **kw) -> None: ...
    async def next_task(self, **kw) -> None: ...
    async def not_done(self, **kw) -> None: ...
    async def reorder(self, **kw) -> None: ...
    async def reorder_confirm(self, **kw) -> None: ...
    async def skip_current(self, **kw) -> None: ...
    async def abandon_current(self, **kw) -> None: ...
    async def is_agent_user(self, **kw) -> None: ...


if __package__ in (None, ""):
    _ROOT = Path(__file__).resolve().parent.parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))


from langgraph_v2.graph import GraphDeps, build_chat_graph  # noqa: E402
from langgraph_v2.contracts import ACPSessionPool  # noqa: E402

_PKG = Path(__file__).resolve().parent.parent
_DOCS = _PKG / "docs"
_DOCS.mkdir(parents=True, exist_ok=True)


def _make_mock_deps() -> GraphDeps:
    _ = _Mock()
    return GraphDeps(
        llm=_,
        image_llm=_,
        classifier=_,
        vault=_,
        todo=_,
        sessions=ACPSessionPool(
            unified="mock_unified",
            todo="mock_todo",
            record="mock_record",
            remind="mock_remind",
            agent="mock_agent",
            debug="",
        ),
        acl=_,
    )


def print_mermaid() -> str:
    app = build_chat_graph(_make_mock_deps())
    mermaid = app.get_graph().draw_mermaid()
    print(mermaid)
    return mermaid


def export_png(output: Path | None = None) -> Path:
    from langchain_core.runnables.graph import MermaidDrawMethod

    out = Path(output) if output else _DOCS / "clawbot_topology.png"
    app = build_chat_graph(_make_mock_deps())
    app.get_graph().draw_mermaid_png(
        output_file_path=str(out),
        draw_method=MermaidDrawMethod.PYPPETEER,
    )
    print(f"PNG exported → {out}")
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ClawBot LangGraph topology visualizer")
    parser.add_argument("--png", action="store_true", help="Export PNG (needs pyppeteer)")
    parser.add_argument("--mermaid", action="store_true", help="Print Mermaid source")
    args = parser.parse_args()

    if args.png:
        export_png()
    elif args.mermaid:
        print_mermaid()
    else:
        # default: Mermaid
        print_mermaid()
