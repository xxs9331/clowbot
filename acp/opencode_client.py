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
import re
import subprocess
from pathlib import Path
import time
import threading
from collections import deque
from typing import Any, Awaitable, Optional, Callable

from utils.flow_log import log_acp_turn, log_flow_event

DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
MULTIMODAL_MODEL = "opencode-go/mimo-v2-omni"


class OpenCodeACP:
    def __init__(
        self,
        cwd: str = ".",
        port: int = 0,
        hostname: str = "127.0.0.1",
        model: str = DEFAULT_MODEL,
        max_tokens: int = 4096,
        mcp_servers: list[dict] | None = None,
        *,
        reply_merge_enabled: bool = True,
    ):
        self.cwd = cwd
        self.port = port
        self.hostname = hostname
        self.model = model
        self.multimodal_model = MULTIMODAL_MODEL
        self.max_tokens = int(max_tokens) if max_tokens else 0
        self.mcp_servers = list(mcp_servers or [])
        self.reply_merge_enabled = bool(reply_merge_enabled)
        self._proc: Optional[subprocess.Popen] = None
        self._msg_id = 0
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stderr_lock = threading.Lock()
        self._running = False
        # 响应和通知队列
        self._response_queue = asyncio.Queue()
        # 收集阶段「误读」的下一帧信令放回队首，供下一次 recv 或下一轮 prompt 消费（单连接 JSON-RPC 保序）
        self._recollect_buf: deque = deque()
        self._notification_handler: Optional[Callable] = None
        # 单通道 JSON-RPC：同一时刻仅允许一个 in-flight 请求消费队列，避免串包
        self._rpc_lock = asyncio.Lock()
        # session 粒度权限模式：auto | confirm（默认 auto）
        self._session_permission_mode: dict[str, str] = {}
        # session 运行时上下文（如 from_user），供 permission 回调使用
        self._session_context: dict[str, dict[str, Any]] = {}
        # 待确认的 permission 请求：request_id -> {"future", ...}
        self._pending_permission_requests: dict[int, dict[str, Any]] = {}
        # 外部回调：用于把高危 permission 请求转给业务层（如微信确认）
        self._permission_request_handler: Optional[
            Callable[[dict[str, Any]], Awaitable[str] | str]
        ] = None
        self._permission_wait_timeout_sec: float = 300.0

    def _build_prompt_params(
        self,
        session_id: str,
        prompt_parts: list[dict],
        *,
        extra: dict | None = None,
    ) -> dict:
        params = {
            "sessionId": session_id,
            "prompt": prompt_parts,
        }
        if self.max_tokens > 0:
            params["maxTokens"] = self.max_tokens
        if extra:
            params.update(dict(extra))
        return params

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
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        await asyncio.sleep(2)
        if self._proc.poll() is not None:
            err_tail = ""
            if self._proc.stderr:
                try:
                    err_tail = self._proc.stderr.read()[:500]
                except Exception:
                    err_tail = ""
            raise RuntimeError(f"opencode acp exited: {err_tail}")

        # stderr 必须持续排空：PIPE 无人读时子进程写满会阻塞，服务模式下无 TTY 更易触发（stdout 永远无 JSON）
        self._stderr_thread = threading.Thread(
            target=self._stderr_drain_loop,
            daemon=True,
            name="acp-stderr",
        )
        self._stderr_thread.start()

        # 开始读线程
        self._running = True
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        # 握手：带 clientInfo + clientCapabilities
        resp = await self._send_and_recv(
            "initialize",
            {
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
            },
        )
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

    def _stderr_drain_loop(self):
        """Drain opencode stderr into logs so the PIPE never fills and blocks the child."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        log_path = Path(__file__).resolve().parent.parent / "logs" / "acp-stderr.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        try:
            with open(log_path, "a", encoding="utf-8", errors="replace") as lf:
                while True:
                    try:
                        line = proc.stderr.readline()
                    except Exception:
                        break
                    if not line:
                        break
                    with self._stderr_lock:
                        lf.write(line)
                        lf.flush()
        except Exception:
            pass

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

    def set_session_permission_mode(self, session_id: str, mode: str) -> None:
        m = (mode or "").strip().lower()
        self._session_permission_mode[session_id] = "confirm" if m == "confirm" else "auto"

    def set_session_context(self, session_id: str, context: dict[str, Any] | None) -> None:
        if not session_id:
            return
        self._session_context[session_id] = dict(context or {})

    def set_permission_request_handler(
        self, handler: Optional[Callable[[dict[str, Any]], Awaitable[str] | str]]
    ) -> None:
        self._permission_request_handler = handler

    def has_pending_permission_request(self, request_id: int) -> bool:
        return int(request_id) in self._pending_permission_requests

    def resolve_permission_request(self, request_id: int, *, approved: bool) -> bool:
        rid = int(request_id)
        pending = self._pending_permission_requests.get(rid)
        if not pending:
            return False
        fut = pending.get("future")
        if not isinstance(fut, asyncio.Future) or fut.done():
            return False
        fut.set_result("approved" if approved else "denied")
        return True

    @staticmethod
    def _extract_session_id_from_params(params: dict) -> str:
        if not isinstance(params, dict):
            return ""
        sid = (
            params.get("sessionId")
            or params.get("session_id")
            or params.get("id")
            or ""
        )
        if sid:
            return str(sid)
        # 部分实现会把 session 信息嵌在 request/target 中
        for key in ("request", "target", "metadata"):
            child = params.get(key)
            if isinstance(child, dict):
                sid2 = (
                    child.get("sessionId")
                    or child.get("session_id")
                    or child.get("id")
                    or ""
                )
                if sid2:
                    return str(sid2)
        return ""

    @staticmethod
    def _summarize_permission_params(params: dict) -> str:
        try:
            raw = json.dumps(params or {}, ensure_ascii=False)
        except Exception:
            raw = str(params)
        return raw[:800]

    async def _handle_permission_request(self, msg_id: int, params: dict):
        session_id = self._extract_session_id_from_params(params)
        mode = self._session_permission_mode.get(session_id, "auto")
        if mode != "confirm":
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"outcome": {"outcome": "approved"}},
                }
            )
            return

        context = dict(self._session_context.get(session_id) or {})
        req = {
            "request_id": msg_id,
            "session_id": session_id,
            "mode": mode,
            "params": params or {},
            "params_summary": self._summarize_permission_params(params or {}),
            "context": context,
        }
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending_permission_requests[msg_id] = {
            **req,
            "future": fut,
            "created_at": time.time(),
        }
        decision = "defer"
        cb = self._permission_request_handler
        if callable(cb):
            try:
                maybe = cb(req)
                decision = await maybe if asyncio.iscoroutine(maybe) else str(maybe or "defer")
            except Exception as e:  # noqa: BLE001
                print(f"[ACP DEBUG] permission handler error: {e}")
                decision = "defer"
        decision = str(decision or "defer").strip().lower()
        if decision in ("approved", "approve", "allow"):
            self._pending_permission_requests.pop(msg_id, None)
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"outcome": {"outcome": "approved"}},
                }
            )
            return
        if decision in ("denied", "deny", "reject"):
            self._pending_permission_requests.pop(msg_id, None)
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"outcome": {"outcome": "denied"}},
                }
            )
            return
        try:
            final_decision = await asyncio.wait_for(
                fut, timeout=self._permission_wait_timeout_sec
            )
        except asyncio.TimeoutError:
            final_decision = "denied"
        self._pending_permission_requests.pop(msg_id, None)
        self._write(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "outcome": {
                        "outcome": (
                            "approved"
                            if str(final_decision).strip().lower() == "approved"
                            else "denied"
                        )
                    }
                },
            }
        )

    @staticmethod
    def _extract_terminal_command(params: dict) -> tuple[list[str], bool]:
        if not isinstance(params, dict):
            return [], False
        command = params.get("command")
        if isinstance(command, str) and command.strip():
            return [command], True
        cmd = params.get("cmd")
        if isinstance(cmd, str) and cmd.strip():
            return [cmd], True
        program = params.get("program")
        args = params.get("args")
        if isinstance(program, str) and program.strip():
            if isinstance(args, list):
                return [program, *[str(x) for x in args]], False
            return [program], False
        return [], False

    async def _handle_terminal_request(self, method: str, msg_id: int, params: dict):
        cmd_parts, use_shell = self._extract_terminal_command(params or {})
        if not cmd_parts:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32000,
                        "message": f"unsupported terminal request payload for {method}",
                    },
                }
            )
            return
        cwd = str((params or {}).get("cwd") or self.cwd or ".")
        timeout_sec = float((params or {}).get("timeoutSec") or 60)

        def _run():
            if use_shell:
                return subprocess.run(
                    cmd_parts[0],
                    cwd=cwd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                    encoding="utf-8",
                    errors="replace",
                )
            return subprocess.run(
                cmd_parts,
                cwd=cwd,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                encoding="utf-8",
                errors="replace",
            )

        try:
            cp = await asyncio.to_thread(_run)
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "exitCode": int(cp.returncode),
                        "stdout": cp.stdout or "",
                        "stderr": cp.stderr or "",
                    },
                }
            )
        except Exception as e:  # noqa: BLE001
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32000, "message": str(e)},
                }
            )

    async def _handle_agent_request(self, msg: dict):
        """处理 agent→client 的请求（fs操作、权限审批、terminal）。"""
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        if method == "session/request_permission":
            await self._handle_permission_request(msg_id, params)
        elif method == "fs/read_text_file":
            fp = params.get("path", "")
            try:
                content = open(fp, encoding="utf-8", errors="replace").read()
                self._write(
                    {"jsonrpc": "2.0", "id": msg_id, "result": {"content": content}}
                )
            except Exception as e:
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32000, "message": str(e)},
                    }
                )
        elif method == "fs/write_text_file":
            fp = params.get("path", "")
            content = params.get("content", "")
            try:
                os.makedirs(os.path.dirname(fp), exist_ok=True)
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(content)
                self._write({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            except Exception as e:
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32000, "message": str(e)},
                    }
                )
        elif method == "fs/list_directory":
            dp = params.get("path", ".")
            try:
                entries = [
                    {"name": e.name, "type": "directory" if e.is_dir() else "file"}
                    for e in os.scandir(dp)
                ]
                self._write(
                    {"jsonrpc": "2.0", "id": msg_id, "result": {"entries": entries}}
                )
            except Exception as e:
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32000, "message": str(e)},
                    }
                )
        elif method in (
            "terminal/run_command",
            "terminal/exec",
            "terminal/run",
            "run_terminal_cmd",
        ):
            await self._handle_terminal_request(method, msg_id, params)
        else:
            log_flow_event(
                stage="acp",
                route="agent_request_unhandled",
                user_text="",
                session_id=str(self._extract_session_id_from_params(params) or ""),
                extra={
                    "method": str(method or ""),
                    "id": msg_id,
                    "params_preview": self._summarize_permission_params(params or {}),
                },
            )
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32601,
                        "message": f"unsupported agent request method: {method}",
                    },
                }
            )

    def _write(self, msg: dict):
        """写入 JSON-RPC 消息到 stdin"""
        if self._proc and self._proc.stdin:
            self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()

    def _send(self, method: str, params: dict = None):
        self._msg_id += 1
        msg = {
            "jsonrpc": "2.0",
            "id": self._msg_id,
            "method": method,
            "params": params or {},
        }
        self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()
        return self._msg_id

    async def _send_and_recv(
        self, method: str, params: dict = None, timeout: float = 120
    ) -> dict:
        """发送请求并等待匹配的响应，同时处理 agent→client 请求"""
        async with self._rpc_lock:
            msg_id = self._send(method, params)
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(self._recv_for_collect(), timeout=1.0)
                except asyncio.TimeoutError:
                    if self._proc and self._proc.poll() is not None:
                        raise RuntimeError("opencode acp process exited")
                    continue

                # Agent→Client 请求 → 自动处理
                if (
                    "method" in msg
                    and "id" in msg
                    and "result" not in msg
                    and "error" not in msg
                ):
                    await self._handle_agent_request(msg)
                    continue

                # 匹配请求 ID 的响应
                if msg.get("id") == msg_id:
                    return msg

                # 其他响应或通知，跳过
            raise TimeoutError(f"Timeout waiting for response to {method}")

    # ─── 文本提取 ───

    @staticmethod
    def _merge_stream_and_result_reply(
        stream_text: str,
        final_rpc_msg: dict | None,
    ) -> tuple[str, dict]:
        """合并流式 agent_message_chunk 与最终 JSON-RPC 中的文本，避免 matched_final_response 先到导致半句。

        返回 (reply, merge_meta)；merge_meta 供 flow 日志观测。
        """
        s = (stream_text or "").strip()
        r = OpenCodeACP._extract_text(final_rpc_msg or {}).strip()
        meta: dict = {
            "stream_reply_len": len(s),
            "result_reply_len": len(r),
            "reply_merge_conflict": False,
            "reply_selected_source": "",
        }
        if not s and not r:
            meta["reply_selected_source"] = "empty"
            return "", meta
        if not s:
            meta["reply_selected_source"] = "result"
            return r, meta
        if not r:
            meta["reply_selected_source"] = "stream"
            return s, meta
        if s == r:
            meta["reply_selected_source"] = "stream"
            return s, meta
        if r.startswith(s):
            meta["reply_selected_source"] = "merged"
            return r, meta
        if s.startswith(r):
            meta["reply_selected_source"] = "merged"
            return s, meta
        meta["reply_merge_conflict"] = True
        meta["reply_selected_source"] = "result"
        return r, meta

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

    @staticmethod
    def _json_brace_balanced(text: str) -> bool:
        """粗判 JSON 是否闭合（用于 tool args 分片结束判断）。"""
        s = (text or "").strip()
        if not s:
            return False
        depth = 0
        in_str = False
        esc = False
        for ch in s:
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        return depth == 0 and s.startswith("{") and s.endswith("}")

    @staticmethod
    def _tool_call_name_and_args(update: dict) -> tuple[str, str]:
        """兼容不同网关字段：提取 tool 名与参数分片。"""
        if not isinstance(update, dict):
            return "", ""
        name = str(
            update.get("name")
            or update.get("toolName")
            or update.get("tool_name")
            or ""
        ).strip()
        args = update.get("arguments")
        if args is None:
            args = update.get("args")
        if args is None:
            content = update.get("content")
            if isinstance(content, dict):
                args = content.get("arguments") or content.get("args")
                name = name or str(content.get("name") or content.get("toolName") or "").strip()
        return name, str(args or "")

    @staticmethod
    def _extract_first_json_object(text: str) -> dict | None:
        s = (text or "").strip()
        if not s:
            return None
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        try:
            import json_repair

            repaired = json_repair.repair_json(s, return_objects=True)
            if isinstance(repaired, dict):
                return repaired
        except Exception:
            pass
        dec = json.JSONDecoder()
        for i, ch in enumerate(s):
            if ch != "{":
                continue
            try:
                obj, _ = dec.raw_decode(s[i:])
            except Exception:
                continue
            if isinstance(obj, dict):
                return obj
        return None

    @staticmethod
    def _slice_balanced_json_object(text: str, start_idx: int) -> str | None:
        """从 `text[start_idx]=='{'` 起切出花括号平衡的 JSON 子串；失败返回 None。"""
        s = text or ""
        if start_idx < 0 or start_idx >= len(s) or s[start_idx] != "{":
            return None
        depth = 0
        in_str = False
        esc = False
        i = start_idx
        while i < len(s):
            ch = s[i]
            if esc:
                esc = False
                i += 1
                continue
            if ch == "\\" and in_str:
                esc = True
                i += 1
                continue
            if ch == '"':
                in_str = not in_str
                i += 1
                continue
            if not in_str:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return s[start_idx : i + 1]
            i += 1
        return None

    @staticmethod
    def _recover_schema_obj_from_partial_tool_payload(
        text: str, json_schema: dict
    ) -> dict | None:
        """整段 JSON 不可解析时，尝试从原文正则提取 tool + payload（不恢复残缺 reply）。"""
        s = (text or "").strip()
        if not s:
            return None
        tm = re.search(r'"tool"\s*:\s*"([^"]+)"', s)
        if not tm:
            return None
        tool_raw = tm.group(1).strip()
        pm = re.search(r'"payload"\s*:\s*\{', s)
        if not pm:
            return None
        brace_start = pm.end() - 1
        blob = OpenCodeACP._slice_balanced_json_object(s, brace_start)
        if not blob:
            return None
        try:
            payload_obj = json.loads(blob)
        except Exception:
            return None
        if not isinstance(payload_obj, dict):
            return None
        cand: dict = {"tool": tool_raw, "payload": payload_obj}
        props = json_schema.get("properties") or {}
        if isinstance(props, dict) and "reply" in props:
            cand.setdefault("reply", "")
        return cand

    @staticmethod
    def _project_obj_to_schema_properties(obj: dict | None, schema: dict) -> dict | None:
        """schema 含 properties 且 additionalProperties=false 时，先丢掉未声明的键再校验。

        避免模型在仅允许 tool+payload 的回合误塞 reply、message 等导致校验失败、连续重试仍失败。
        """
        if not isinstance(obj, dict) or not isinstance(schema, dict):
            return obj
        props = schema.get("properties")
        if not isinstance(props, dict) or not props:
            return obj
        if schema.get("additionalProperties") is False:
            return {k: v for k, v in obj.items() if k in props}
        return obj

    @staticmethod
    def _take_structured_reply_spill(raw_obj: dict | None, schema: dict) -> str:
        """若原始 JSON 含 reply 且 schema 不允许该键，保留文案供上游复用，避免再打一枪 unified_reply。"""
        if not isinstance(raw_obj, dict) or not isinstance(schema, dict):
            return ""
        if schema.get("additionalProperties") is not False:
            return ""
        props = schema.get("properties") or {}
        if "reply" in props:
            return ""
        r = raw_obj.get("reply")
        if r is None:
            return ""
        return str(r).strip()

    # dispatcher 在合并决策后 pop 掉，仅用于观测
    STRUCTURED_TRACE_META_KEY = "_structured_trace"

    _STRUCTURED_VAGUE_REFERENCE_SUBSTRINGS = (
        "刚给你列过",
        "刚才说过了",
        "刚说过",
        "前面说过了",
        "已经列过",
        "刚才列过了",
        "前面列过",
        "都说过了",
        "已经说过",
        "前面给你",
        "刚给你看过",
    )

    @classmethod
    def _structured_informativeness(cls, s: str) -> int:
        """粗分：用于在多次 structured 尝试间保留更有信息量的用户可见文本。"""
        t = (s or "").strip()
        if len(t) < 24:
            return 0
        score = min(len(t) // 100, 6)
        if re.search(r"\b[0-9a-f]{7,40}\b", t, re.I):
            score += 10
        if "`" in t or "feat:" in t or "fix:" in t or "refactor:" in t:
            score += 5
        if "\n-" in t or "\n•" in t or re.search(r"\n\s*[-*]\s", t):
            score += 6
        if "http://" in t or "https://" in t:
            score += 4
        return score

    @classmethod
    def _structured_is_vague_reference_reply(cls, s: str) -> bool:
        t = (s or "").strip()
        if not t or len(t) > 420:
            return False
        if cls._structured_informativeness(t) >= 12:
            return False
        return any(x in t for x in cls._STRUCTURED_VAGUE_REFERENCE_SUBSTRINGS)

    @classmethod
    def _pick_richer_structured_reply(cls, current: str, candidate: str) -> str:
        ca = (candidate or "").strip()
        if not ca:
            return current
        if cls._structured_informativeness(ca) > cls._structured_informativeness(current):
            return ca
        return current

    def _accumulate_best_effort_from_raw(
        self,
        best_effort: str,
        reply_raw: str,
        _reasoning_raw: str,
        json_schema: dict,
    ) -> str:
        """从单轮模型主回复中抽取可回注的用户可见片段（仅 reply，不含 reasoning）。"""
        props = json_schema.get("properties") or {}
        has_reply = "reply" in props
        out = best_effort
        # 安全约束：reasoning 绝不参与用户可见 best_effort 回注，避免把思维链误发到微信侧。
        for chunk in (reply_raw or "",):
            c = (chunk or "").strip()
            if not c:
                continue
            ro = self._extract_first_json_object(c)
            if isinstance(ro, dict) and has_reply:
                r = str(ro.get("reply") or "").strip()
                projected = self._project_obj_to_schema_properties(ro, json_schema)
                if r and (
                    not isinstance(projected, dict)
                    or not self._validate_schema_obj(projected, json_schema)
                ):
                    out = self._pick_richer_structured_reply(out, r)
            if ro is None and len(c) >= 36:
                if c.startswith("{") and not c.endswith("}"):
                    continue
                if c.startswith("{") and c.endswith("}"):
                    continue
                out = self._pick_richer_structured_reply(out, c)
        return out

    @classmethod
    def _merge_structured_final_reply(
        cls,
        final_reply: str,
        best_effort: str,
        tool: str,
    ) -> tuple[str, str]:
        """返回 (merged_reply, final_reply_source)。"""
        fr = (final_reply or "").strip()
        be = (best_effort or "").strip()
        tv = (tool or "").strip().lower()
        if not be or tv != "none":
            return fr, "combined_direct"
        if cls._structured_is_vague_reference_reply(fr) and cls._structured_informativeness(
            be
        ) > cls._structured_informativeness(fr):
            merged = f"{be.rstrip()}\n\n{fr}".strip()
            return merged, "combined_merged_best+vague_final"
        if cls._structured_informativeness(be) > cls._structured_informativeness(fr) + 4:
            merged = f"{be.rstrip()}\n\n{fr}".strip() if fr else be
            return merged, "combined_merged_informativeness"
        return fr, "combined_direct"

    @classmethod
    def _validate_schema_obj(cls, obj: dict, schema: dict) -> bool:
        if not isinstance(obj, dict) or not isinstance(schema, dict):
            return False
        if schema.get("type") and schema.get("type") != "object":
            return False
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        for k in required:
            if k not in obj:
                return False
        if schema.get("additionalProperties") is False:
            for k in obj:
                if k not in props:
                    return False
        for k, v in obj.items():
            ps = props.get(k)
            if not isinstance(ps, dict):
                continue
            t = ps.get("type")
            if t == "string" and not isinstance(v, str):
                return False
            if t == "object" and not isinstance(v, dict):
                return False
            if t == "array" and not isinstance(v, list):
                return False
            if t == "number" and not isinstance(v, (int, float)):
                return False
            if t == "integer" and not isinstance(v, int):
                return False
            if t == "boolean" and not isinstance(v, bool):
                return False
            enum_vals = ps.get("enum")
            if isinstance(enum_vals, list) and v not in enum_vals:
                return False
            if t == "object" and isinstance(v, dict):
                child_required = ps.get("required")
                child_props = ps.get("properties")
                child_additional = ps.get("additionalProperties")
                if child_required or child_props is not None or child_additional is False:
                    if not cls._validate_schema_obj(v, ps):
                        return False
        return True

    # ─── 收集流式响应 ───

    async def _recv_for_collect(self) -> dict:
        """优先消费「回收队首」，再读 ACP 队列（与 _unget_for_collect 成对）。"""
        if self._recollect_buf:
            return self._recollect_buf.popleft()
        return await self._response_queue.get()

    def _unget_for_collect(self, msg: dict) -> None:
        """把本不该在尾部排空阶段消费的消息放回队首，保证 JSON-RPC 顺序。"""
        self._recollect_buf.appendleft(msg)

    async def _collect_prompt_response(
        self,
        msg_id: int,
        *,
        session_id: str = "",
        timeout: float = 180,
    ) -> dict:
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
        saw_end_turn = False
        saw_final_response = False
        break_reason = "deadline_timeout"
        update_counters: dict[str, int] = {}
        # tool_call 参数分片：只在参数闭合后视为可执行，避免 partial arguments 闪烁/误执行
        tool_args_buf: dict[str, str] = {}
        tool_args_partial_updates = 0
        tool_args_finalized = 0
        tool_status_transitions: list[str] = []
        deadline = time.time() + timeout
        last_activity = time.time()

        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(self._recv_for_collect(), timeout=1.0)
            except asyncio.TimeoutError:
                if self._proc and self._proc.poll() is not None:
                    break_reason = "process_exited"
                    break
                if time.time() - last_activity > 30:
                    break_reason = "idle_timeout_30s"
                    break
                continue

            last_activity = time.time()

            if (
                "method" in msg
                and "id" in msg
                and "result" not in msg
                and "error" not in msg
            ):
                await self._handle_agent_request(msg)
                continue

            # 最终响应（匹配 msg_id）
            if msg.get("id") == msg_id:
                final_result = msg
                saw_final_response = True
                break_reason = "matched_final_response"
                # 与下方「其它响应」一致：把 result.parts 并入正文（部分网关只在 result 里给全文）
                if "result" in msg:
                    res = msg.get("result") or {}
                    for p in res.get("parts", []) or []:
                        if p.get("type") == "text":
                            all_text.append(p.get("text", ""))
                # ACP 常见竞态：JSON-RPC result 先于最后几帧 agent_message_chunk 入队；
                # 若立即 break，流式 JSON 会残缺 → structured 走 partial 恢复且 reply 为空。
                tail_deadline = time.time() + 0.35
                while time.time() < tail_deadline:
                    try:
                        msg2 = await asyncio.wait_for(self._recv_for_collect(), timeout=0.06)
                    except asyncio.TimeoutError:
                        break
                    last_activity = time.time()
                    if (
                        "method" in msg2
                        and "id" in msg2
                        and "result" not in msg2
                        and "error" not in msg2
                    ):
                        await self._handle_agent_request(msg2)
                        continue
                    if msg2.get("method") == "session/update":
                        params2 = msg2.get("params", {}) or {}
                        if session_id:
                            sid2 = (
                                params2.get("sessionId")
                                or params2.get("session_id")
                                or params2.get("id")
                                or ""
                            )
                            if sid2 and sid2 != session_id:
                                self._unget_for_collect(msg2)
                                break
                        all_raw.append(msg2)
                        update2 = params2.get("update", {})
                        su2 = update2.get("sessionUpdate", "")
                        if su2:
                            update_counters[su2] = update_counters.get(su2, 0) + 1
                        if su2 == "text_delta":
                            all_text.append(update2.get("textDelta", ""))
                        elif su2 == "agent_message_chunk":
                            content2 = update2.get("content", {})
                            if isinstance(content2, dict) and content2.get("type") == "text":
                                all_text.append(content2.get("text", ""))
                            elif isinstance(content2, list):
                                for part in content2:
                                    if isinstance(part, dict) and part.get("type") == "text":
                                        all_text.append(part.get("text", ""))
                        elif su2 == "agent_thought_chunk":
                            content2 = update2.get("content", {})
                            if isinstance(content2, dict) and content2.get("type") == "text":
                                all_reasoning.append(content2.get("text", ""))
                        elif su2 == "thinking":
                            all_reasoning.append(
                                update2.get("textDelta", update2.get("text", ""))
                            )
                        elif su2 in ("tool_call_update", "tool_call"):
                            name, arg_chunk = self._tool_call_name_and_args(update2)
                            if name:
                                tool_status_transitions.append(f"executing:{name}")
                            if arg_chunk:
                                key = name or f"tool_{len(tool_args_buf)+1}"
                                tool_args_buf[key] = (tool_args_buf.get(key, "") + arg_chunk)
                                tool_args_partial_updates += 1
                                if self._json_brace_balanced(tool_args_buf[key]):
                                    tool_args_finalized += 1
                        elif su2 == "end_turn":
                            saw_end_turn = True
                            break_reason = "end_turn"
                            all_notifications.append(msg2)
                            break
                        elif su2 == "usage_update":
                            pass
                        else:
                            if su2:
                                _uk2 = (
                                    list(update2.keys())[:5]
                                    if isinstance(update2, dict)
                                    else []
                                )
                                print(
                                    f"[ACP DEBUG] unhandled sessionUpdate (tail): {su2}, keys={_uk2}"
                                )
                        all_notifications.append(msg2)
                        if su2 == "end_turn":
                            break
                        continue
                    self._unget_for_collect(msg2)
                    break
                break

            # 记录所有消息用于调试
            all_raw.append(msg)

            # session/update 通知
            if msg.get("method") == "session/update":
                params = msg.get("params", {}) or {}
                # 防串包：只收集当前 session 的流式更新
                if session_id:
                    sid = (
                        params.get("sessionId")
                        or params.get("session_id")
                        or params.get("id")
                        or ""
                    )
                    if sid and sid != session_id:
                        continue
                update = params.get("update", {})
                su = update.get("sessionUpdate", "")
                if su:
                    update_counters[su] = update_counters.get(su, 0) + 1
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
                    all_reasoning.append(
                        update.get("textDelta", update.get("text", ""))
                    )
                elif su in ("tool_call_update", "tool_call"):
                    name, arg_chunk = self._tool_call_name_and_args(update)
                    if name:
                        tool_status_transitions.append(f"executing:{name}")
                    if arg_chunk:
                        key = name or f"tool_{len(tool_args_buf)+1}"
                        tool_args_buf[key] = (tool_args_buf.get(key, "") + arg_chunk)
                        tool_args_partial_updates += 1
                        if self._json_brace_balanced(tool_args_buf[key]):
                            tool_args_finalized += 1
                elif su == "end_turn":
                    saw_end_turn = True
                    break_reason = "end_turn"
                    break
                elif su == "usage_update":
                    # OpenCode 推送的用量/计费元数据；不参与正文拼接，已由 update_counters 计数。
                    pass
                else:
                    if su:
                        _uk = (
                            list(update.keys())[:5] if isinstance(update, dict) else []
                        )
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
            "saw_end_turn": saw_end_turn,
            "saw_final_response": saw_final_response,
            "break_reason": break_reason,
            "update_counters": update_counters,
            "tool_args_partial_updates": tool_args_partial_updates,
            "tool_args_finalized": tool_args_finalized,
            "tool_status_transitions": tool_status_transitions[:12],
        }

    # ─── 业务 API ───

    async def create_session(self, model: str = None) -> str:
        """创建 session，设置模型，返回 session_id

        ACP 协议不支持在 session/prompt 中指定模型，
        必须通过 session/set_config_option 设置。
        模型格式: "provider/model" 如 "opencode-go/deepseek-v4-flash"
        """
        params = {"cwd": self.cwd, "mcpServers": self.mcp_servers}

        resp = await self._send_and_recv("session/new", params)
        result = resp.get("result", {})
        sid = result.get("sessionId", result.get("id", ""))

        if not sid:
            raise RuntimeError(
                f"Failed to create session: {json.dumps(resp, ensure_ascii=False)[:300]}"
            )

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
        resp = await self._send_and_recv(
            "session/set_config_option",
            {
                "sessionId": session_id,
                "configId": "model",
                "value": model,
            },
        )
        if "error" in resp:
            print(f"[ACP] set_model error: {resp['error']}")
        else:
            print(f"[ACP] model set to: {model}")

    async def prompt(
        self,
        session_id: str,
        message: str,
        *,
        trace_tag: str = "prompt",
        session_context: dict[str, Any] | None = None,
        prompt_extra: dict | None = None,
    ) -> tuple:
        """发送文本消息，返回 (reply_text, reasoning_text)"""
        async with self._rpc_lock:
            if session_context is not None:
                self.set_session_context(session_id, session_context)
            msg_id = self._send(
                "session/prompt",
                self._build_prompt_params(
                    session_id,
                    [{"type": "text", "text": message}],
                    extra=prompt_extra,
                ),
            )
            collected = await self._collect_prompt_response(
                msg_id, session_id=session_id, timeout=180
            )

        # 调试：空响应时 dump 信息
        if not collected["text"] and not collected["reasoning"]:
            result_keys = (
                list(collected.get("result", {}).keys())
                if collected.get("result")
                else []
            )
            print(
                f"[ACP DEBUG] empty response! raw_count={collected.get('raw_count',0)}, "
                f"result_keys={result_keys}, notifications={len(collected.get('notifications',[]))}"
            )
            if collected.get("result"):
                print(
                    f"[ACP DEBUG] result sample: {json.dumps(collected['result'], ensure_ascii=False)[:500]}"
                )

        if self.reply_merge_enabled:
            reply, merge_meta = self._merge_stream_and_result_reply(
                collected["text"], collected.get("result")
            )
        else:
            if collected["text"]:
                reply = collected["text"]
            else:
                reply = self._extract_text(collected["result"])
            merge_meta = {
                "stream_reply_len": len((collected["text"] or "").strip()),
                "result_reply_len": len(self._extract_text(collected.get("result") or {})),
                "reply_merge_conflict": False,
                "reply_selected_source": (
                    "stream"
                    if (collected["text"] or "").strip()
                    else ("result" if reply else "empty")
                ),
            }
        reasoning = collected["reasoning"]
        log_acp_turn(
            trace=trace_tag,
            session_id=session_id,
            model=self.model,
            prompt=message,
            reply=reply or "",
            reasoning=reasoning or "",
            meta={
                "raw_count": collected.get("raw_count"),
                "notification_count": len(collected.get("notifications", [])),
                "saw_end_turn": collected.get("saw_end_turn"),
                "saw_final_response": collected.get("saw_final_response"),
                "break_reason": collected.get("break_reason"),
                "update_counters": collected.get("update_counters"),
                "tool_args_partial_updates": collected.get("tool_args_partial_updates"),
                "tool_args_finalized": collected.get("tool_args_finalized"),
                "tool_status_transitions": collected.get("tool_status_transitions"),
                **merge_meta,
            },
        )
        return reply, reasoning

    # 仅 prompt_structured → dispatcher 使用：校验通过后附带回用的用户可见 reply，随后由 dispatcher pop 掉
    STRUCTURED_DECISION_SPILL_REPLY_KEY = "_spill_reply"

    async def prompt_structured(
        self,
        session_id: str,
        message: str,
        *,
        json_schema: dict,
        retry_count: int = 3,
        trace_tag: str = "prompt_structured",
    ) -> dict | None:
        """结构化输出：文本协议 + 本地 schema 校验 + retry。

        不依赖 provider 原生 function calling，兼容 openai-compatible 网关。
        若模型在 JSON 里多写了 reply 且被 schema 剥离，成功时会把原文放在键
        STRUCTURED_DECISION_SPILL_REPLY_KEY 上一并返回，供 dispatcher 在 tool=none 时复用，免再打 unified_reply。

        多轮重试时保留「信息量更高」的中间自然语言（best_effort），避免首轮已列出事实
        但次轮仅输出空泛承接句导致微信侧看不到原文。
        """
        schema_text = json.dumps(json_schema, ensure_ascii=False)
        structured_prompt = (
            f"{message}\n\n"
            "你必须只返回一个 JSON 对象，且严格满足下面的 JSON Schema。\n"
            "禁止输出解释、markdown、代码块。\n"
            f"JSON Schema: {schema_text}\n"
            "仅输出 JSON："
        )
        attempts = max(1, int(retry_count or 1))
        props = json_schema.get("properties") or {}
        has_reply_field = isinstance(props, dict) and "reply" in props
        best_effort = ""
        retry_feedback = ""

        def _build_retry_feedback(candidate: dict | None, *, source: str) -> str:
            if not isinstance(candidate, dict):
                return f"{source} 未解析出合法 JSON 对象。"
            required = json_schema.get("required") or []
            missing = [str(k) for k in required if k not in candidate]
            if missing:
                return "缺少必填字段: " + ", ".join(missing)
            schema_props = json_schema.get("properties") or {}
            type_hints: list[str] = []
            for key, spec in schema_props.items():
                if key not in candidate or not isinstance(spec, dict):
                    continue
                expected = spec.get("type")
                value = candidate.get(key)
                if expected == "string" and not isinstance(value, str):
                    type_hints.append(f"{key} 应为 string")
                elif expected == "object" and not isinstance(value, dict):
                    type_hints.append(f"{key} 应为 object")
                elif expected == "array" and not isinstance(value, list):
                    type_hints.append(f"{key} 应为 array")
                elif expected == "number" and not isinstance(value, (int, float)):
                    type_hints.append(f"{key} 应为 number")
                elif expected == "integer" and not isinstance(value, int):
                    type_hints.append(f"{key} 应为 integer")
                elif expected == "boolean" and not isinstance(value, bool):
                    type_hints.append(f"{key} 应为 boolean")
            if type_hints:
                return "类型不匹配: " + "; ".join(type_hints[:3])
            if not self._validate_schema_obj(candidate, json_schema):
                return "JSON 未通过 schema 校验（可能是 enum 或额外字段问题）。"
            return ""

        def _finalize_out(
            base: dict,
            *,
            spill_text: str,
            attempt_idx: int,
        ) -> dict:
            out = dict(base)
            tool_s = str(out.get("tool") or "none").strip().lower()
            reply_src = "combined_direct"
            if has_reply_field:
                fr0 = str(out.get("reply") or "").strip()
                merged, reply_src = self._merge_structured_final_reply(
                    fr0, best_effort, tool_s
                )
                out["reply"] = merged
            spill_final = (spill_text or "").strip()
            if not has_reply_field and tool_s == "none" and best_effort.strip():
                if self._structured_informativeness(best_effort) > self._structured_informativeness(
                    spill_final
                ):
                    spill_final = best_effort.strip()
            if spill_final:
                out[self.STRUCTURED_DECISION_SPILL_REPLY_KEY] = spill_final
            trace = {
                "structured_attempts": attempt_idx,
                "best_effort_reply_len": len((best_effort or "").strip()),
                "final_reply_source": reply_src
                if has_reply_field
                else ("decision_only_spill" if spill_final else "decision_only"),
            }
            if trace["structured_attempts"] > 1 or trace["best_effort_reply_len"] > 0:
                out[self.STRUCTURED_TRACE_META_KEY] = trace
                log_acp_turn(
                    trace=f"{trace_tag}_summary",
                    session_id=session_id,
                    model=self.model,
                    prompt=f"(structured summary) {message[:400]}",
                    reply="",
                    reasoning="",
                    meta={
                        "structured_attempts": trace["structured_attempts"],
                        "best_effort_reply_len": trace["best_effort_reply_len"],
                        "final_reply_source": trace["final_reply_source"],
                    },
                )
            return out

        for i in range(attempts):
            attempt_prompt = structured_prompt
            if retry_feedback:
                attempt_prompt = (
                    f"{structured_prompt}\n\n"
                    "上次输出存在格式问题，请只修复格式，不要改语义内容。\n"
                    f"错误摘要：{retry_feedback}\n"
                    "再次强调：只输出一个合法 JSON 对象，不要输出解释文字。"
                )
            reply, reasoning = await self.prompt(
                session_id,
                attempt_prompt,
                trace_tag=f"{trace_tag}#{i + 1}",
                prompt_extra={"response_format": {"type": "json_object"}},
            )
            best_effort = self._accumulate_best_effort_from_raw(
                best_effort, reply or "", reasoning or "", json_schema
            )
            raw_obj = self._extract_first_json_object(reply or "")
            spill = self._take_structured_reply_spill(raw_obj, json_schema)
            obj = self._project_obj_to_schema_properties(raw_obj, json_schema)
            if isinstance(obj, dict) and self._validate_schema_obj(obj, json_schema):
                return _finalize_out(obj, spill_text=spill, attempt_idx=i + 1)
            raw_obj2 = self._extract_first_json_object(reasoning or "")
            spill2 = self._take_structured_reply_spill(raw_obj2, json_schema)
            obj2 = self._project_obj_to_schema_properties(raw_obj2, json_schema)
            if isinstance(obj2, dict) and self._validate_schema_obj(obj2, json_schema):
                return _finalize_out(obj2, spill_text=spill2, attempt_idx=i + 1)

            for probe in (reply or "", reasoning or ""):
                recovered = OpenCodeACP._recover_schema_obj_from_partial_tool_payload(
                    probe, json_schema
                )
                if not isinstance(recovered, dict):
                    continue
                projected = self._project_obj_to_schema_properties(recovered, json_schema)
                if isinstance(projected, dict) and self._validate_schema_obj(
                    projected, json_schema
                ):
                    raw_spill = self._extract_first_json_object(probe)
                    spill_r = self._take_structured_reply_spill(raw_spill, json_schema)
                    return _finalize_out(
                        projected, spill_text=spill_r, attempt_idx=i + 1
                    )

            retry_feedback = (
                _build_retry_feedback(obj, source="reply")
                or _build_retry_feedback(obj2, source="reasoning")
                or "未输出可解析且符合 schema 的 JSON 对象。"
            )

        log_flow_event(
            stage="route",
            route="prompt_structured_fail",
            user_text="",
            session_id=session_id,
            extra={
                "trace_tag": trace_tag,
                "structured_attempts": attempts,
                "best_effort_reply_len": len((best_effort or "").strip()),
            },
        )
        return None

    async def prompt_with_image(
        self,
        session_id: str,
        text: str,
        image_bytes: bytes = b"",
        mime_type: str = "image/jpeg",
        *,
        trace_tag: str = "prompt_with_image",
        log_model: Optional[str] = None,
    ) -> tuple:
        """发送文本+图片（base64 内联），返回 (reply_text, reasoning_text)

        ACP 协议不支持 file:// 路径，需要 base64 内联图片数据。
        """
        prompt_parts = [{"type": "text", "text": text}]
        if image_bytes:
            import base64 as _b64

            b64_data = _b64.b64encode(image_bytes).decode("ascii")
            prompt_parts.append(
                {
                    "type": "image",
                    "mimeType": mime_type,
                    "data": b64_data,
                }
            )
        async with self._rpc_lock:
            msg_id = self._send(
                "session/prompt",
                self._build_prompt_params(session_id, prompt_parts),
            )
            collected = await self._collect_prompt_response(
                msg_id, session_id=session_id, timeout=180
            )
        if self.reply_merge_enabled:
            reply, merge_meta = self._merge_stream_and_result_reply(
                collected["text"], collected.get("result")
            )
        else:
            if collected["text"]:
                reply = collected["text"]
            else:
                reply = self._extract_text(collected["result"])
            merge_meta = {
                "stream_reply_len": len((collected["text"] or "").strip()),
                "result_reply_len": len(self._extract_text(collected.get("result") or {})),
                "reply_merge_conflict": False,
                "reply_selected_source": (
                    "stream"
                    if (collected["text"] or "").strip()
                    else ("result" if reply else "empty")
                ),
            }
        reasoning = collected["reasoning"]
        # 多模态不把 base64 打进 flow 日志
        prompt_for_log = text + (f"\n[image bytes={len(image_bytes)} mime={mime_type}]" if image_bytes else "")
        log_acp_turn(
            trace=trace_tag,
            session_id=session_id,
            model=log_model or self.multimodal_model,
            prompt=prompt_for_log,
            reply=reply or "",
            reasoning=reasoning or "",
            meta={
                "raw_count": collected.get("raw_count"),
                "notification_count": len(collected.get("notifications", [])),
                "saw_end_turn": collected.get("saw_end_turn"),
                "saw_final_response": collected.get("saw_final_response"),
                "break_reason": collected.get("break_reason"),
                "update_counters": collected.get("update_counters"),
                "tool_args_partial_updates": collected.get("tool_args_partial_updates"),
                "tool_args_finalized": collected.get("tool_args_finalized"),
                "tool_status_transitions": collected.get("tool_status_transitions"),
                **merge_meta,
            },
        )
        return reply, reasoning


def _coach_skill_path(vault_root: str, name: str) -> str:
    return str(Path(vault_root).resolve() / ".opencode" / "skills" / name / "SKILL.md")


def todo_coach_skill_path(vault_root: str) -> str:
    """OpenCode 技能 todo-coach 的绝对路径（与 ACP cwd 下 .opencode 一致）。"""
    return _coach_skill_path(vault_root, "todo-coach")


def record_coach_skill_path(vault_root: str) -> str:
    """OpenCode 技能 record-coach 的绝对路径。"""
    return _coach_skill_path(vault_root, "record-coach")


def remind_coach_skill_path(vault_root: str) -> str:
    """OpenCode 技能 remind-coach 的绝对路径。"""
    return _coach_skill_path(vault_root, "remind-coach")


def build_todo_coach_skill_binding(vault_root: str) -> str:
    """绑定待办统筹技能：让模型显式以 todo-coach 为准，避免与泛化「生活日志」规则打架。"""
    p = todo_coach_skill_path(vault_root)
    return f"""## 待办技能 todo-coach（最高优先级）
凡涉及 `## 📋 待办` 的读取、分组、催办、整组打勾、跳过/放弃/重排、批量添加、微信采访式追问，你必须严格遵循 OpenCode 技能 **todo-coach**，而不是凭默认 Markdown 习惯臆造格式。
- 技能文件路径（处理待办前应用 fs/read_text_file 读取全文）：`{p}`
- 待办行的分组（每行最多五项、中文逗号）、整组完成后才改 `- [x]`、进度 (N/M) 文案等，均以该文件为准；与本提示其它段落冲突时 **以 todo-coach 为准**。"""


def task_decompose_skill_path(vault_root: str) -> str:
    return _coach_skill_path(vault_root, "task-decompose")


def build_task_decompose_skill_binding(vault_root: str) -> str:
    p = task_decompose_skill_path(vault_root)
    return f"""## 任务分解技能 task-decompose
当用户说「分解 xxx」「拆分 xxx」「拆解 xxx」时，进入多轮协商模式把任务拆成可执行步骤，协商完成后按用户选择存模板或导入待办。
- 技能文件路径：`{p}`
- 规则以 task-decompose SKILL 为准，不要凭默认习惯臆造。"""


def daily_summary_skill_path(vault_root: str) -> str:
    return _coach_skill_path(vault_root, "daily-summary")


def build_daily_summary_skill_binding(vault_root: str) -> str:
    p = daily_summary_skill_path(vault_root)
    return f"""## 日报总结技能 daily-summary
当用户说「总结」「日报」「今天干什么了」「今天怎么样」时，读取今日生活日志生成三项汇总（记录/待办/提醒），只读不写。
- 技能文件路径：`{p}`
- 规则以 daily-summary SKILL 为准。"""


def build_system_prompt(
    vault_root: str, daily_log_dir: str, project_dir: str = "", task_dir: str = ""
) -> str:
    todo_block = build_todo_coach_skill_binding(vault_root)
    decompose_block = build_task_decompose_skill_binding(vault_root)
    summary_block = build_daily_summary_skill_binding(vault_root)
    skill_block = f"{todo_block}\n\n{decompose_block}\n\n{summary_block}"
    return f"""你是"生生项目"的生活日志助手。只处理生活相关的事，不处理工作/学术任务。

## 核心规则
1. 文件三章节：`## 📝 记录` / `## ⏰ 提醒` / `## 📋 待办`
2. 写入文件：{daily_log_dir}/YYYY/MM/YYYY-MM-DD.md
3. 日期提取：消息中如有明确日期（如"5月2日"），写入对应日期文件，而非今天
4. 📝 记录（已发生）→ 按分类分节 `### 身体/运动/阅读/事务` → `- [x] 内容 ✅HH:MM`
5. ⏰ 提醒（有目标时间）→ 平铺 → `- [ ] 目标时间：内容`
6. 📋 待办：格式与催办流程 **不按「每项单独一行」的简化规则**；必须遵守技能 **todo-coach**（见下文「待办技能」），含分组书写、整组标记、采访式推进。
7. `✅HH:MM`：📝 记录每条可带；⏰ 提醒行不带完成戳；📋 待办整组完成、行末时间等 **一律按 todo-coach**，勿套用「只有记录能带时间」的旧口诀
8. 章节/分类节不存在则创建，同一分类追加在同一节内（📝）；待办节重写/追加以 todo-coach 为准
9. 先读文件再修改，避免重复
10. 回复：简要确认（中文），偏微信短句；待办催办话术以 todo-coach「响应格式」为准

{skill_block}

## 路径
- vault 根: {vault_root}
- 生活日志: {daily_log_dir}/YYYY/MM/YYYY-MM-DD.md

## 日期识别
- "N月N日" / "N.N" / "N月N号" → 当前年份的该日期
- "明天" / "后天" → 相对日期
- 无明确日期 → 使用今天"""
