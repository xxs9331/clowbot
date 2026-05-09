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


def collect_config_errors(cfg: dict) -> list[str]:
    """收集配置错误；供单测断言。通过则返回空列表。"""
    errors: list[str] = []
    if not isinstance(cfg, dict):
        return ["config root must be a mapping"]

    vault = cfg.get("vault")
    if not isinstance(vault, dict):
        errors.append("vault must be a mapping")
    else:
        for key in ("root", "daily_log_dir", "diary_dir", "project_dir", "task_dir"):
            if not str(vault.get(key) or "").strip():
                errors.append(f"vault.{key} is required and must be non-empty")

    tl = cfg.get("timeline")
    if tl is None:
        errors.append("timeline section is required")
    elif not isinstance(tl, dict):
        errors.append("timeline must be a mapping")
    else:
        str_keys = (
            "root_dir",
            "timeline_dir",
            "append_separator",
            "project_overview_path",
            "state_dir",
        )
        for key in str_keys:
            if not str(tl.get(key) or "").strip():
                errors.append(f"timeline.{key} is required and must be non-empty")
        if "checkin_enabled" not in tl:
            errors.append("timeline.checkin_enabled is required (true or false)")
        try:
            sm = int(tl.get("slot_minutes"))
        except (TypeError, ValueError):
            errors.append("timeline.slot_minutes must be an integer")
        else:
            if sm != 30:
                errors.append("timeline.slot_minutes must be 30 (48-slot format)")
        try:
            float(tl.get("checkin_ai_timeout_sec"))
        except (TypeError, ValueError):
            errors.append("timeline.checkin_ai_timeout_sec must be a number")
        poll = tl.get("checkin_poll_interval_sec", None)
        if poll is not None and poll != "":
            try:
                pv = float(poll)
            except (TypeError, ValueError):
                errors.append("timeline.checkin_poll_interval_sec must be a number")
            else:
                if pv < 0:
                    errors.append("timeline.checkin_poll_interval_sec must be >= 0")
                elif 0 < pv < 10:
                    errors.append("timeline.checkin_poll_interval_sec if >0 must be >= 10")
        write_policy = str(tl.get("write_policy") or "whitelist_only").strip()
        if write_policy and write_policy != "whitelist_only":
            errors.append("timeline.write_policy must be whitelist_only")
        cwi = tl.get("checkin_write_if_filled", False)
        if not isinstance(cwi, bool):
            errors.append("timeline.checkin_write_if_filled must be true or false")
        cbp = tl.get("checkin_meta_bypass_phrases", None)
        if cbp is not None:
            if not isinstance(cbp, list):
                errors.append("timeline.checkin_meta_bypass_phrases must be a list of strings")
            else:
                if len(cbp) > 64:
                    errors.append("timeline.checkin_meta_bypass_phrases must have at most 64 entries")
                for i, item in enumerate(cbp):
                    if not isinstance(item, str) or not item.strip():
                        errors.append(
                            f"timeline.checkin_meta_bypass_phrases[{i}] must be a non-empty string"
                        )
        if "compact_enabled" not in tl:
            errors.append("timeline.compact_enabled is required (true or false)")
        elif not isinstance(tl.get("compact_enabled"), bool):
            errors.append("timeline.compact_enabled must be true or false")
        if "compact_max_chars" not in tl:
            errors.append("timeline.compact_max_chars is required")
        else:
            try:
                cmc = int(tl.get("compact_max_chars"))
            except (TypeError, ValueError):
                errors.append("timeline.compact_max_chars must be an integer")
            else:
                if cmc < 10 or cmc > 200:
                    errors.append("timeline.compact_max_chars must be between 10 and 200 inclusive")

    graph = cfg.get("graph")
    if graph is not None:
        if not isinstance(graph, dict):
            errors.append("graph must be a mapping")
        else:
            enabled = graph.get("enabled", False)
            shadow_mode = graph.get("shadow_mode", False)
            if not isinstance(enabled, bool):
                errors.append("graph.enabled must be true or false")
                enabled = False
            if not isinstance(shadow_mode, bool):
                errors.append("graph.shadow_mode must be true or false")
                shadow_mode = False
            rate = graph.get("rollout_rate", 0.0)
            try:
                rv = float(rate)
            except (TypeError, ValueError):
                errors.append("graph.rollout_rate must be a number between 0 and 1")
                rv = 0.0
            else:
                if rv < 0 or rv > 1:
                    errors.append("graph.rollout_rate must be between 0 and 1")
            users = graph.get("rollout_users", [])
            if not isinstance(users, list):
                errors.append("graph.rollout_users must be a list")
            if enabled and shadow_mode:
                shadow_log_dir = str(graph.get("shadow_log_dir") or "").strip()
                if not shadow_log_dir:
                    errors.append("graph.shadow_log_dir is required when graph.enabled=true and graph.shadow_mode=true")
            if not enabled and shadow_mode:
                errors.append("graph.shadow_mode must be false when graph.enabled=false")

    return errors


def validate_config(cfg: dict) -> None:
    """严格校验：失败则打印并 ``sys.exit(1)``。"""
    errors = collect_config_errors(cfg)
    if errors:
        for e in errors:
            print(f"CONFIG ERROR: {e}")
        sys.exit(1)


def load_config() -> dict:
    path = PACKAGE_ROOT / "config.yaml"
    if not path.exists():
        print("ERROR: config.yaml not found. Copy from config.example.yaml")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    validate_config(cfg)
    return cfg
