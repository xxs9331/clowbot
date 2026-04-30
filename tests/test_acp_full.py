"""测试 OpenCode ACP 完整流程：initialize → session/new → prompt"""
import subprocess
import json
import time
import os
import sys

def find_opencode():
    for path in [
        os.path.expandvars(r"%NODIST_PREFIX%\bin\opencode.cmd"),
        r"E:\Program Files (x86)\Nodist\bin\opencode.cmd",
        r"opencode",
    ]:
        if path == "opencode" or os.path.exists(path):
            return path
    raise FileNotFoundError("opencode not found")


def test_acp_full():
    print("[TEST] Starting opencode acp...")
    opencode_path = find_opencode()
    proc = subprocess.Popen(
        [opencode_path, "acp", "--port", "4098"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        bufsize=1,
        encoding="utf-8", errors="replace",
    )
    time.sleep(3)
    
    if proc.poll() is not None:
        print(f"[TEST] FAIL - process exited ({proc.returncode})")
        print(proc.stderr.read()[:500])
        return False
    print(f"[TEST] OK - ACP running (PID={proc.pid})")

    msg_id = 0
    def send(method, params=None):
        nonlocal msg_id
        msg_id += 1
        rpc = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        payload = json.dumps(rpc, ensure_ascii=False) + "\n"
        proc.stdin.write(payload)
        proc.stdin.flush()
        return msg_id

    def recv(timeout=120):
        start = time.time()
        while time.time() - start < timeout:
            line = proc.stdout.readline()
            if line:
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    pass
            if proc.poll() is not None:
                break
        return None

    # Step 1: initialize
    print("[TEST] Step 1: initialize...")
    send("initialize", {"protocolVersion": 1})
    resp = recv()
    if not resp or "error" in resp:
        print(f"[TEST] FAIL - initialize: {json.dumps(resp, ensure_ascii=False)[:300]}")
        proc.kill()
        return False
    print(f"[TEST] OK - initialize")

    # Step 2: session/new
    print("[TEST] Step 2: session/new...")
    send("session/new", {
        "cwd": r"D:\Lenovo\Documents\世界树\世界树",
        "mcpServers": [],
        "parts": [{"type": "text", "text": "Reply with 'ACP connected OK' and nothing else."}]
    })
    resp = recv(timeout=60)
    if not resp:
        print("[TEST] FAIL - session/new timed out")
        proc.kill()
        return False
    print(f"[TEST] OK - session/new: {json.dumps(resp, ensure_ascii=False)[:400]}")
    
    session_id = resp.get("result", {}).get("sessionId", "")
    if not session_id:
        # try finding sessionId elsewhere
        sid = resp.get("result", {})
        session_id = sid.get("id", sid.get("sessionId", ""))
    
    # Extract reply text
    parts = resp.get("result", {}).get("parts", [])
    reply_text = ""
    for p in parts:
        if p.get("type") == "text":
            reply_text += p.get("text", "")
    
    print(f"[TEST] Reply text: {reply_text[:200]}")
    print(f"[TEST] Session ID: {session_id[:30] if session_id else 'NOT FOUND'}")

    proc.terminate()
    try: proc.wait(timeout=5)
    except: proc.kill()
    
    return session_id != "" and reply_text != ""


if __name__ == "__main__":
    ok = test_acp_full()
    print(f"\n=== Result: {'PASS' if ok else 'FAIL'} ===")
