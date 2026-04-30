"""快速测试 OpenCode ACP 连通性"""
import subprocess
import json
import time
import sys

def test_acp():
    print("[TEST] Starting opencode acp...")
    import os
    opencode_path = os.path.expandvars(r"%NODIST_PREFIX%\bin\opencode.cmd")
    if not os.path.exists(opencode_path):
        # fallback
        opencode_path = r"E:\Program Files (x86)\Nodist\bin\opencode.cmd"
    proc = subprocess.Popen(
        [opencode_path, "acp", "--port", "4098"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    
    time.sleep(3)  # 等启动
    
    if proc.poll() is not None:
        stderr = proc.stderr.read()
        print(f"[TEST] [FAIL] ACP process exited (code={proc.returncode})")
        print(f"[TEST] stderr:\n{stderr[:500]}")
        return False
    
    print(f"[TEST] [OK] ACP process running (PID={proc.pid})")
    
    # 发送 initialize
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    print("[TEST] Sent initialize request, waiting...")
    
    # 读响应（等最多 60 秒）
    start = time.time()
    while time.time() - start < 60:
        line = proc.stdout.readline()
        if line:
            try:
                resp = json.loads(line)
                print(f"[TEST] [OK] Received response: {json.dumps(resp, ensure_ascii=False)[:300]}")
                proc.terminate()
                return True
            except json.JSONDecodeError:
                print(f"[TEST] Non-JSON output: {line[:200]}")
        if proc.poll() is not None:
            break
    
    stderr = proc.stderr.read()
    print(f"[TEST] stderr:\n{stderr[:500]}")
    proc.kill()
    return False

if __name__ == "__main__":
    ok = test_acp()
    print(f"\n[TEST] Result: {'PASS' if ok else 'FAIL'}")
