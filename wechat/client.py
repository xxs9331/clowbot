"""微信 iLink Bot API 客户端"""

import asyncio
import base64
import json
import random
import uuid

import aiohttp

from config import PACKAGE_ROOT

#
# 微信 ClawBot 基于 iLink Bot API (ilinkai.weixin.qq.com)
# 接入方式：
#   方式A: 直接用 iLink REST API（当前实现）
#   方式B: 用 golembot weixin-login 获取 token，再走 REST
#   方式C: 用 wechat-opencode-bot（Node.js 桥接）
#
# 当前实现为方式A的骨架，需要实际扫码测试后微调API端点


class ClawBotClient:
    """微信 iLink Bot API 客户端

    基于腾讯官方 iLink 协议，参考以下开源实现：
    - CherryHQ/cherry-studio (最完整的 Python/TS 双版本)
    - QwenLM/qwen-code (Qwen 官方)
    - lobehub/lobehub (活跃维护)
    - ValueCell-ai/ClawX (MIT 许可)
    - iOfficeAI/AionUi (Apache 许可)

    API 端点（全部基于 https://ilinkai.weixin.qq.com）：
    - GET  /ilink/bot/get_bot_qrcode?bot_type=3  → 获取扫码二维码
    - GET  /ilink/bot/get_qrcode_status?qrcode=<ticket>  → 轮询扫码状态
    - POST /ilink/bot/getupdates  → 长轮询收消息（body: {get_updates_buf, base_info}）
    - POST /ilink/bot/sendmessage  → 发消息（body: {msg, base_info}）
    - POST /ilink/bot/sendtyping   → 发送"正在输入"状态
    """

    BASE_URL = "https://ilinkai.weixin.qq.com"
    BOT_TYPE = "3"  # 个人 bot 类型
    CHANNEL_VERSION = "clawbot/1.0"

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.session: aiohttp.ClientSession = None
        self.bot_id = ""  # ilink_bot_id
        self.token = ""  # bot_token
        self.user_id = ""  # ilink_user_id
        self.base_url = self.BASE_URL  # 可能会被登录响应更新
        self.get_updates_buf = ""  # 长轮询游标
        self._context_tokens = {}  # 用户→context_token 映射（发消息需要）
        self._route_tag = ""  # 路由标签（getconfig 返回）

    def _base_info(self) -> dict:
        return {"channel_version": self.CHANNEL_VERSION}

    def _headers(self, route_tag: str = "") -> dict:
        """构建请求头 — 参考 OpenCode Bridge weixin-api.ts

        必须包含:
        - Authorization: Bearer <bot_token>
        - AuthorizationType: ilink_bot_token  ← 区分 token 类型
        - X-WECHAT-UIN: <random_base64_uint32> ← 每请求随机
        - SKRouteTag: 路由标签（getconfig 返回后使用）
        """
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self.token}",
            "X-WECHAT-UIN": self._random_uin(),
        }
        if route_tag or getattr(self, "_route_tag", ""):
            headers["SKRouteTag"] = route_tag or self._route_tag
        return headers

    @staticmethod
    def _random_uin() -> str:
        """生成随机 X-WECHAT-UIN（base64 编码的随机 uint32）"""
        return base64.b64encode(
            random.randint(0, 0xFFFFFFFF).to_bytes(4, "big")
        ).decode("ascii")

    async def start(self):
        # 不设置 Content-Type 请求头（iLink 用 octet-stream 响应）
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
        )

    async def _get_json(self, url: str, **kwargs) -> dict:
        """GET 请求并强制解析 JSON（iLink 返回 application/octet-stream）"""
        # 合并认证头和调用者自定义头
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        async with self.session.get(url, headers=headers, **kwargs) as resp:
            text = await resp.text()
            if resp.status in (401, 403):
                print(f"[ClawBot] GET {resp.status}: url={url}")
                return {"_status": resp.status}
            try:
                data = json.loads(text)
                data["_status"] = resp.status
                # iLink 业务错误码：-14=session timeout, -1=未授权
                errcode = data.get("errcode", 0)
                if errcode in (-14, -1):
                    print(
                        f"[ClawBot] GET iLink错误: errcode={errcode}, msg={data.get('errmsg','')}"
                    )
                    data["_auth_expired"] = True
                return data
            except json.JSONDecodeError:
                print(
                    f"[ClawBot] GET 非JSON响应: status={resp.status}, url={url}, body={text[:200]}"
                )
                return {}

    async def _post_json(self, url: str, body: dict, **kwargs) -> dict:
        """POST 请求并强制解析 JSON"""
        # 合并认证头和调用者自定义头
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        headers["Content-Type"] = "application/json"
        async with self.session.post(url, json=body, headers=headers, **kwargs) as resp:
            text = await resp.text()
            if resp.status in (401, 403):
                print(f"[ClawBot] POST {resp.status}: url={url}")
                return {"_status": resp.status}
            try:
                data = json.loads(text)
                data["_status"] = resp.status
                # iLink 业务错误码：-14=session timeout, -1=未授权
                errcode = data.get("errcode", 0)
                if errcode in (-14, -1):
                    print(
                        f"[ClawBot] POST iLink错误: errcode={errcode}, msg={data.get('errmsg','')}"
                    )
                    data["_auth_expired"] = True
                return data
            except json.JSONDecodeError:
                print(
                    f"[ClawBot] POST 非JSON响应: status={resp.status}, url={url}, body={text[:200]}"
                )
                return {}

    async def stop(self):
        if self.session:
            await self.session.close()

    async def login(self) -> bool:
        """扫码登录流程
        GET /ilink/bot/get_bot_qrcode?bot_type=3 → 获取二维码
        GET /ilink/bot/get_qrcode_status?qrcode=<ticket> → 轮询状态
        """
        print("[ClawBot] 获取二维码...")
        data = await self._get_json(
            f"{self.BASE_URL}/ilink/bot/get_bot_qrcode",
            params={"bot_type": self.BOT_TYPE},
        )
        if not data:
            return False

        qrcode_ticket = data.get("qrcode", "")
        qrcode_img_url = data.get("qrcode_img_content", "")

        if not qrcode_ticket:
            print(
                f"[ClawBot] 二维码获取失败: {json.dumps(data, ensure_ascii=False)[:300]}"
            )
            return False

        # 显示二维码
        print(f"\n{'='*50}")
        if qrcode_img_url:
            print(f"请用微信扫描以下二维码链接:")
            print(f"{qrcode_img_url}")
        print(f"{'='*50}\n")

        # 轮询扫码状态
        for i in range(480):  # 最多等8分钟
            await asyncio.sleep(1)
            try:
                status_data = await self._get_json(
                    f"{self.BASE_URL}/ilink/bot/get_qrcode_status",
                    params={"qrcode": qrcode_ticket},
                    headers={"iLink-App-ClientVersion": "1"},
                )
            except asyncio.TimeoutError:
                continue  # 长轮询超时是正常的
            except Exception as e:
                print(f"[ClawBot] 状态检查错误: {e}")
                await asyncio.sleep(3)
                continue

            status = status_data.get("status", "")
            if i % 3 == 0:
                # 调试：打印完整响应（前3次和关键状态变化时）
                print(
                    f"[ClawBot] login poll #{i}: status={status}, keys={list(status_data.keys())}"
                )
            if status == "confirmed":
                # 保存完整响应便于调试
                print(
                    f"[ClawBot] 登录响应完整字段: {json.dumps(status_data, ensure_ascii=False)[:500]}"
                )
                self.token = status_data.get("bot_token", "")
                self.bot_id = status_data.get("ilink_bot_id", "")
                self.user_id = status_data.get("ilink_user_id", "")
                # 服务器可能返回不同的 base_url
                if status_data.get("baseurl"):
                    self.base_url = status_data["baseurl"]
                self._save_auth()
                print(
                    f"[ClawBot] 登录成功! bot_id={self.bot_id[:8] if self.bot_id else '?'}..."
                )
                return True
            elif status == "scaned":
                if i % 5 == 0:
                    print("[ClawBot] 已扫描，请在手机上确认...")
            elif status == "expired":
                print("[ClawBot] 二维码已过期，请重新运行程序")
                return False
            # status == "wait" → 继续等待
            if i % 15 == 0 and status == "wait":
                print(f"[ClawBot] 等待扫码... ({i}s)")

        print("[ClawBot] 登录超时")
        return False

    def _save_auth(self):
        auth_data = {
            "bot_id": self.bot_id,
            "token": self.token,
            "user_id": self.user_id,
            "base_url": self.base_url,
        }
        (PACKAGE_ROOT / ".auth.json").write_text(
            json.dumps(auth_data, ensure_ascii=False), encoding="utf-8"
        )

    def _load_auth(self) -> bool:
        f = PACKAGE_ROOT / ".auth.json"
        if f.exists():
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                self.bot_id = d.get("bot_id", "")
                self.token = d.get("token", "")
                self.user_id = d.get("user_id", "")
                self.base_url = d.get("base_url", self.BASE_URL)
                return bool(self.token)
            except Exception:
                return False
        return False

    async def poll_messages(self):
        """长轮询消息 — POST /ilink/bot/getupdates
        返回格式 (参考 lobehub/lobehub):
        {
          "msg_list": [{
            "msgid": "...",
            "from_user_id": "...",
            "message_type": 1,  // 1=文本
            "item_list": [{"type": 1, "text_item": {"text": "..."}}]
          }],
          "get_updates_buf": "..."  // 下次轮询用的游标
        }
        """
        retry_delay = 1
        max_retry_delay = 30
        poll_count = 0

        while True:
            try:
                body = {
                    "get_updates_buf": self.get_updates_buf or "",
                    "base_info": self._base_info(),
                }
                data = await self._post_json(
                    f"{self.base_url}/ilink/bot/getupdates",
                    body,
                    timeout=aiohttp.ClientTimeout(total=60),
                )
                poll_count += 1

                # 调试日志：每10次轮询或收到消息时打印
                msg_list = data.get("msg_list", [])
                if poll_count % 10 == 1 or msg_list:
                    buf_preview = (self.get_updates_buf or "")[:20]
                    print(
                        f"[ClawBot] poll #{poll_count}: keys={list(data.keys())}, "
                        f"msg_count={len(msg_list)}, buf={buf_preview}..."
                    )
                    if not data:
                        print(f"[ClawBot] poll #{poll_count}: empty response")

                if not data and self.session:
                    continue

                # 认证失效检测：HTTP 401/403 或 iLink errcode -14/-1
                if data.get("_status") in (401, 403) or data.get("_auth_expired"):
                    errcode = data.get("errcode", "?")
                    errmsg = data.get("errmsg", "")
                    print(
                        f"[ClawBot] 认证失效 (errcode={errcode}, msg={errmsg})，需要重新登录"
                    )
                    self.token = ""
                    (PACKAGE_ROOT / ".auth.json").unlink(missing_ok=True)
                    return  # 退出轮询，由 main() 触发重连

                retry_delay = 1

                # 更新游标
                new_buf = data.get("get_updates_buf", "")
                if new_buf:
                    self.get_updates_buf = new_buf

                # 解析消息（兼容 msg_list 和 msgs 两种键名）
                messages = data.get("msg_list", data.get("msgs", []))
                for msg in messages:
                    # OpenCode Bridge 用 msg_type，iLink API 也可能返回 message_type
                    msg_type = msg.get("msg_type", msg.get("message_type", 0))
                    from_id = msg.get("from_user_id", "")
                    # 兼容多种 ID 字段：message_id、msgid、client_id、seq
                    msg_id = msg.get(
                        "message_id",
                        msg.get("msgid", msg.get("client_id", str(msg.get("seq", "")))),
                    )

                    # 提取文本和图片（参考 weixin-types.ts: item_list with type enum）
                    text_parts = []
                    image_items = []
                    for item in msg.get("item_list", []):
                        item_type = item.get("type", 0)
                        if item_type == 1:  # MessageItemType.TEXT
                            text_item = item.get("text_item", {})
                            text_parts.append(text_item.get("text", ""))
                        elif item_type == 2:  # MessageItemType.IMAGE
                            image_items.append(item)

                    text = " ".join(text_parts).strip()
                    has_images = len(image_items) > 0
                    print(
                        f"[ClawBot] 收到消息: type={msg_type}, from={from_id[:15]}..., "
                        f"text={text[:50] if text else '(empty)'}"
                        + (f", images={len(image_items)}" if has_images else "")
                    )

                    # 保存 context_token 到内存映射（send_text 发消息时需要）
                    context_token = msg.get("context_token", "")
                    if context_token and from_id:
                        self._context_tokens[from_id] = context_token

                    yield {
                        "id": msg_id,
                        "from": from_id,
                        "text": text,
                        "type": "image" if has_images and not text else
                                "text" if msg_type in (1, 2) else "other",
                        "context_token": context_token,
                        "image_items": image_items if has_images else None,
                    }

            except asyncio.TimeoutError:
                # 长轮询超时是正常的，继续
                if poll_count % 10 == 1:
                    print(
                        f"[ClawBot] poll #{poll_count}: timeout (normal for long-poll)"
                    )
            except aiohttp.ClientError as e:
                print(f"[ClawBot] 轮询错误: {e}, {retry_delay}s后重试")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)
            except Exception as e:
                print(f"[ClawBot] 轮询错误: {e}")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)

    async def send_text(self, text: str, to_user: str, context_token: str = ""):
        """发送文本消息 — POST /ilink/bot/sendmessage

        参考 golembot weixin.ts：msg 包含 from_user_id, client_id, message_type=2, message_state=2
        """
        # 微信单条消息限制约2000字符，分段发送
        chunks = [text[i : i + 1800] for i in range(0, len(text), 1800)]
        results = []

        for chunk in chunks:
            msg = {
                "from_user_id": "",
                "to_user_id": to_user,
                "client_id": str(uuid.uuid4()),
                "message_type": 2,  # golembot 用 2
                "message_state": 2,
                "item_list": [{"type": 1, "text_item": {"text": chunk}}],
            }
            if context_token:
                msg["context_token"] = context_token

            body = {"msg": msg, "base_info": self._base_info()}

            try:
                result = await self._post_json(
                    f"{self.base_url}/ilink/bot/sendmessage", body
                )
                results.append(result)
            except Exception as e:
                print(f"[ClawBot] 发送错误: {e}")

        return results[-1] if results else None

    async def set_typing(
        self, to_user: str = "", status: int = 1, context_token: str = ""
    ):
        """设置正在输入状态 — 先获取 typing_ticket，再调用 sendtyping
        参考 OpenCode Bridge：sendtyping 需要 typing_ticket（从 getconfig 获取）
        """
        if not to_user:
            return
        # 先获取 typing_ticket
        typing_ticket = ""
        try:
            config_data = await self._post_json(
                f"{self.base_url}/ilink/bot/getconfig",
                {
                    "ilink_user_id": to_user,
                    "context_token": context_token,
                    "base_info": self._base_info(),
                },
                timeout=aiohttp.ClientTimeout(total=10),
            )
            typing_ticket = config_data.get("typing_ticket", "")
            if config_data.get("route_tag"):
                # route_tag 可以传给后续请求
                self._route_tag = config_data["route_tag"]
        except Exception:
            pass  # 获取 ticket 失败是尽力而为的

        if not typing_ticket:
            return  # 没有 ticket，不能发 typing

        body = {
            "ilink_user_id": to_user,
            "typing_ticket": typing_ticket,
            "status": status,
            "base_info": self._base_info(),
        }
        try:
            await self._post_json(f"{self.base_url}/ilink/bot/sendtyping", body)
        except Exception:
            pass  # 输入状态是尽力而为的
