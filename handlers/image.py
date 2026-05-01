"""微信图片消息：下载 + 多模态描述 → 走和文字一样的 record.add 路径。

不再 ad-hoc 拼 prompt 写盘；图片描述出来后构造一个 user_text 喂给统一决策层，
分流 LLM 给出 record.add（含 category 推断），由 RecordCoachMixin 完成写入。
"""

from __future__ import annotations

import json as _json

from utils.flow_log import log_flow_event
from utils.wechat_media import download_image


class ImageMixin:
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

            print(f"[Bot] 🖼 图片已下载: {len(img_data)} bytes")

            mm_model = self.cfg["opencode"].get(
                "multimodal_model", "opencode-go/mimo-v2-omni"
            )
            vision_sid = await self.acp.create_session(mm_model)

            desc, _ = await self.acp.prompt_with_image(
                vision_sid,
                "用简洁的中文描述这张图片的内容，只描述可见内容，不要推理。",
                img_data,
                trace_tag="vision_describe",
                log_model=mm_model,
            )
            image_desc = (desc or "").strip() or "无法识别图片内容"
            print(f"[Bot] 👁 图片描述: {image_desc[:100]}")

            # 把图片描述（+用户附言）当作普通文本，丢给统一分流决策。
            user_text_for_route = f"[图片] {image_desc}"
            if text:
                user_text_for_route = f"{user_text_for_route}\n附言：{text}"

            decision = await self._llm_unified_decide(from_user, user_text_for_route)
            log_flow_event(
                stage="route",
                route="image_to_unified",
                user_text=user_text_for_route,
                from_user=from_user,
                session_id=self.session_id,
                extra={"decision": decision},
            )
            handled = await self._apply_unified_decision(
                decision,
                from_user,
                context_token,
                user_text=user_text_for_route,
            )
            if not handled:
                await self.wx.send_text(
                    "我看到了图片，没看出要记什么。要我记成生活记录吗？",
                    from_user,
                    context_token,
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
