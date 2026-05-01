"""微信图片消息：下载、视觉描述、写入日志"""

import json as _json

from config import _log_reasoning
from utils.wechat_media import download_image


class ImageMixin:
    async def _handle_image(self, msg: dict) -> None:
        text = msg.get("text", "").strip()
        from_user = msg.get("from", "")
        context_token = msg.get("context_token", "")
        image_items = msg.get("image_items", [])
        if not image_items:
            return
        await self.wx.set_typing(
            to_user=from_user, status=1, context_token=context_token
        )
        try:
            print(f"[Bot] 🖼 收到图片, raw item: {_json.dumps(image_items[0], ensure_ascii=False)[:500]}")
            img_data = await download_image(image_items[0], self.wx.session)
            if not img_data:
                await self.wx.send_text("图片下载失败", from_user, context_token)
                return

            print(f"[Bot] 🖼 图片已下载: {len(img_data)} bytes")

            mm_model = self.cfg["opencode"].get(
                "multimodal_model", "opencode-go/mimo-v2-omni"
            )
            vision_sid = await self.acp.create_session(mm_model)

            desc, _ = await self.acp.prompt_with_image(
                vision_sid,
                "用简洁的中文描述这张图片的内容，只描述可见内容，不要推理。",
                img_data,
            )
            image_desc = desc or "无法识别图片内容"
            print(f"[Bot] 👁 图片描述: {image_desc[:100]}")

            system_prefix = (
                "你是生活日志助手。用户发来一张图片，图片描述如下：\n"
                f"「{image_desc}」\n"
            )
            if text:
                system_prefix += f"用户附言：「{text}」\n"
            system_prefix += (
                "你需要：\n"
                "1. 分类：身体/运动/阅读/事务\n"
                "2. 写入文件（用 fs/write_text_file）\n"
                "3. 只回复一行简短确认，不要输出分析过程\n"
            )
            reply, reasoning = await self.acp.prompt(
                self.session_id, system_prefix
            )
            if reasoning:
                _log_reasoning(f"[图片] {image_desc[:50]}", reasoning)

            reply = (reply or "已记录").strip()[
                : self.cfg["bot"].get("max_reply_length", 2000)
            ]
            await self.wx.send_text(reply, from_user, context_token)
            print(f"[Bot] >>> {(reply)[:80]}")

        except Exception as e:
            print(f"[Bot] 图片处理错误: {e}")
            await self.wx.send_text(
                f"图片处理出错: {str(e)[:100]}", from_user, context_token
            )
        finally:
            await self.wx.set_typing(
                to_user=from_user, status=2, context_token=context_token
            )
