"""管理本机私密记录、来源导入和不使用模型的关心提醒。"""

import datetime as dt
import hashlib
import json
import logging
from pathlib import Path
import subprocess
import sys

from second_proprio.config import atomic_json, resolve_path
from second_proprio import review_sources


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


def import_chat(root: Path, config: dict, source: Path) -> Path:
    """将用户选择的聊天导出规范化到应用私密目录，保留消息原日期。

    Args:
        root: 项目根目录。
        config: 已校验配置。
        source: 用户明确提供的 JSON 文件。

    Returns:
        应用私密目录中的规范化快照路径。

    Raises:
        ValueError: 没有可用的带时间消息或 JSON 格式不支持。
    """
    messages, warnings = review_sources.read_file(source)
    if not messages:
        raise ValueError(
            "没有可用的带时区/时间戳聊天文本；请提供 conversations.json 或 messages 快照")
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()[:16]
    target = review_sources.import_directory(root,
                                             config) / f"chat-{digest}.json"
    atomic_json(target, {
        "source": source.name,
        "messages": messages,
        "warnings": warnings
    })
    return target


def review_context(root: Path, config: dict, day: dt.date) -> dict:
    """读取配置目录当天证据，保留覆盖范围与读取问题。"""
    evidence = review_sources.scan(root, config, day)
    # 隔离工作区快照附带父进程核验的原始来源位置与读取问题。
    snapshot = resolve_path(root,
                            config["paths"]["state"]) / "review-source.json"
    if snapshot.exists():
        origin = json.loads(snapshot.read_text("utf-8"))
        fingerprint = origin.get("source_fingerprint")
        if fingerprint and fingerprint != evidence["source_fingerprint"]:
            raise ValueError("复盘快照与已验证的来源指纹不一致，请重新准备任务")
        for key in ("sources", "errors", "warnings", "directory",
                    "source_kinds"):
            evidence[key] = origin.get(key, evidence[key])
    output = resolve_path(root,
                          config["paths"]["private"]) / "daily" / f"{day}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = "请告诉我今天的记录在哪里。"
    if evidence["errors"]:
        issues = "；".join(f"{item['source']}：{item['message']}"
                          for item in evidence["errors"][:3])
        summary += " 来源读取问题：" + issues
    if not evidence["messages"]:
        if not output.exists():
            output.write_text(f"# {day} 每日复盘\n\n状态：等待来源。\n\n{summary}\n",
                              "utf-8")
        return {
            "status": "waiting_source",
            "output": str(output.relative_to(root)),
            "summary": summary,
            "source_errors": evidence["errors"],
            "source_fingerprint": evidence["source_fingerprint"]
        }
    context = resolve_path(
        root, config["paths"]["private"]) / "review-context" / f"{day}.json"
    atomic_json(context, evidence)
    return {
        "status": "ready",
        "context": str(context.relative_to(root)),
        "output": str(output.relative_to(root)),
        "messages": len(evidence["messages"]),
        "source_fingerprint": evidence["source_fingerprint"],
        "source_errors": evidence["errors"]
    }
