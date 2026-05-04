"""
微信 ClawBot → OpenCode ACP → Vault
模型: opencode-go/deepseek-v4-flash（文本）/ opencode-go/qwen3.6-plus（多模态）
架构: WeChat → iLink API → ClawBotClient → OpenCode ACP → vault + 回复

iLink 协议参考：
- GolemBot: https://github.com/0xranx/golembot (goolembot weixin-login / golembot gateway)
- wechat-opencode-bot: https://github.com/zsxink/wechat-opencode-bot
- routa bridge: https://github.com/phodal/routa (OpenCode ACP → HTTP+SSE)
- sechub: https://sechub.in/view/3194079

微信接入流程：
1. golembot weixin-login → 扫码获取 bearer token
2. 后续用 token 进行 HTTP 长轮询收消息 / 发消息
3. 不需要公网 IP，纯 fetch 实现
"""

import asyncio
from contextlib import suppress

from acp.opencode_client import OpenCodeACP
from config import load_config
from handlers import Handler
from scheduler.archive import auto_archive_loop
from scheduler.checkin import checkin_loop
from scheduler.log_rotation import log_rotation_loop
from scheduler.reminders import remind_check_loop
from wechat import ClawBotClient


async def main():
    config = load_config()
    oc_cfg = config.get("opencode", {})

    acp = OpenCodeACP(
        cwd=oc_cfg.get("cwd", "."),
        port=oc_cfg.get("port", 0),
        hostname=oc_cfg.get("hostname", "127.0.0.1"),
        model=oc_cfg.get("model", "deepseek/deepseek-v4-flash"),
        max_tokens=int(oc_cfg.get("max_tokens", 4096) or 4096),
        mcp_servers=(oc_cfg.get("mcp_servers") or []),
        reply_merge_enabled=bool(oc_cfg.get("reply_merge_enabled", True)),
    )
    wx = ClawBotClient(config.get("bot", {}))
    archive_task = None
    remind_task = None
    checkin_task = None
    log_rotate_task = None

    try:
        await acp.start()

        await wx.start()
        if not wx._load_auth():
            if not await wx.login():
                print("[Bot] 微信登录失败，退出")
                return
        else:
            print(f"[Bot] Loaded auth (bot_id={wx.bot_id[:8]}...)")

        h = Handler(acp, config, wx)
        await h.init_session()

        archive_task = asyncio.create_task(auto_archive_loop(acp, config, h))
        remind_task = asyncio.create_task(remind_check_loop(h))
        tl = (config.get("timeline") or {})
        if bool(tl.get("enabled")) and bool(tl.get("checkin_enabled")):
            checkin_task = asyncio.create_task(checkin_loop(h))
        log_rotate_task = asyncio.create_task(log_rotation_loop())

        print(f"[Bot] Ready ✓ 微信生活日志助手已启动")
        print(f"[Bot] 已提醒缓存数: {len(h._reminded_ids)}")

        while True:
            try:
                async for msg in wx.poll_messages():
                    asyncio.create_task(h.handle(msg))
            except KeyboardInterrupt:
                print("\n[Bot] Exiting...")
                break
            except Exception as e:
                print(f"[Bot] 消息循环异常: {e}")

            if not wx.token:
                print("[Bot] Token 失效，重新登录...")
                if not await wx.login():
                    print("[Bot] 重新登录失败，退出")
                    break

                for task in (archive_task, remind_task, checkin_task, log_rotate_task):
                    if task:
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task

                h = Handler(acp, config, wx)
                await h.init_session()
                archive_task = asyncio.create_task(auto_archive_loop(acp, config, h))
                remind_task = asyncio.create_task(remind_check_loop(h))
                tl2 = (config.get("timeline") or {})
                if bool(tl2.get("enabled")) and bool(tl2.get("checkin_enabled")):
                    checkin_task = asyncio.create_task(checkin_loop(h))
                else:
                    checkin_task = None
                log_rotate_task = asyncio.create_task(log_rotation_loop())
                print("[Bot] Ready ✓ 重新连接成功")
            else:
                print("[Bot] 5秒后重试...")
                await asyncio.sleep(5)
    finally:
        for task in (archive_task, remind_task, checkin_task, log_rotate_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        with suppress(Exception):
            await wx.stop()
        with suppress(Exception):
            await acp.stop()


if __name__ == "__main__":
    asyncio.run(main())
