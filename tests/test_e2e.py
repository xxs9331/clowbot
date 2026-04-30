"""Phase 3 E2E - 完整 ACP 协议实现（参考 routa/onyx/hapi 等项目）

关键协议要点：
1. initialize 需要 clientInfo + clientCapabilities
2. session/prompt 用 'prompt' 字段（不是 'parts'）
3. 必须响应 agent→client 请求（fs 操作、权限审批）
4. session/update 是流式通知
"""
import subprocess, json, time, os, sys

TEST_MSG = "体重 68.5kg"
VAULT = r"D:\Lenovo\Documents\世界树\世界树"

def find_opencode():
    for p in [os.path.expandvars(r"%NODIST_PREFIX%\bin\opencode.cmd"),
              r"E:\Program Files (x86)\Nodist\bin\opencode.cmd", "opencode"]:
        if p == "opencode" or os.path.exists(p): return p
    raise FileNotFoundError

def recv_lines(proc, timeout=120, idle_timeout=15):
    """读取所有可用的 JSON-RPC 行，支持超时和空闲检测"""
    results = []
    deadline = time.time() + timeout
    last_msg = time.time()
    while time.time() < deadline:
        if proc.poll() is not None:
            # 进程已结束，尝试读最后一行
            line = proc.stdout.readline()
            if line and line.strip():
                try: results.append(json.loads(line.strip()))
                except: pass
            break
        line = proc.stdout.readline()
        if line:
            line = line.strip()
            if line:
                try:
                    results.append(json.loads(line))
                    last_msg = time.time()
                except json.JSONDecodeError:
                    pass
        else:
            break  # EOF
        # 空闲超时：已收到响应且长时间无新数据
        if results and time.time() - last_msg > idle_timeout:
            break
    return results


def handle_agent_request(proc, msg):
    """处理 agent→client 的请求（fs 操作、权限审批等）
    参考 routa server.js 的 _handleAgentRequest
    """
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params", {})
    
    if method == "session/request_permission":
        # 自动批准所有权限请求
        print(f"    [PERMISSION] Auto-approving: {params}")
        resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"outcome": {"outcome": "approved"}}}
    elif method == "fs/read_text_file":
        # 读取文件 — agent 要读本地文件
        fp = params.get("path", "")
        print(f"    [FS READ] {fp}")
        try:
            content = open(fp, encoding="utf-8", errors="replace").read()
            resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"content": content}}
        except Exception as e:
            resp = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": str(e)}}
    elif method == "fs/write_text_file":
        # 写入文件 — agent 要写本地文件
        fp = params.get("path", "")
        content = params.get("content", "")
        print(f"    [FS WRITE] {fp} ({len(content)} chars)")
        try:
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                f.write(content)
            resp = {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        except Exception as e:
            resp = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": str(e)}}
    elif method == "fs/list_directory":
        # 列出目录
        dp = params.get("path", ".")
        print(f"    [FS LIST] {dp}")
        try:
            entries = []
            for e in os.scandir(dp):
                entries.append({"name": e.name, "type": "directory" if e.is_dir() else "file"})
            resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"entries": entries}}
        except Exception as e:
            resp = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": str(e)}}
    else:
        print(f"    [AGENT REQ] Unknown method: {method}")
        resp = {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    
    proc.stdin.write(json.dumps(resp, ensure_ascii=False) + "\n")
    proc.stdin.flush()


def test():
    print("=" * 55)
    print(f"E2E: '{TEST_MSG}'")
    print("=" * 55)

    print("\n[1] Start acp...")
    oc = find_opencode()
    proc = subprocess.Popen(
        [oc, "acp", "--port", "4099", "--cwd", VAULT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        bufsize=1, encoding="utf-8", errors="replace",
    )
    time.sleep(4)
    if proc.poll() is not None:
        err = proc.stderr.read()[:500]
        print(f"FAIL: exited ({proc.returncode})\n{err}")
        return False
    print(f"OK - PID={proc.pid}")

    mid = [0]
    def send(method, params=None):
        mid[0] += 1
        rpc = {"jsonrpc": "2.0", "id": mid[0], "method": method, "params": params or {}}
        proc.stdin.write(json.dumps(rpc, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        return mid[0]

    # 2. initialize — 必须带 clientInfo + clientCapabilities（参考 routa/onyx）
    print("\n[2] initialize (with clientInfo)...")
    send("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": True, "writeTextFile": True},
            "terminal": True,
        },
        "clientInfo": {
            "name": "clawbot-e2e-test",
            "title": "ClawBot E2E Test",
            "version": "1.0.0",
        },
    })
    # 读取所有响应（initialize 可能触发 agent 请求）
    init_results = []
    deadline = time.time() + 15
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            # 如果是 agent→client 请求，自动处理
            if "method" in msg and "id" in msg and "result" not in msg and "error" not in msg:
                print(f"    Agent request: {msg.get('method')}")
                handle_agent_request(proc, msg)
                continue
            init_results.append(msg)
            # 收到 initialize 响应就停止
            if msg.get("id") == 1:
                break
        except json.JSONDecodeError:
            pass

    if not init_results:
        print("FAIL: no response from initialize")
        proc.kill(); return False
    init_resp = init_results[0] if init_results else {}
    if "error" in init_resp:
        print(f"FAIL: {init_resp['error']}")
        proc.kill(); return False
    print(f"OK - {json.dumps(init_resp.get('result',{}), ensure_ascii=False)[:200]}")

    # 3. session/new
    print("\n[3] session/new...")
    send("session/new", {"cwd": VAULT, "mcpServers": []})
    
    sid = ""
    session_results = []
    deadline = time.time() + 60
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            # Agent request — 自动处理
            if "method" in msg and "id" in msg and "result" not in msg and "error" not in msg:
                print(f"    Agent request: {msg.get('method')}")
                handle_agent_request(proc, msg)
                continue
            session_results.append(msg)
            # 提取 sessionId
            if not sid:
                res = msg.get("result", {})
                sid = res.get("sessionId", "") or res.get("id", "")
            if sid and msg.get("id") == 2:
                break
        except json.JSONDecodeError:
            pass

    if not sid:
        # 尝试从所有结果中找
        for r in session_results:
            res = r.get("result", {})
            sid = res.get("sessionId", "") or res.get("id", "")
            if sid: break
    
    if not sid:
        print(f"FAIL: no sessionId")
        for r in session_results[:5]:
            print(f"    {json.dumps(r, ensure_ascii=False)[:200]}")
        proc.kill(); return False
    print(f"OK - session={sid}")

    # 4. session/prompt — 注意：用 'prompt' 字段！
    print(f"\n[4] prompt: '{TEST_MSG}'...")
    prompt_text = (
        f"你是生活日志助手。请执行以下操作：\n"
        f"1. 用 Python pathlib 写入文件 {VAULT}/2-Areas/习惯养成/生活日志/YYYY/MM/YYYY-MM-DD.md\n"
        f"2. 格式：- HH:MM 体重 68.5kg，早饭前称的\n"
        f"3. 用 datetime.now() 获取当前时间\n"
        f"4. 回复：已记录：体重 68.5kg"
    )
    # 参考 routa: 用 'prompt' 而不是 'parts'
    send("session/prompt", {
        "sessionId": sid,
        "prompt": [{"type": "text", "text": prompt_text}],
    })

    print("   Waiting for AI (max 3 min)...")
    all_results = []
    all_text = []
    deadline = time.time() + 180
    last_activity = time.time()
    
    while time.time() < deadline:
        if proc.poll() is not None:
            # 读最后一行
            line = proc.stdout.readline()
            if line and line.strip():
                try:
                    msg = json.loads(line.strip())
                    all_results.append(msg)
                except: pass
            break
        
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        
        # Agent request — 自动处理
        if "method" in msg and "id" in msg and "result" not in msg and "error" not in msg:
            print(f"    Agent request: {msg.get('method')}")
            handle_agent_request(proc, msg)
            last_activity = time.time()
            continue
        
        all_results.append(msg)
        last_activity = time.time()
        
        # 提取文本
        if "result" in msg:
            res = msg["result"]
            # stopReason=end_turn 表示对话结束
            if res.get("stopReason") == "end_turn" or res.get("stopReason") == "stop":
                print("    [STOP] end_turn received")
                break
            # 标准文本 parts
            for p in res.get("parts", []):
                if isinstance(p, dict) and p.get("type") == "text":
                    all_text.append(p.get("text", ""))
            # info 结构
            info = res.get("info", {})
            if isinstance(info, dict):
                for p in info.get("parts", []):
                    if isinstance(p, dict) and p.get("type") == "text":
                        all_text.append(p.get("text", ""))
            # 直接字段
            for k in ["text", "content", "reply"]:
                v = res.get(k)
                if isinstance(v, str) and v.strip():
                    all_text.append(v)
        
        # session/update 通知
        if msg.get("method") == "session/update":
            update = msg.get("params", {}).get("update", {})
            su = update.get("sessionUpdate", "")
            if su == "text_delta":
                all_text.append(update.get("textDelta", ""))
            elif su == "agent_thought_chunk":
                # AI 流式思考/回复，文本在 update.content.text 中
                content = update.get("content", {})
                if isinstance(content, dict) and content.get("type") == "text":
                    all_text.append(content.get("text", ""))
            elif su == "tool_call":
                print(f"    [TOOL] {update.get('toolName', '?')}: {json.dumps(update.get('toolInput',{}), ensure_ascii=False)[:100]}")
            elif su == "end_turn":
                print("    [UPDATE] end_turn")
                break
        
        # 空闲超时
        if time.time() - last_activity > 30:
            print("    [TIMEOUT] 30s idle")
            break

    # 5. 汇总结果
    final = " ".join(all_text).strip()
    print(f"\n[5] Response ({len(all_results)} events, {len(final)} chars):")
    print(f"    Text: {final[:500] or '(empty)'}")
    for i, r in enumerate(all_results[:5]):
        print(f"    [{i}] {json.dumps(r, ensure_ascii=False)[:250]}")
    if len(all_results) > 5:
        print(f"    ... +{len(all_results)-5} more")

    # 6. 检查 vault 文件
    print(f"\n[6] Vault check...")
    from datetime import datetime
    n = datetime.now()
    log_path = os.path.join(VAULT, "2-Areas", "习惯养成", "生活日志",
                           str(n.year), f"{n.month:02d}", f"{n:%Y-%m-%d}.md")
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as f:
            content = f.read()
        has_685 = "68.5" in content
        print(f"    File: {log_path}")
        print(f"    Contains '68.5': {has_685}")
        print(f"    Size: {len(content)} chars")
        for line in content.split('\n')[-10:]:
            print(f"    | {line}")
    else:
        # 搜索可能的文件位置
        life_log_dir = os.path.join(VAULT, "2-Areas", "习惯养成", "生活日志")
        if os.path.exists(life_log_dir):
            print(f"    Directory exists: {life_log_dir}")
            found = False
            for root, dirs, files in os.walk(life_log_dir):
                for f in files:
                    if f.endswith('.md'):
                        fp = os.path.join(root, f)
                        c = open(fp, encoding="utf-8", errors="replace").read()
                        if "68.5" in c:
                            print(f"    FOUND: {fp} (contains '68.5'!)")
                            for ln in c.split('\n')[-5:]:
                                print(f"    | {ln}")
                            found = True
            if not found:
                for root, dirs, files in os.walk(life_log_dir):
                    for f in files:
                        if f.endswith('.md') and n.strftime("%Y-%m-%d") in f:
                            fp = os.path.join(root, f)
                            print(f"    Today's file (no 68.5): {fp}")
        else:
            print(f"    Directory NOT FOUND: {life_log_dir}")

    # 清理
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except:
        proc.kill()

    ok = bool(final) and len(final) > 2
    print(f"\n{'='*55}")
    print(f"{'PASS' if ok else 'FAIL'}")
    print(f"{'='*55}")
    return ok


if __name__ == "__main__":
    test()