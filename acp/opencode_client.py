"""OpenCode ACP 客户端 — JSON-RPC 2.0 over stdin/stdout

协议流程（参考 routa/onyx/hapi 等项目）：
1. initialize — 需要带 clientInfo + clientCapabilities
2. session/new — 创建会话，获取 sessionId
3. session/prompt — 用 'prompt' 字段发消息
4. 响应 agent→client 请求（fs 操作、权限审批、terminal）
5. 接收 session/update 流式通知

模型：opencode-go/deepseek-v4-flash（默认）、opencode-go/qwen3.6-plus（多模态）
"""

import asyncio
import json
import os
import subprocess
import time
import threading
from typing import Optional, Callable

DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
MULTIMODAL_MODEL = "opencode-go/mimo-v2-omni"


class OpenCodeACP:
    def __init__(self, cwd: str = ".", port: int = 0, hostname: str = "127.0.0.1",
                 model: str = DEFAULT_MODEL):
        self.cwd = cwd
        self.port = port
        self.hostname = hostname
        self.model = model
        self.multimodal_model = MULTIMODAL_MODEL
        self._proc: Optional[subprocess.Popen] = None
        self._msg_id = 0
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False
        # 响应和通知队列
        self._response_queue = asyncio.Queue()
        self._notification_handler: Optional[Callable] = None

    # ─── 生命周期管理 ───

    @staticmethod
    def _find_opencode() -> str:
        """查找 opencode 可执行文件路径（Windows 兼容）"""
        # 1. 直接可用（已在 PATH 中）
        import shutil
        oc = shutil.which("opencode")
        if oc:
            return oc
        # 2. Nodist 安装路径
        for p in [
            os.path.expandvars(r"%NODIST_PREFIX%\bin\opencode.cmd"),
            r"E:\Program Files (x86)\Nodist\bin\opencode.cmd",
        ]:
            if os.path.exists(p):
                return p
        # 3. PowerShell 脚本路径
        for p in [
            r"E:\Program Files (x86)\Nodist\bin\opencode.ps1",
        ]:
            if os.path.exists(p):
                return p
        raise FileNotFoundError("opencode not found in PATH or known locations")

    async def start(self):
        """启动 ACP 进程并完成握手"""
        oc_path = self._find_opencode()
        cmd = [oc_path, "acp", "--cwd", self.cwd]
        if self.port:
            cmd.extend(["--port", str(self.port)])
        if self.hostname:
            cmd.extend(["--hostname", self.hostname])

        print(f"[ACP] Starting: {oc_path} acp --cwd {self.cwd}...")
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, encoding="utf-8", errors="replace", bufsize=1,
        )
        await asyncio.sleep(2)
        if self._proc.poll() is not None:
            raise RuntimeError(f"opencode acp exited: {self._proc.stderr.read()[:500]}")

        # 开始读线程
        self._running = True
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        # 握手：带 clientInfo + clientCapabilities
        resp = await self._send_and_recv("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
            },
            "clientInfo": {
                "name": "clawbot",
                "title": "ClawBot Life Logger",
                "version": "1.0.0",
            },
        })
        if "error" in resp:
            raise RuntimeError(f"ACP init failed: {resp['error']}")
        print(f"[ACP] started (PID={self._proc.pid}), model={self.model}")

    async def stop(self):
        """停止 ACP 进程"""
        self._running = False
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            print("[ACP] stopped")

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ─── 内部通信 ───

    def _reader_loop(self):
        """后台线程：不断读取 stdout，分发到 asyncio.Queue"""
        while self._running and self._proc and self._proc.poll() is None:
            line = self._proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            # 线程安全放入 asyncio.Queue
            try:
                self._response_queue.put_nowait(msg)
            except Exception:
                pass

    def _handle_agent_request(self, msg: dict):
        """处理 agent→client 的请求（fs操作、权限审批等）"""
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        if method == "session/request_permission":
            # 自动批准所有权限请求
            self._write({"jsonrpc": "2.0", "id": msg_id,
                         "result": {"outcome": {"outcome": "approved"}}})
        elif method == "fs/read_text_file":
            fp = params.get("path", "")
            try:
                content = open(fp, encoding="utf-8", errors="replace").read()
                self._write({"jsonrpc": "2.0", "id": msg_id, "result": {"content": content}})
            except Exception as e:
                self._write({"jsonrpc": "2.0", "id": msg_id,
                             "error": {"code": -32000, "message": str(e)}})
        elif method == "fs/write_text_file":
            fp = params.get("path", "")
            content = params.get("content", "")
            try:
                os.makedirs(os.path.dirname(fp), exist_ok=True)
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(content)
                self._write({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            except Exception as e:
                self._write({"jsonrpc": "2.0", "id": msg_id,
                             "error": {"code": -32000, "message": str(e)}})
        elif method == "fs/list_directory":
            dp = params.get("path", ".")
            try:
                entries = [{"name": e.name, "type": "directory" if e.is_dir() else "file"}
                           for e in os.scandir(dp)]
                self._write({"jsonrpc": "2.0", "id": msg_id, "result": {"entries": entries}})
            except Exception as e:
                self._write({"jsonrpc": "2.0", "id": msg_id,
                             "error": {"code": -32000, "message": str(e)}})
        else:
            # 未知请求，返回空结果
            self._write({"jsonrpc": "2.0", "id": msg_id, "result": {}})

    def _write(self, msg: dict):
        """写入 JSON-RPC 消息到 stdin"""
        if self._proc and self._proc.stdin:
            self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()

    def _send(self, method: str, params: dict = None):
        self._msg_id += 1
        msg = {"jsonrpc": "2.0", "id": self._msg_id, "method": method, "params": params or {}}
        self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()
        return self._msg_id

    async def _send_and_recv(self, method: str, params: dict = None, timeout: float = 120) -> dict:
        """发送请求并等待匹配的响应，同时处理 agent→client 请求"""
        msg_id = self._send(method, params)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(self._response_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if self._proc and self._proc.poll() is not None:
                    raise RuntimeError("opencode acp process exited")
                continue

            # Agent→Client 请求 → 自动处理
            if "method" in msg and "id" in msg and "result" not in msg and "error" not in msg:
                self._handle_agent_request(msg)
                continue

            # 匹配请求 ID 的响应
            if msg.get("id") == msg_id:
                return msg

            # 其他响应或通知，跳过
        raise TimeoutError(f"Timeout waiting for response to {method}")

    # ─── 文本提取 ───

    @staticmethod
    def _extract_text(response: dict) -> str:
        """从 ACP 响应中提取文本内容"""
        result = response.get("result", {})

        # parts 结构
        parts = result.get("parts", [])
        texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
        if texts:
            return "\n".join(texts)

        # info 结构（session/prompt 的回复在这里）
        if "info" in result and isinstance(result["info"], dict):
            info_parts = result["info"].get("parts", [])
            texts = [p.get("text", "") for p in info_parts if p.get("type") == "text"]
            if texts:
                return "\n".join(texts)

        # 直接字段
        for key in ["text", "content", "reply", "message"]:
            if key in result:
                return str(result[key])

        if "error" in response:
            return f"[Error] {response['error'].get('message', str(response['error']))}"

        return ""

    # ─── 收集流式响应 ───

    async def _collect_prompt_response(self, msg_id: int, timeout: float = 180) -> dict:
        """收集 session/prompt 的所有响应（包含流式通知和最终结果）
        
        返回:
            text: 最终回复文本（不含推理过程）
            reasoning: 推理过程文本（CoT thinking）
            result: 最终 JSON-RPC 响应
            notifications: 所有通知消息
        """
        all_text = []
        all_reasoning = []
        all_notifications = []
        all_raw = []  # 调试：记录所有原始消息
        final_result = {}
        deadline = time.time() + timeout
        last_activity = time.time()

        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(self._response_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if self._proc and self._proc.poll() is not None:
                    break
                if time.time() - last_activity > 30:
                    break
                continue

            last_activity = time.time()

            # 最终响应（匹配 msg_id）
            if msg.get("id") == msg_id:
                final_result = msg
                break

            # 记录所有消息用于调试
            all_raw.append(msg)

            # session/update 通知
            if msg.get("method") == "session/update":
                update = msg.get("params", {}).get("update", {})
                su = update.get("sessionUpdate", "")
                if su == "text_delta":
                    all_text.append(update.get("textDelta", ""))
                elif su == "agent_message_chunk":
                    # 👈 OpenCode ACP 实际返回的文本消息（不是 text_delta）
                    content = update.get("content", {})
                    if isinstance(content, dict) and content.get("type") == "text":
                        all_text.append(content.get("text", ""))
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                all_text.append(part.get("text", ""))
                elif su == "agent_thought_chunk":
                    # 推理过程（CoT thinking），保留用于日志
                    content = update.get("content", {})
                    if isinstance(content, dict) and content.get("type") == "text":
                        all_reasoning.append(content.get("text", ""))
                elif su == "thinking":
                    all_reasoning.append(update.get("textDelta", update.get("text", "")))
                elif su in ("tool_call_update", "tool_call"):
                    pass  # 工具调用（fs 操作等），不需要收集文本
                elif su == "end_turn":
                    break
                else:
                    if su:
                        _uk = list(update.keys())[:5] if isinstance(update, dict) else []
                        print(f"[ACP DEBUG] unhandled sessionUpdate: {su}, keys={_uk}")
                all_notifications.append(msg)
                continue

            # 其他响应
            if "result" in msg:
                res = msg["result"]
                for p in res.get("parts", []):
                    if p.get("type") == "text":
                        all_text.append(p.get("text", ""))

        return {
            "text": "".join(all_text).strip(),
            "reasoning": "".join(all_reasoning).strip(),
            "result": final_result,
            "notifications": all_notifications,
            "raw_count": len(all_raw),
        }

    # ─── 业务 API ───

    async def create_session(self, model: str = None) -> str:
        """创建 session，设置模型，返回 session_id
        
        ACP 协议不支持在 session/prompt 中指定模型，
        必须通过 session/set_config_option 设置。
        模型格式: "provider/model" 如 "opencode-go/deepseek-v4-flash"
        """
        params = {"cwd": self.cwd, "mcpServers": []}

        resp = await self._send_and_recv("session/new", params)
        result = resp.get("result", {})
        sid = result.get("sessionId", result.get("id", ""))
        
        if not sid:
            raise RuntimeError(f"Failed to create session: {json.dumps(resp, ensure_ascii=False)[:300]}")
        
        # 设置模型（ACP 协议要求通过 set_config_option）
        model_str = model or self.model
        if model_str:
            try:
                await self._set_model(sid, model_str)
            except Exception as e:
                print(f"[ACP] Warning: failed to set model: {e}")
        
        return sid

    async def _set_model(self, session_id: str, model: str):
        """通过 session/set_config_option 设置模型
        
        ACP 协议中模型通过 config option 设置:
        configId: "model"
        value: "provider/model" 格式
        """
        resp = await self._send_and_recv("session/set_config_option", {
            "sessionId": session_id,
            "configId": "model",
            "value": model,
        })
        if "error" in resp:
            print(f"[ACP] set_model error: {resp['error']}")
        else:
            print(f"[ACP] model set to: {model}")

    async def prompt(self, session_id: str, message: str) -> tuple:
        """发送文本消息，返回 (reply_text, reasoning_text)"""
        msg_id = self._send("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": message}],
        })
        collected = await self._collect_prompt_response(msg_id, timeout=180)
        
        # 调试：空响应时 dump 信息
        if not collected["text"] and not collected["reasoning"]:
            result_keys = list(collected.get("result", {}).keys()) if collected.get("result") else []
            print(f"[ACP DEBUG] empty response! raw_count={collected.get('raw_count',0)}, "
                  f"result_keys={result_keys}, notifications={len(collected.get('notifications',[]))}")
            if collected.get("result"):
                print(f"[ACP DEBUG] result sample: {json.dumps(collected['result'], ensure_ascii=False)[:500]}")

        # 优先用流式文本，否则用结果提取
        if collected["text"]:
            reply = collected["text"]
        else:
            reply = self._extract_text(collected["result"])
        return reply, collected["reasoning"]

    async def prompt_with_image(self, session_id: str, text: str,
                                image_bytes: bytes = b"",
                                mime_type: str = "image/jpeg") -> tuple:
        """发送文本+图片（base64 内联），返回 (reply_text, reasoning_text)
        
        ACP 协议不支持 file:// 路径，需要 base64 内联图片数据。
        """
        prompt_parts = [{"type": "text", "text": text}]
        if image_bytes:
            import base64 as _b64
            b64_data = _b64.b64encode(image_bytes).decode("ascii")
            prompt_parts.append({
                "type": "image",
                "mimeType": mime_type,
                "data": b64_data,
            })
        msg_id = self._send("session/prompt", {
            "sessionId": session_id,
            "prompt": prompt_parts,
        })
        collected = await self._collect_prompt_response(msg_id, timeout=180)
        if collected["text"]:
            reply = collected["text"]
        else:
            reply = self._extract_text(collected["result"])
        return reply, collected["reasoning"]


def build_system_prompt(vault_root: str, daily_log_dir: str,
                         project_dir: str = "", task_dir: str = "") -> str:
    return f"""你是"生生项目"的生活日志助手。严格遵循「生活日志」skill 的规则。

## 核心规则
1. 分类：身体 / 运动 / 阅读 / 事务，对应的节标题 emoji：
   - 身体 → ## 🏋️ 身体
   - 运动 → ## 🏃 运动
   - 阅读 → ## 📖 阅读
   - 事务 → ## 📦 事务
2. 写入文件：{daily_log_dir}/YYYY/MM/YYYY-MM-DD.md
3. 日期提取：消息中如有明确日期（如"5月2日"），写入对应日期文件，而非今天
4. 格式：`- [x] 内容描述 ✅HH:MM`（今天/过去）/ `- [ ] 内容描述 ✅HH:MM`（未来日期），追加在对应分类节下
5. 如果分类节不存在则创建，同一分类记录追加在同一节内（不重复创建节标题）
6. 回复：一行简短确认（中文），不输出分析过程

## 路径
- vault 根: {vault_root}
- 生活日志: {daily_log_dir}/YYYY/MM/YYYY-MM-DD.md
- 用 pathlib 处理中文路径，先读文件再追加

## 日期识别
- "N月N日" / "N.N" / "N月N号" → 当前年份的该日期
- "明天" / "后天" → 相对日期
- 无明确日期 → 使用今天"""