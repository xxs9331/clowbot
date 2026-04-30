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
import heapq
import json
import os
import random
import uuid
import base64
import sys
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import yaml

from acp.opencode_client import OpenCodeACP, build_system_prompt
from utils.time_utils import now, time_str
from utils.intent import detect_intent, INTENT_REMIND, INTENT_TODO, INTENT_NONE, INTENT_QUERY_TODO, INTENT_QUERY_REMIND
from utils.log_sync import get_log_path, parse_reminders_from_log, mark_reminder_done

# ─── 日志目录 ───
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def _log_reasoning(msg_text: str, reasoning: str):
    """推理过程写入日志文件"""
    if not reasoning:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"reasoning-{today}.log"
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] 用户: {msg_text[:100]}\n{'─'*40}\n{reasoning}\n{'='*60}\n"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(entry)


# ─── 配置 ───

def load_config() -> dict:
    path = Path(__file__).parent / "config.yaml"
    if not path.exists():
        print("ERROR: config.yaml not found. Copy from config.example.yaml")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


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
        (Path(__file__).parent / ".auth.json").write_text(
            json.dumps(auth_data, ensure_ascii=False), encoding="utf-8"
        )

    def _load_auth(self) -> bool:
        f = Path(__file__).parent / ".auth.json"
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
                    (Path(__file__).parent / ".auth.json").unlink(missing_ok=True)
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


# ─── 消息处理 ───


class Handler:
    def __init__(self, acp: OpenCodeACP, config: dict, wechat: ClawBotClient):
        self.acp = acp
        self.cfg = config
        self.wx = wechat
        self.session_id = ""
        self._reminded_ids = set()  # 已触发的提醒（避免重复推送）
        self._reminder_refresh = asyncio.Event()

    async def init_session(self):
        """初始化 ACP session 并设置模型"""
        self.session_id = await self.acp.create_session()
        print(f"[Bot] session: {self.session_id}")

    def notify_reminder_refresh(self):
        """提醒列表发生变化时，唤醒调度器重建索引"""
        self._reminder_refresh.set()

    async def handle(self, msg: dict):
        text = msg.get("text", "").strip()
        msg_type = msg.get("type", "text")
        from_user = msg.get("from", "")
        context_token = msg.get("context_token", "")

        # 图片消息处理（必须在 empty text 检查之前，纯图片消息 text 为空）
        if msg_type == "image":
            image_items = msg.get("image_items", [])
            if not image_items:
                return
            await self.wx.set_typing(
                to_user=from_user, status=1, context_token=context_token
            )
            try:
                # 阶段1: 下载图片
                from utils.wechat_media import download_image
                import json as _json
                print(f"[Bot] 🖼 收到图片, raw item: {_json.dumps(image_items[0], ensure_ascii=False)[:500]}")
                img_data = await download_image(image_items[0], self.wx.session)
                if not img_data:
                    await self.wx.send_text("图片下载失败", from_user, context_token)
                    return
                
                print(f"[Bot] 🖼 图片已下载: {len(img_data)} bytes")
                
                # 阶段2: 用视觉模型提取描述（独立 session，不影响主会话）
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
                
                # 阶段3: deepseek-flash 根据描述进行分类和写入
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
            return

        # 非图片消息但无文本内容，跳过
        if not text:
            return

        if text.startswith("/"):
            await self._cmd(text, from_user, context_token)
        else:
            # ─── 自然语言意图检测 ───
            intent, data = detect_intent(text)
            if intent == INTENT_REMIND:
                # 让 AI 写入日志的 ⏰ 提醒 节
                await self.wx.set_typing(to_user=from_user, status=1, context_token=context_token)
                try:
                    vault = self.cfg["vault"]
                    system_prefix = build_system_prompt(
                        vault_root=vault["root"],
                        daily_log_dir=vault["daily_log_dir"],
                    )
                    prompt = (
                        f"{system_prefix}\n"
                        f"用户要设置提醒：「{data}」\n"
                        f"请在日志的「## ⏰ 提醒」节追加一行：\n"
                        f"- [ ] {data}\n"
                        f"如果该节不存在则创建。只回复确认信息。"
                    )
                    reply, _ = await self.acp.prompt(self.session_id, prompt)
                    reply = (reply or "").strip()[:2000]
                    await self.wx.send_text(reply or f"⏰ 已记录提醒：{data}", from_user, context_token)
                    print(f"[Bot] ⏰ 提醒: {data}")
                    self.notify_reminder_refresh()
                except Exception as e:
                    print(f"[Bot] 提醒写入错误: {e}")
                    await self.wx.send_text(f"⏰ 已记录提醒：{data}（写入可能失败，请检查）", from_user, context_token)
                finally:
                    await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)
                return
            elif intent == INTENT_TODO:
                # 让 AI 写入日志的 📋 待办 节
                await self.wx.set_typing(to_user=from_user, status=1, context_token=context_token)
                try:
                    vault = self.cfg["vault"]
                    system_prefix = build_system_prompt(
                        vault_root=vault["root"],
                        daily_log_dir=vault["daily_log_dir"],
                    )
                    prompt = (
                        f"{system_prefix}\n"
                        f"用户要添加待办：「{data}」\n"
                        f"请在日志的「## 📋 待办」节追加一行：\n"
                        f"- [ ] {data}\n"
                        f"如果该节不存在则创建。只回复确认信息。"
                    )
                    reply, _ = await self.acp.prompt(self.session_id, prompt)
                    reply = (reply or "").strip()[:2000]
                    await self.wx.send_text(reply or f"✅ 已添加待办：{data}", from_user, context_token)
                    print(f"[Bot] 📋 待办: {data}")
                except Exception as e:
                    print(f"[Bot] 待办写入错误: {e}")
                    await self.wx.send_text(f"✅ 已添加待办：{data}（写入可能失败，请检查）", from_user, context_token)
                finally:
                    await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)
                return
            elif intent == INTENT_QUERY_TODO:
                # 直接读日志文件返回
                vault = self.cfg["vault"]
                log_path = get_log_path(vault["root"], vault["daily_log_dir"])
                content = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
                # 提取 📋 待办 节
                import re
                match = re.search(
                    r"(##\s*(?:\d+(?:\.\d+)?\s+)?📋\s*待办.*?)(?=##|\Z)",
                    content,
                    re.DOTALL,
                )
                if match:
                    await self.wx.send_text(match.group(1).strip(), from_user, context_token)
                else:
                    await self.wx.send_text("📭 暂无待办", from_user, context_token)
                return
            elif intent == INTENT_QUERY_REMIND:
                # 直接读日志文件返回
                vault = self.cfg["vault"]
                log_path = get_log_path(vault["root"], vault["daily_log_dir"])
                content = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
                import re
                match = re.search(
                    r"(##\s*(?:\d+(?:\.\d+)?\s+)?⏰\s*提醒.*?)(?=##|\Z)",
                    content,
                    re.DOTALL,
                )
                if match:
                    await self.wx.send_text(match.group(1).strip(), from_user, context_token)
                else:
                    await self.wx.send_text("📭 暂无提醒", from_user, context_token)
                return
            print(f"[Bot] <<< {text[:50]}")
            await self.wx.set_typing(
                to_user=from_user, status=1, context_token=context_token
            )

            try:
                # 使用 build_system_prompt 构造含路径和日期规则的系统提示词
                vault = self.cfg["vault"]
                system_prefix = build_system_prompt(
                    vault_root=vault["root"],
                    daily_log_dir=vault["daily_log_dir"],
                    project_dir=vault.get("project_dir", ""),
                    task_dir=vault.get("task_dir", ""),
                )
                reply, reasoning = await self.acp.prompt(
                    self.session_id, f"{system_prefix}\n用户发来：「{text}」"
                )
                # 推理过程写入日志文件，终端显示摘要，不发给微信
                if reasoning:
                    print(f"[Bot] 🧠 {reasoning[:200]}")
                    _log_reasoning(text, reasoning)
                reply = (reply or "").strip()[
                    : self.cfg["bot"].get("max_reply_length", 2000)
                ]
                await self.wx.send_text(reply or "OK", from_user, context_token)
                print(f"[Bot] >>> {(reply or 'OK')[:80]}")
            except Exception as e:
                print(f"[Bot] error: {e}")
                if self.cfg["bot"].get("reply_on_error", True):
                    await self.wx.send_text(
                        f"处理出错: {str(e)[:100]}", from_user, context_token
                    )
            finally:
                await self.wx.set_typing(
                    to_user=from_user, status=2, context_token=context_token
                )

    async def _cmd(self, text: str, to: str, context_token: str = ""):
        cmd = text.lower().split()[0]
        if cmd in ["/help", "/帮助"]:
            await self.wx.send_text(
                "🤖 生生生活日志助手\n\n"
                "发送消息自动记录：\n"
                "  体重 68.5kg\n"
                "  跑步 5km\n"
                "  看了1小时书\n\n"
                "命令：\n"
                "/remind 7:30 上班        → 设置提醒\n"
                "/remind list             → 查看提醒\n"
                "/todo add 买菜           → 添加待办\n"
                "/todo done 1             → 完成待办\n"
                "/todo list               → 查看待办\n"
                "/today  - 查看今日记录\n"
                "/stat <分类> - 7日趋势\n"
                "/status - 系统状态\n"
                "/help   - 帮助",
                to,
                context_token,
            )
        elif cmd in ["/remind", "/提醒"]:
            # /remind 命令 → 直接走 AI 写入日志
            await self.wx.set_typing(to_user=to, status=1, context_token=context_token)
            try:
                vault = self.cfg["vault"]
                system_prefix = build_system_prompt(
                    vault_root=vault["root"],
                    daily_log_dir=vault["daily_log_dir"],
                )
                arg = text.strip()
                for prefix in ["/remind", "/提醒"]:
                    if arg.lower().startswith(prefix):
                        arg = arg[len(prefix):].strip()
                        break
                if arg.lower() in ["list", "列表", "ls", ""]:
                    # 查看提醒列表
                    log_path = get_log_path(vault["root"], vault["daily_log_dir"])
                    content = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
                    import re
                    match = re.search(
                        r"(##\s*(?:\d+(?:\.\d+)?\s+)?⏰\s*提醒.*?)(?=##|\Z)",
                        content,
                        re.DOTALL,
                    )
                    if match:
                        await self.wx.send_text(match.group(1).strip(), to, context_token)
                    else:
                        await self.wx.send_text("📭 暂无提醒", to, context_token)
                else:
                    prompt = (
                        f"{system_prefix}\n"
                        f"用户要设置提醒：「{arg}」\n"
                        f"请在日志的「## ⏰ 提醒」节追加。只回复确认信息。"
                    )
                    reply, _ = await self.acp.prompt(self.session_id, prompt)
                    await self.wx.send_text((reply or "").strip() or f"⏰ 已记录提醒：{arg}", to, context_token)
                    self.notify_reminder_refresh()
            except Exception as e:
                await self.wx.send_text(f"错误: {e}", to, context_token)
            finally:
                await self.wx.set_typing(to_user=to, status=2, context_token=context_token)

        elif cmd in ["/todo", "/待办"]:
            await self.wx.set_typing(to_user=to, status=1, context_token=context_token)
            try:
                vault = self.cfg["vault"]
                system_prefix = build_system_prompt(
                    vault_root=vault["root"],
                    daily_log_dir=vault["daily_log_dir"],
                )
                arg = text.strip()
                for prefix in ["/todo", "/待办"]:
                    if arg.lower().startswith(prefix):
                        arg = arg[len(prefix):].strip()
                        break
                if arg.lower() in ["list", "列表", "ls", ""]:
                    log_path = get_log_path(vault["root"], vault["daily_log_dir"])
                    content = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
                    import re
                    match = re.search(
                        r"(##\s*(?:\d+(?:\.\d+)?\s+)?📋\s*待办.*?)(?=##|\Z)",
                        content,
                        re.DOTALL,
                    )
                    if match:
                        await self.wx.send_text(match.group(1).strip(), to, context_token)
                    else:
                        await self.wx.send_text("📭 暂无待办", to, context_token)
                elif arg.lower().startswith(("done ", "完成 ")):
                    # 完成待办 → 让 AI 更新日志
                    prompt = (
                        f"{system_prefix}\n"
                        f"用户完成了待办：「{arg.split(maxsplit=1)[1] if ' ' in arg else arg}」\n"
                        f"请在日志的「## 📋 待办」节中找到对应的条目，将 - [ ] 改为 - [x]，加上 ✅HH:MM。\n"
                        f"只回复确认信息。"
                    )
                    reply, _ = await self.acp.prompt(self.session_id, prompt)
                    await self.wx.send_text((reply or "").strip() or "✅ 已完成", to, context_token)
                else:
                    prompt = (
                        f"{system_prefix}\n"
                        f"用户要添加待办：「{arg}」\n"
                        f"请在日志的「## 📋 待办」节追加 - [ ] {arg}。\n"
                        f"只回复确认信息。"
                    )
                    reply, _ = await self.acp.prompt(self.session_id, prompt)
                    await self.wx.send_text((reply or "").strip() or f"✅ 已添加待办：{arg}", to, context_token)
            except Exception as e:
                await self.wx.send_text(f"错误: {e}", to, context_token)
            finally:
                await self.wx.set_typing(to_user=to, status=2, context_token=context_token)
        elif cmd in ["/status", "/状态"]:
            status = "🟢 运行中" if self.acp.is_running else "🔴 已停止"
            model = self.cfg["opencode"].get("model", "?")
            await self.wx.send_text(
                f"状态: {status}\n模型: {model}\n会话: {self.session_id[:12]}...",
                to,
                context_token,
            )
        elif cmd in ["/today", "/今天"]:
            await self.wx.set_typing(to_user=to, status=1, context_token=context_token)
            try:
                vault = self.cfg["vault"]
                today = datetime.now()
                log_path = f"{vault['root']}/{vault['daily_log_dir']}/{today.year}/{today.month:02d}/{today.strftime('%Y-%m-%d')}.md"
                reply, reasoning = await self.acp.prompt(
                    self.session_id,
                    f"只回复摘要，不要输出分析过程。读取 {log_path}，简洁总结今天的生活记录。",
                )
                if reasoning:
                    print(f"[Bot] 🧠 {reasoning[:200]}")
                    _log_reasoning(f"/today", reasoning)
                await self.wx.send_text(reply or "今天暂无记录", to, context_token)
            except Exception as e:
                await self.wx.send_text(f"错误: {e}", to, context_token)
            finally:
                await self.wx.set_typing(
                    to_user=to, status=2, context_token=context_token
                )
        elif cmd.startswith("/stat"):
            cat = text.split()[1] if len(text.split()) > 1 else ""
            if cat:
                await self.wx.set_typing(
                    to_user=to, status=1, context_token=context_token
                )
                try:
                    vault = self.cfg["vault"]
                    reply, reasoning = await self.acp.prompt(
                        self.session_id,
                        f"只回复数据，不要输出分析过程。读取 {vault['root']}/{vault['daily_log_dir']} 下最近7天的文件，提取'{cat}'分类的记录，总结趋势。",
                    )
                    if reasoning:
                        print(f"[Bot] 🧠 {reasoning[:200]}")
                        _log_reasoning(f"/stat {cat}", reasoning)
                    await self.wx.send_text(
                        reply or f"暂无{cat}数据", to, context_token
                    )
                except Exception as e:
                    await self.wx.send_text(f"错误: {e}", to, context_token)
                finally:
                    await self.wx.set_typing(
                        to_user=to, status=2, context_token=context_token
                    )
        elif cmd in ["/new", "/新会话"]:
            await self.init_session()
            await self.wx.send_text("已创建新会话", to, context_token)


# ─── Main ───


def _build_today_reminder_heap(handler: Handler):
    """从今日日志提取未完成提醒，构建最小堆（按触发时间）"""
    vault = handler.cfg["vault"]
    log_path = get_log_path(vault["root"], vault["daily_log_dir"])
    today = datetime.now().date()
    heap = []
    for r in parse_reminders_from_log(log_path):
        if r["done"]:
            continue
        try:
            hh, mm = r["time"].split(":")
            due_dt = datetime.combine(today, datetime.min.time()).replace(
                hour=int(hh), minute=int(mm)
            )
        except Exception:
            continue
        rid = f"{today.isoformat()}:{r['line']}:{r['time']}"
        heapq.heappush(heap, (due_dt, rid, r))
    return log_path, heap


async def _remind_check_loop(handler: Handler):
    """轻量调度器：只关注今日日志，睡眠到最近到期提醒"""
    last_day = None
    reminder_heap = []
    log_path = None

    while True:
        try:
            now_dt = datetime.now()
            today = now_dt.date()

            if last_day != today or handler._reminder_refresh.is_set() or not reminder_heap:
                log_path, reminder_heap = _build_today_reminder_heap(handler)
                handler._reminder_refresh.clear()
                last_day = today
                print(f"[Bot] 提醒索引已加载: {len(reminder_heap)} 条，日期={today.isoformat()}")

            if not reminder_heap:
                # 无提醒时阻塞等待新提醒写入或每日重启
                await handler._reminder_refresh.wait()
                continue

            next_due_dt = reminder_heap[0][0]
            sleep_seconds = max((next_due_dt - now_dt).total_seconds(), 0.0)

            try:
                # 新提醒写入时提前唤醒并重建索引
                await asyncio.wait_for(handler._reminder_refresh.wait(), timeout=sleep_seconds)
                continue
            except asyncio.TimeoutError:
                pass

            now_dt = datetime.now()
            while reminder_heap and reminder_heap[0][0] <= now_dt:
                _, rid, r = heapq.heappop(reminder_heap)
                if rid in handler._reminded_ids:
                    continue

                msg = f"⏰ 提醒：{r['text']}"
                if handler.wx._context_tokens:
                    last_user, last_token = list(handler.wx._context_tokens.items())[-1]
                    await handler.wx.send_text(msg, last_user, last_token)
                else:
                    await handler.wx.send_text(msg, handler.wx.user_id, "")
                print(f"[Bot] ⏰ 提醒触发: {r['text']}")

                ok = mark_reminder_done(log_path, r["line"])
                if ok:
                    handler._reminded_ids.add(rid)
                    print(f"[Bot] ⏰ 提醒已标记完成: {r['text']}")
                else:
                    print(f"[Bot] ⏰ 提醒标记失败: {r['text']}")

        except Exception as e:
            print(f"[Bot] 提醒检查错误: {e}")
            await asyncio.sleep(3)


async def _auto_archive(acp: OpenCodeACP, config: dict, handler: Handler):
    """每天凌晨 2 点自动归档昨日生活日志到生生项目"""
    while True:
        now = datetime.now()
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        print(f"[Bot] 下次自动归档: {target.strftime('%m-%d %H:%M')} ({wait/3600:.1f}h后)")

        await asyncio.sleep(wait)

        vault = config["vault"]
        yesterday = datetime.now() - timedelta(days=1)
        log_path = f"{vault['root']}/{vault['daily_log_dir']}/{yesterday.year}/{yesterday.month:02d}/{yesterday.strftime('%Y-%m-%d')}.md"

        try:
            reply, reasoning = await acp.prompt(
                handler.session_id,
                f"读取 {log_path}，按「生活日志」skill 的归档流程将记录分发到生生项目各分类文件并更新任务监控。如果文件不存在或为空，回复「无记录」。只回复一行确认。",
            )
            print(f"[Bot] 自动归档: {reply or 'OK'}")
            if reasoning:
                print(f"[Bot] 🧠 {reasoning[:200]}")
        except Exception as e:
            print(f"[Bot] 自动归档失败: {e}")


async def main():
    config = load_config()
    oc_cfg = config.get("opencode", {})

    acp = OpenCodeACP(
        cwd=oc_cfg.get("cwd", "."),
        port=oc_cfg.get("port", 0),
        hostname=oc_cfg.get("hostname", "127.0.0.1"),
        model=oc_cfg.get("model", "deepseek/deepseek-v4-flash"),
    )
    wx = ClawBotClient(config.get("bot", {}))
    archive_task = None
    remind_task = None

    try:
        # 1. 启动 ACP
        await acp.start()

        # 2. 微信连接
        await wx.start()
        if not wx._load_auth():
            if not await wx.login():
                print("[Bot] 微信登录失败，退出")
                return
        else:
            print(f"[Bot] Loaded auth (bot_id={wx.bot_id[:8]}...)")

        # 3. Handler + session
        h = Handler(acp, config, wx)
        await h.init_session()

        # 启动自动归档调度器（每天凌晨 2 点）
        archive_task = asyncio.create_task(_auto_archive(acp, config, h))

        # 启动提醒检查循环（每 30 秒检查一次）
        remind_task = asyncio.create_task(_remind_check_loop(h))

        print(f"[Bot] Ready ✓ 微信生活日志助手已启动")
        print(f"[Bot] 已提醒缓存数: {len(h._reminded_ids)}")

        # 4. 消息循环（支持 token 过期重连）
        while True:
            try:
                async for msg in wx.poll_messages():
                    asyncio.create_task(h.handle(msg))
            except KeyboardInterrupt:
                print("\n[Bot] Exiting...")
                break
            except Exception as e:
                print(f"[Bot] 消息循环异常: {e}")

            # poll_messages 退出 → 可能是 401（token 过期）
            # 检查是否需要重新登录
            if not wx.token:
                print("[Bot] Token 失效，重新登录...")
                if not await wx.login():
                    print("[Bot] 重新登录失败，退出")
                    break

                for task in (archive_task, remind_task):
                    if task:
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task

                h = Handler(acp, config, wx)
                await h.init_session()
                archive_task = asyncio.create_task(_auto_archive(acp, config, h))
                remind_task = asyncio.create_task(_remind_check_loop(h))
                print("[Bot] Ready ✓ 重新连接成功")
            else:
                # 其他异常，短暂等待后重试
                print("[Bot] 5秒后重试...")
                await asyncio.sleep(5)
    finally:
        for task in (archive_task, remind_task):
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
