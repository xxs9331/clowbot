"""微信图片消息：下载 → Base64 注入 LangGraph（describe_img → 决策 → DomainServices）。

写盘仍由工具链（record.add 等）与 Handler 适配方法完成。
"""

from __future__ import annotations

import base64
import json as _json
import re

from utils.flow_log import log_flow_event
from utils.wechat_media import download_image


class ImageMixin:
    @staticmethod
    def _pick_vision_desc(desc: str, reasoning: str) -> str:
        """优先用 reply；为空时回退到 reasoning，避免多模态文本丢失。"""
        d = (desc or "").strip()
        if d:
            return d
        r = (reasoning or "").strip()
        if not r:
            return "无法识别图片内容"
        # 清掉常见提示复读前缀，尽量保留可见内容描述本体。
        r = re.sub(r"^\s*我们被要求[^。\n]*[。\n]\s*", "", r, count=1).strip()
        return r or "无法识别图片内容"

    async def _handle_image(self, msg: dict) -> None:
        text = msg.get("text", "").strip()
        from_user = msg.get("from", "")
        context_token = msg.get("context_token", "")
        image_items = msg.get("image_items", [])
        if not image_items:
            return
        log_flow_event(
            stage="route",
            route="image_message",
            user_text=text or "(无附言)",
            from_user=from_user,
            session_id=self.session_id,
        )
        await self.wx.set_typing(to_user=from_user, status=1, context_token=context_token)
        try:
            print(
                f"[Bot] 🖼 收到图片, raw item: "
                f"{_json.dumps(image_items[0], ensure_ascii=False)[:500]}"
            )
            img_data = await download_image(image_items[0], self.wx.session)
            if not img_data:
                await self.wx.send_text("图片下载失败", from_user, context_token)
                return

            print(f"[Bot] 🖼 图片已下载: {len(img_data)} bytes（交由 LangGraph describe_img）")

            if from_user not in self._conversation_window:
                self._restore_conversation_window(from_user)
            user_line = "[图片]"
            if text:
                user_line = f"{user_line}\n附言：{text}"
            self._append_conversation_user(from_user, user_line)

            b64 = base64.b64encode(img_data).decode("ascii")
            mime = "image/jpeg"
            if len(img_data) >= 8 and img_data[:8] == b"\x89PNG\r\n\x1a\n":
                mime = "image/png"
            elif img_data[:6] in (b"GIF87a", b"GIF89a"):
                mime = "image/gif"
            elif img_data[:4] == b"RIFF" and img_data[8:12] == b"WEBP":
                mime = "image/webp"

            log_flow_event(
                stage="route",
                route="image_to_graph",
                user_text=text or "(无附言)",
                from_user=from_user,
                session_id=self.session_id,
            )
            await self._run_chat_graph_turn(
                text=text,
                from_user=from_user,
                context_token=context_token,
                image_base64=b64,
                image_mime=mime,
            )

        except Exception as e:  # noqa: BLE001
            print(f"[Bot] 图片处理错误: {e}")
            await self.wx.send_text(
                f"图片处理出错: {str(e)[:100]}", from_user, context_token
            )
        finally:
            await self.wx.set_typing(
                to_user=from_user, status=2, context_token=context_token
            )
