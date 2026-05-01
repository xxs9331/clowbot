"""配置与推理日志路径（与 bot 根目录绑定）"""

import sys
from datetime import datetime
from pathlib import Path

import yaml

# 项目根目录（.clawbot/）
PACKAGE_ROOT = Path(__file__).resolve().parent

LOG_DIR = PACKAGE_ROOT / "logs"
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


def load_config() -> dict:
    path = PACKAGE_ROOT / "config.yaml"
    if not path.exists():
        print("ERROR: config.yaml not found. Copy from config.example.yaml")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
