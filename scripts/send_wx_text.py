#!/usr/bin/env python3
"""直接向微信会话发一条文本（iLink sendmessage），用于测排版，不经过 Handler/LLM。

在仓库根目录 ``.clawbot/`` 下执行::

    python scripts/send_wx_text.py --to <对方ilink_user_id> "第一行\n第二行"

    python scripts/send_wx_text.py --to <id> -f sample.txt

    echo "你好" | python scripts/send_wx_text.py --to <id>

    # 正文里是模型复制的字面量 \\n（两字符），想变成真换行再发：
    python scripts/send_wx_text.py --to <id> --unescape -f scripts/_send_once.md

``--to`` 与收消息时 ``msg["from"]`` 相同；可在 ``logs/flow-*.log`` 里搜 ``from_user``，
或临时在 ``poll_messages`` 里收到消息时打印。

也可设置环境变量 ``CLAWBOT_WX_TO_USER``、``CLAWBOT_WX_CONTEXT_TOKEN``（可选，与收包里的
``context_token`` 一致，部分网关发消息更稳）。

``--unescape`` 与 Handler 里 ``utils.llm_reply_unescape`` 一致：把字面量 ``\\n`` / ``\\r\\n`` 等
转成真实换行（测排版用；勿用于含 Windows 路径等反斜杠敏感正文，除非你能接受误替换风险）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# 保证可从任意 cwd 导入 clawbot 包
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="微信 iLink 直发文本（测排版）")
    p.add_argument(
        "message",
        nargs="?",
        default=None,
        help="正文；可含真实换行。省略则须用 -f 或标准输入",
    )
    p.add_argument(
        "-f",
        "--file",
        type=Path,
        default=None,
        help="从 UTF-8 文件读正文",
    )
    p.add_argument(
        "--to",
        default=(os.environ.get("CLAWBOT_WX_TO_USER") or "").strip(),
        help="对方 ilink_user_id；也可用环境变量 CLAWBOT_WX_TO_USER",
    )
    p.add_argument(
        "--context-token",
        default=(os.environ.get("CLAWBOT_WX_CONTEXT_TOKEN") or "").strip(),
        help="可选；环境变量 CLAWBOT_WX_CONTEXT_TOKEN",
    )
    p.add_argument(
        "--unescape",
        "-u",
        action="store_true",
        help="发送前将字面量 \\\\n / \\\\r\\\\n / \\\\t 转为真换行/空格（与 bot 内 llm_reply_unescape 一致）",
    )
    return p.parse_args()


async def _amain() -> int:
    from config import load_config
    from utils.llm_reply_unescape import unescape_llm_visible_newlines
    from wechat import ClawBotClient

    args = _parse_args()
    if not args.to:
        print(
            "错误：缺少 --to。请传对方 ilink_user_id，或设置环境变量 CLAWBOT_WX_TO_USER。",
            file=sys.stderr,
        )
        return 2

    if args.file is not None:
        body = args.file.read_text(encoding="utf-8")
    elif args.message is not None:
        body = args.message
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
    else:
        print("错误：请提供正文（参数、-f 文件或管道 stdin）。", file=sys.stderr)
        return 2

    if args.unescape or (
        str(os.environ.get("CLAWBOT_WX_UNESCAPE") or "").strip().lower()
        in ("1", "true", "yes")
    ):
        body = unescape_llm_visible_newlines(body)

    cfg = load_config()
    wx = ClawBotClient(cfg.get("bot") or {})
    try:
        await wx.start()
        if not wx._load_auth():
            print("错误：未找到有效 .auth.json，请先运行 bot.py 完成扫码登录。", file=sys.stderr)
            return 1
        result = await wx.send_text(
            body,
            args.to,
            args.context_token or "",
        )
        if result:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            print("(无返回或发送失败，见上方 ClawBot 日志)", file=sys.stderr)
            return 1
    finally:
        await wx.stop()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
