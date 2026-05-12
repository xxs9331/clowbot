"""每日晨报调度：天气 + 本地上下文 + ACP 生成 + 后台 immediate 事件触达。"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp

from config import PACKAGE_ROOT
from utils.log_sync import get_log_path
from utils.section_reader import extract_section_text
from utils.timeline_sync import timeline_path


PROMPTS_DIR = PACKAGE_ROOT / "prompts"
_BRIEFING_FILE = "daily_briefing.md"
_API_KEY_ENV = "QWEATHER_API_KEY"

_DEFAULT_BRIEFING_PROMPTS = """你是中文个人助理，负责每天早上生成晨间简报。

输出约束：
- 只输出 1 条微信消息，长度 200~400 字
- 不要 markdown，不要 JSON
- 结构：问候 -> 天气 -> 穿衣建议 -> 待办/提醒 -> 结束语

当前时间：{time}
日期：{date}

【今日天气】
温度：{temp_low}°C ~ {temp_high}°C
白天：{text_day}，夜间：{text_night}
风力：{wind}
降水：{precip}mm
湿度：{humidity}%
紫外线指数：{uv_index}
日出：{sunrise}，日落：{sunset}

【穿衣建议】
{indices_text}

【生活指数】
{all_indices_text}

【今日待办与提醒】
{tasks_and_reminders}

【近期时间轴】
{timeline_recent}

【今日日记尾部】
{diary_tail}

请生成今日晨间简报：

---

你是中文个人助理，负责每天早上生成晨间简报。

今天天气 API 暂时不可用，请根据以下信息生成简要问候。
输出要求：50~100字，自然中文，不要 markdown，不要 JSON。

当前时间：{time}
日期：{date}

【今日待办与提醒】
{tasks_and_reminders}

【近期时间轴】
{timeline_recent}

【今日日记尾部】
{diary_tail}

请生成一条简短晨间问候：
"""


def _load_briefing_prompts() -> tuple[str, str]:
    """读取晨报模板，按 `---` 分段，失败时返回内置默认模板。"""
    path = PROMPTS_DIR / _BRIEFING_FILE
    try:
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            parts = content.split("\n---\n", 1)
            if len(parts) == 2:
                return parts[0].strip(), parts[1].strip()
    except OSError:
        pass
    parts = _DEFAULT_BRIEFING_PROMPTS.split("\n---\n", 1)
    return parts[0].strip(), parts[1].strip()


def _sleep_seconds_until(hour: int, minute: int, now: datetime | None = None) -> float:
    """计算离目标时刻的秒数。若今天已过，则等待到明天。"""
    now = now or datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max((target - now).total_seconds(), 1.0)


async def _sleep_until(hour: int, minute: int) -> None:
    """等待到指定时刻（本地时间）。"""
    await asyncio.sleep(_sleep_seconds_until(hour, minute))


def _indices_param(indices: list[int] | None) -> str:
    vals = indices or [3, 1, 5, 8, 9]
    clean: list[str] = []
    for x in vals:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        clean.append(str(n))
    return ",".join(clean) if clean else "3,1,5,8,9"


def _error_weather(message: str) -> dict:
    now = datetime.now()
    return {
        "error": message,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "indices": [],
    }


async def _fetch_weather(cfg: dict) -> dict:
    """调用和风天气 API 获取当天天气与指数。异常时返回 `error` 字段。"""
    bc = cfg.get("briefing") or {}
    wc = bc.get("weather") or {}
    api_key = str(os.getenv(_API_KEY_ENV, "") or "").strip()
    if not api_key:
        return _error_weather("missing_api_key")

    host = str(wc.get("api_host") or "").strip().rstrip("/")
    location = str(wc.get("location") or "").strip()
    if not host or not location:
        return _error_weather("missing_weather_config")

    weather_url = f"{host}/v7/weather/3d"
    indices_url = f"{host}/v7/indices/1d"
    timeout = aiohttp.ClientTimeout(total=10)
    now = datetime.now()

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                weather_url,
                params={"location": location, "key": api_key},
            ) as resp:
                if resp.status != 200:
                    return _error_weather(f"weather_http_{resp.status}")
                weather_obj = await resp.json(content_type=None)
            if str(weather_obj.get("code") or "200") != "200":
                return _error_weather(f"weather_code_{weather_obj.get('code')}")

            async with session.get(
                indices_url,
                params={
                    "type": _indices_param(wc.get("indices")),
                    "location": location,
                    "key": api_key,
                },
            ) as resp:
                if resp.status != 200:
                    return _error_weather(f"indices_http_{resp.status}")
                indices_obj = await resp.json(content_type=None)
            if str(indices_obj.get("code") or "200") != "200":
                return _error_weather(f"indices_code_{indices_obj.get('code')}")
    except asyncio.TimeoutError:
        return _error_weather("timeout")
    except Exception as e:  # noqa: BLE001
        return _error_weather(f"request_error:{str(e)[:120]}")

    daily = ((weather_obj.get("daily") or [{}])[0]) or {}
    idx_raw = indices_obj.get("daily") or []
    indices = []
    for item in idx_raw:
        if not isinstance(item, dict):
            continue
        indices.append(
            {
                "name": str(item.get("name") or "").strip(),
                "category": str(item.get("category") or "").strip(),
                "text": str(item.get("text") or "").strip(),
            }
        )

    return {
        "date": str(daily.get("fxDate") or now.strftime("%Y-%m-%d")),
        "time": now.strftime("%H:%M"),
        "temp_high": str(daily.get("tempMax") or ""),
        "temp_low": str(daily.get("tempMin") or ""),
        "text_day": str(daily.get("textDay") or ""),
        "text_night": str(daily.get("textNight") or ""),
        "wind": f"{daily.get('windDirDay') or ''} {daily.get('windScaleDay') or ''}".strip(),
        "precip": str(daily.get("precip") or ""),
        "humidity": str(daily.get("humidity") or ""),
        "uv_index": str(daily.get("uvIndex") or ""),
        "sunrise": str(daily.get("sunrise") or ""),
        "sunset": str(daily.get("sunset") or ""),
        "indices": indices,
    }


def _snip_file(path: Path, max_chars: int) -> str:
    if not path.exists():
        return ""
    try:
        s = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return s if len(s) <= max_chars else s[-max_chars:]


def _diary_path(handler, today: datetime) -> Path:
    vault = handler.cfg.get("vault") or {}
    root = Path(str(vault.get("root") or "").strip()).resolve()
    rel = str(vault.get("diary_dir") or "").strip()
    y = today.strftime("%Y")
    m = today.strftime("%m")
    d = today.strftime("%Y-%m-%d")
    return root / rel.replace("\\", "/") / y / m / f"{d}.md"


def _timeline_recent(cfg: dict, today: datetime) -> str:
    try:
        tp = timeline_path(cfg, today)
    except Exception:
        return ""
    if not tp.exists():
        return ""
    try:
        lines = tp.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    out: list[str] = []
    for line in reversed(lines):
        m = re.match(r"^(\d{2}:\d{2})\s+(.+)$", line.strip())
        if not m:
            continue
        body = m.group(2).strip()
        if not body:
            continue
        out.append(f"{m.group(1)} {body}")
        if len(out) >= 3:
            break
    out.reverse()
    return "\n".join(out)


def _read_vault_context(handler) -> dict:
    """读取当日待办/提醒/日记/时间轴片段。"""
    cfg = handler.cfg
    vault = cfg.get("vault") or {}
    root = Path(str(vault.get("root") or "").strip()).resolve()
    today = datetime.now()

    log_path = get_log_path(str(root), str(vault.get("daily_log_dir") or ""), today)
    log_text = _snip_file(log_path, 8000)
    tasks = extract_section_text(log_text, "📋", "待办", include_heading=True)
    reminders = extract_section_text(log_text, "⏰", "提醒", include_heading=True)
    tasks_and_reminders = "\n\n".join(x for x in (tasks, reminders) if x.strip())

    return {
        "tasks": tasks or "暂无待办",
        "reminders": reminders or "暂无提醒",
        "tasks_and_reminders": tasks_and_reminders or "暂无待办与提醒",
        "diary_tail": _snip_file(_diary_path(handler, today), 1600) or "今日日记暂无内容",
        "timeline_recent": _timeline_recent(cfg, today) or "近期时间轴暂无内容",
    }


def _format_indices(indices: list[dict]) -> tuple[str, str]:
    if not indices:
        return "暂无", "暂无"
    all_lines: list[str] = []
    wear_line = ""
    for item in indices:
        name = str(item.get("name") or "").strip()
        category = str(item.get("category") or "").strip()
        text = str(item.get("text") or "").strip()
        line = f"{name}（{category}）：{text}".strip("：")
        all_lines.append(line)
        if ("穿衣" in name) and not wear_line:
            wear_line = line
    return wear_line or all_lines[0], "\n".join(all_lines)


def _build_prompt(weather: dict, context: dict, template: str) -> str:
    """填充模板。"""
    idx_text, all_idx = _format_indices(list(weather.get("indices") or []))
    data = {
        "date": str(weather.get("date") or datetime.now().strftime("%Y-%m-%d")),
        "time": str(weather.get("time") or datetime.now().strftime("%H:%M")),
        "temp_high": str(weather.get("temp_high") or "未知"),
        "temp_low": str(weather.get("temp_low") or "未知"),
        "text_day": str(weather.get("text_day") or "未知"),
        "text_night": str(weather.get("text_night") or "未知"),
        "wind": str(weather.get("wind") or "未知"),
        "precip": str(weather.get("precip") or "未知"),
        "humidity": str(weather.get("humidity") or "未知"),
        "uv_index": str(weather.get("uv_index") or "未知"),
        "sunrise": str(weather.get("sunrise") or "未知"),
        "sunset": str(weather.get("sunset") or "未知"),
        "indices_text": idx_text,
        "all_indices_text": all_idx,
        "tasks_and_reminders": str(context.get("tasks_and_reminders") or "暂无待办与提醒"),
        "timeline_recent": str(context.get("timeline_recent") or "近期时间轴暂无内容"),
        "diary_tail": str(context.get("diary_tail") or "今日日记暂无内容"),
    }
    return template.format(**data)


def _fallback_briefing(weather: dict, context: dict) -> str:
    """模型不可用时的兜底晨报。"""
    return (
        f"早上好，今天是 {weather.get('date') or datetime.now().strftime('%Y-%m-%d')}。"
        "天气接口暂不可用，建议出门前看下实时天气。"
        f"\n\n{context.get('tasks_and_reminders') or '暂无待办与提醒'}"
    )


async def _generate_briefing(handler, weather: dict, context: dict) -> str:
    ok_tmpl, fallback_tmpl = _load_briefing_prompts()
    use_fallback_prompt = bool(weather.get("error"))
    prompt = _build_prompt(weather, context, fallback_tmpl if use_fallback_prompt else ok_tmpl)
    sid = getattr(handler, "unified_session_id", "") or getattr(handler, "session_id", "")
    if not sid or not getattr(handler, "acp", None):
        return _fallback_briefing(weather, context)
    try:
        reply, _ = await handler.acp.prompt(sid, prompt, trace_tag="daily_briefing")
        body = (reply or "").strip()
        if not body:
            raise ValueError("empty briefing reply")
        return body
    except Exception as e:  # noqa: BLE001
        print(f"[Bot] briefing LLM 失败: {e}")
        return _fallback_briefing(weather, context)


async def briefing_loop(handler):
    """每天固定时间生成晨报，并通过后台 immediate 事件触达。"""
    while True:
        try:
            cfg = handler.cfg
            bc = cfg.get("briefing") or {}
            if not bool(bc.get("enabled", False)):
                await asyncio.sleep(300)
                continue

            try:
                push_hour = int(bc.get("push_hour", 7))
                push_minute = int(bc.get("push_minute", 0))
            except (TypeError, ValueError):
                push_hour, push_minute = 7, 0

            await _sleep_until(push_hour, push_minute)

            weather = await _fetch_weather(cfg)
            context = _read_vault_context(handler)
            briefing_text = await _generate_briefing(handler, weather, context)

            event_id = handler.add_background_event(
                kind="daily_briefing",
                summary=briefing_text,
                priority="immediate",
                source="scheduler.briefing",
            )
            if event_id:
                print(f"[Bot] briefing 已投递事件: {event_id}")
            else:
                print("[Bot] briefing 未投递：unified_chat_mode 可能未开启")
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            print(f"[Bot] briefing 循环错误: {e}")
            await asyncio.sleep(5)
