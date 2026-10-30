"""管理本机私密记录、来源导入和不使用模型的关心提醒。"""

import datetime as dt
import hashlib
import json
import logging
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

from second_proprio.config import atomic_json, resolve_path


def notify(config: dict, title: str, message: str) -> bool:
    """尝试提交本机通知，并记录不含通知正文的失败诊断。

    Args:
        config: 包含 runtime.notify 开关的配置。
        title: 通知标题，仅作为子进程参数传递，不写入日志。
        message: 通知正文，仅作为子进程参数传递，不写入日志。

    Returns:
        osascript 返回零时为 True，只表示通知命令已被接受，不保证通知
        实际显示或送达；禁用、不支持的平台或命令失败时为 False。
    """
    if not config["runtime"]["notify"] or sys.platform != "darwin":
        return False
    logger = logging.getLogger(__name__)
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
    script = (
        'on run argv\n'
        'display notification (item 2 of argv) with title (item 1 of argv)\n'
        'end run')
    try:
        result = subprocess.run(["osascript", "-e", script, title, message],
                                check=False,
                                capture_output=True,
                                timeout=15)
    except subprocess.TimeoutExpired:
        logger.warning(
            "event=notification_command_timeout timeout_seconds=15 "
            "at=%s", timestamp)
        return False
    except OSError as error:
        logger.warning("event=notification_command_os_error errno=%s at=%s",
                       error.errno, timestamp)
        return False
    if result.returncode:
        # 原始标准错误可能复述通知参数，因此只记录退出码和是否存在诊断。
        logger.warning(
            "event=notification_command_failed exit_code=%s "
            "stderr_present=%s at=%s", result.returncode, bool(result.stderr),
            timestamp)
        return False
    return True


def relationship_days(config: dict, day: dt.date) -> int:
    """按配置时区的日期差计算纪念日，不受小时与夏令时影响。"""
    relationship = config["relationship"]
    anchor = dt.date.fromisoformat(relationship["anchor_date"])
    return relationship["anchor_days"] + (day - anchor).days


def save_mood(root: Path,
              config: dict,
              day: dt.date,
              mood: int | None,
              note: str = "",
              action: str = "") -> Path:
    """保存用户自己填写的心情分数和关心行动，保留其他字段。"""
    if mood is not None and not 1 <= mood <= 5:
        raise ValueError("心情分数必须在 1 到 5 之间")
    path = resolve_path(root, config["paths"]["private"])
    path = path / "relationship" / f"{day}.json"
    value = json.loads(path.read_text("utf-8")) if path.exists() else {}
    value.setdefault("mood", None)
    value.setdefault("note", "")
    value.setdefault("actions", [])
    if mood is not None:
        value["mood"] = mood
    if note:
        value["note"] = note
    if action:
        value["actions"].append(action)
    atomic_json(path, value)
    return path


def care(root: Path, config: dict, now: dt.datetime) -> dict:
    """渲染当天提醒；无当日填写就显示待填写。"""
    day = now.date()
    folder = resolve_path(root, config["paths"]["private"]) / "relationship"
    path = folder / f"{day}.json"
    value = json.loads(path.read_text("utf-8")) if path.exists() else {}
    mood = value.get("mood")
    mood_text = f"{mood}/5（由你填写）" if mood is not None else "待填写"
    prompts = config["relationship"]["prompts"]
    times = sorted(config["schedules"]["care"]["times"])
    index = max(0, sum(clock <= now.strftime("%H:%M") for clock in times) - 1)
    prompt = prompts[index % len(prompts)]
    days = relationship_days(config, day)
    name = config["relationship"]["name"]
    actions = value.get("actions") or ["尚未记录"]
    text = (f"# {day} · 关心 {name}\n\n"
            f"今天是在一起的 **第 {days} 天**。\n\n"
            f"心情指数：{mood_text}\n\n{prompt}\n\n"
            f"你的备注：{value.get('note') or '未填写'}\n\n"
            "## 今天已经做过\n\n" + "\n".join(f"- {a}" for a in actions) + "\n")
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / f"{day}.md"
    output.write_text(text, "utf-8")
    notify(config, f"关心 {name} · 第 {days} 天", prompt)
    return {"artifacts": [str(output.relative_to(root))], "summary": prompt}


def _timestamp(value: object) -> str | None:
    """将导出时间转换为有时区 ISO 字符串。"""
    try:
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.isoformat() if parsed.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def import_chat(root: Path, config: dict, source: Path) -> Path:
    """导入快照或 ChatGPT conversations.json，只保留当前分支文本。

    Args:
        root: 项目根目录。
        config: 已校验配置。
        source: 用户明确提供的 JSON 文件。

    Returns:
        仅在本机保存的规范化记录路径。

    Raises:
        ValueError: 没有可用的带时间消息或格式不支持。
    """
    raw = json.loads(source.read_text("utf-8"))
    messages = []
    if isinstance(raw, dict) and "messages" in raw:
        messages = raw["messages"]
    else:
        conversations = raw if isinstance(raw, list) else [raw]
        for conversation in conversations:
            mapping = conversation.get("mapping", {})
            node = conversation.get("current_node")
            branch = []
            seen = set()
            while node and node in mapping and node not in seen:
                seen.add(node)
                entry = mapping[node]
                branch.append(entry)
                node = entry.get("parent")
            if not branch:
                branch = list(mapping.values())
            for entry in reversed(branch):
                message = entry.get("message") or {}
                parts = message.get("content", {}).get("parts", [])
                messages.append({
                    "timestamp": message.get("create_time"),
                    "role": message.get("author", {}).get("role"),
                    "text": "\n".join(p for p in parts if isinstance(p, str)),
                    "conversation_id": conversation.get("id", "unknown"),
                    "title": conversation.get("title", "未命名聊天"),
                })
    clean = []
    for message in messages:
        stamp = _timestamp(message.get("timestamp"))
        if stamp and message.get("role") in {"user", "assistant"}:
            if isinstance(message.get("text"), str) and message["text"].strip():
                clean.append({
                    key: message.get(key, "")
                    for key in ("role", "text", "conversation_id", "title")
                } | {"timestamp": stamp})
    if not clean:
        raise ValueError(
            "没有可用的带时区/时间戳聊天文本；请提供 conversations.json 或 messages 快照")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    target = resolve_path(root, config["review"]["source_directory"])
    target = target / f"chat-{digest}.json"
    atomic_json(target, {"source": source.name, "messages": clean})
    return target


def review_context(root: Path, config: dict, day: dt.date) -> dict:
    """提取当天证据，记录可见范围，缺来源时不编造总结。"""
    folder = resolve_path(root, config["review"]["source_directory"])
    messages = []
    sources = []
    seen = set()
    for path in sorted(folder.glob("*.json")):
        data = json.loads(path.read_text("utf-8"))
        selected = []
        for message in data.get("messages", []):
            stamp = dt.datetime.fromisoformat(message["timestamp"])
            if stamp.astimezone(ZoneInfo(config["timezone"])).date() == day:
                key = (message.get("conversation_id"), message["timestamp"],
                       message.get("role"), message.get("text"))
                if key not in seen:
                    seen.add(key)
                    selected.append(message)
        if selected:
            sources.append(str(path.relative_to(root)))
            messages.extend(selected)
    output = resolve_path(root, config["paths"]["private"])
    output = output / "daily" / f"{day}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    if not messages:
        if not output.exists():
            output.write_text(f"# {day} 每日复盘\n\n状态：等待来源。\n\n"
                              "请告诉我今天的记录在哪里。\n", "utf-8")
        return {
            "status": "waiting_source",
            "output": str(output.relative_to(root))
        }
    evidence = {"date": str(day), "sources": sources, "messages": messages}
    context = resolve_path(root, config["paths"]["private"])
    context = context / "review-context" / f"{day}.json"
    atomic_json(context, evidence)
    return {
        "status": "ready",
        "context": str(context.relative_to(root)),
        "output": str(output.relative_to(root)),
        "messages": len(messages)
    }
