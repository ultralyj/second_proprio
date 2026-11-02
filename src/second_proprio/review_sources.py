"""只读解析复盘来源，返回按日规范化证据及不包含正文的来源诊断。"""

from __future__ import annotations

from collections import OrderedDict
import datetime as dt
import hashlib
import json
from pathlib import Path
import threading
from zoneinfo import ZoneInfo

from second_proprio import config as configuration

_MAX_BYTES = 256 * 1024 * 1024
_CACHE: OrderedDict[str, tuple[tuple[int, int],
                               tuple[list[dict], list[str]]]] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def import_directory(root: Path, config: dict) -> Path:
    """返回应用拥有的私密导入目录，永不指向外部来源。"""
    return configuration.resolve_path(
        root, config["paths"]["private"]) / "chat-imports"


def _timestamp(value: object) -> str | None:
    """转换消息自身时间，拒绝无时区时间和文件修改时间替代。"""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            stamp = dt.datetime.fromtimestamp(value, dt.timezone.utc)
        else:
            stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.astimezone(
            dt.timezone.utc).isoformat() if stamp.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def normalize(data: object) -> tuple[list[dict], list[str]]:
    """解析 ChatGPT 当前分支或规范化快照，返回消息与格式警告。

    Args:
        data: 已读取的 JSON 数据，不作为执行指令。

    Returns:
        规范化文本消息和不包含聊天正文的警告。

    Raises:
        ValueError: JSON 结构不是支持的聊天格式。
    """
    raw = []
    warnings = []
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        raw = data["messages"]
    else:
        conversations = data.get("conversations") if isinstance(data,
                                                                dict) else data
        if isinstance(data, dict) and isinstance(data.get("mapping"), dict):
            conversations = [data]
        if not isinstance(conversations, list) or not all(
                isinstance(item, dict) and isinstance(item.get("mapping"), dict)
                for item in conversations):
            raise ValueError("不支持的 JSON 格式；需要 conversations.json 或 messages 快照")
        for conversation in conversations:
            mapping = conversation["mapping"]
            node = conversation.get("current_node")
            branch, seen = [], set()
            while isinstance(node,
                             str) and node in mapping and node not in seen:
                seen.add(node)
                entry = mapping[node]
                if not isinstance(entry, dict):
                    break
                branch.append(entry)
                node = entry.get("parent")
            if not branch:
                branch = [
                    item for item in mapping.values() if isinstance(item, dict)
                ]
                if branch:
                    warnings.append("导出未提供有效 current_node，使用现有节点并去重")
            for entry in reversed(branch):
                message = entry.get("message") or {}
                if not isinstance(message, dict):
                    continue
                content = message.get("content") or {}
                author = message.get("author") or {}
                parts = content.get("parts", []) if isinstance(content,
                                                               dict) else []
                if not isinstance(parts, list):
                    warnings.append("跳过一条正文片段格式异常的导出消息")
                    continue
                raw.append({
                    "timestamp":
                        message.get("create_time"),
                    "role":
                        author.get("role")
                        if isinstance(author, dict) else None,
                    "text":
                        "\n".join(
                            part for part in parts if isinstance(part, str)),
                    "conversation_id":
                        conversation.get(
                            "id", conversation.get("conversation_id", "")),
                    "title":
                        conversation.get("title", "未命名聊天"),
                })
    clean = []
    invalid = 0
    for message in raw:
        if not isinstance(message, dict):
            invalid += 1
            continue
        if message.get("role") not in ("user", "assistant"):
            continue
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        stamp = _timestamp(message.get("timestamp"))
        if not stamp:
            invalid += 1
            continue
        clean.append({
            "timestamp": stamp,
            "role": message["role"],
            "text": text,
            "conversation_id": str(message.get("conversation_id") or ""),
            "title": str(message.get("title") or "未命名聊天"),
            "source": str(message.get("source") or "ChatGPT 导出或手工快照")
        })
    if invalid:
        warnings.append(f"跳过 {invalid} 条缺少有效消息时间或结构异常的记录")
    return clean, warnings


def _read_rollout(path: Path) -> tuple[list[dict], list[str]]:
    """流式提取主会话的用户正文和最终回答，跳过工具及推理记录。"""
    messages, warnings = [], []
    session = None
    with path.open("rb") as stream:
        for number, line in enumerate(stream, 1):
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                if not line.endswith(b"\n"):
                    # 活动日志最后一行可能仍在写入，不把半行当作坏源。
                    break
                warnings.append(f"第 {number} 行 JSON 不完整或格式无效，已跳过")
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if event.get("type") == "session_meta":
                source = payload.get("source")
                parts = Path(str(payload.get("cwd", ""))).parts
                child = (payload.get("parent_thread_id") or
                         payload.get("agent_path") or
                         isinstance(source, dict) and "subagent" in source)
                background = (source == "exec" or "draft-runs" in parts or
                              "desktop-runs" in parts)
                if child or background:
                    return [], []
                if source not in ("vscode", "cli"):
                    return [], ["未识别为主交互会话，跳过该 Codex 日志"]
                session = payload
                continue
            if session is None or event.get(
                    "type") != "response_item" or payload.get(
                        "type") != "message":
                continue
            role = payload.get("role")
            phase = payload.get("phase")
            if role not in ("user", "assistant"):
                continue
            if role == "assistant" and phase != "final_answer" and not (
                    phase is None and session.get("source") == "cli"):
                continue
            content = payload.get("content")
            if not isinstance(content, list):
                continue
            expected = "input_text" if role == "user" else "output_text"
            parts = [
                part["text"]
                for part in content
                if isinstance(part, dict) and part.get("type") == expected and
                isinstance(part.get("text"), str)
            ]
            text = "\n".join(parts).strip()
            if not text or (role == "user" and
                            text.startswith("<environment_context>") and
                            text.endswith("</environment_context>")):
                continue
            if role == "user" and text.startswith(
                    "本次运行由已授权的 second proprio 后台任务调用。"):
                return [], []
            stamp = _timestamp(event.get("timestamp"))
            if stamp is None:
                warnings.append(f"第 {number} 行消息缺少有效时间，已跳过")
                continue
            messages.append({
                "timestamp": stamp,
                "role": role,
                "text": text,
                "conversation_id": str(session.get("id", path.stem)),
                "title": "Codex 本机对话",
                "source": "Codex 本机对话"
            })
    return messages, warnings


def _read_uncached(path: Path, size: int) -> tuple[list[dict], list[str]]:
    """读取单个文件的可见消息，不保存到公开目录。"""
    if path.suffix == ".jsonl":
        return _read_rollout(path)
    if size > _MAX_BYTES:
        raise ValueError("JSON 超过 256 MiB，请按日期拆分导出后读取")
    try:
        raw = json.loads(path.read_text("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("JSON 无法解析，请检查文件编码和完整性") from error
    messages, warnings = normalize(raw)
    return messages, warnings


def read_file(path: Path) -> tuple[list[dict], list[str]]:
    """只读 JSON 或 Codex JSONL，按大小与修改时间复用内存缓存。"""
    info = path.stat()
    key, signature = str(path), (info.st_size, info.st_mtime_ns)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] == signature:
            _CACHE.move_to_end(key)
            return cached[1]
    result = _read_uncached(path, info.st_size)
    with _CACHE_LOCK:
        # 同一活动日志仅保留最新版本，最多缓存 128 个文件。
        _CACHE[key] = (signature, result)
        _CACHE.move_to_end(key)
        while len(_CACHE) > 128:
            _CACHE.popitem(last=False)
    return result


def _label(root: Path, path: Path) -> str:
    """保留来源定位信息，仅在私密状态与证据中使用。"""
    return str(
        path.relative_to(root)) if path.is_relative_to(root) else str(path)


def scan(root: Path, config: dict, day: dt.date | None = None) -> dict:
    """读取配置目录与私密导入记录，按消息时间筛选并去重。

    Args:
        root: 当前工作区根目录。
        config: 已验证配置，外部目录仅用于读取。
        day: 目标日期；空值返回全部日期以检测来源更新。

    Returns:
        消息、来源、错误、日期覆盖和稳定输入指纹；调用方不得公开消息。
    """
    root = root.resolve()
    configured = configuration.resolve_review_source(root, config)
    internal = import_directory(root, config)
    folders = list(dict.fromkeys((configured, internal)))
    files, errors = [], []
    for folder in folders:
        if not folder.exists():
            if folder == configured:
                errors.append({"source": str(folder), "message": "配置的来源目录不存在"})
            continue
        if not folder.is_dir():
            errors.append({"source": str(folder), "message": "来源路径不是目录"})
            continue
        try:
            for pattern in ("*.json", "rollout-*.jsonl"):
                files.extend(path for path in folder.rglob(pattern)
                             if path.is_file() and not path.is_symlink())
        except OSError:
            errors.append({"source": str(folder), "message": "没有读取来源目录的权限"})
    messages, sources, warnings = [], [], []
    seen = set()
    zone = ZoneInfo(config["timezone"])
    dates = {}
    for path in sorted(set(files)):
        label = _label(root, path)
        try:
            rows, notices = read_file(path)
        except (OSError, ValueError) as error:
            errors.append({"source": label, "message": str(error)})
            continue
        warnings.extend({
            "source": label,
            "message": message
        } for message in notices)
        selected = False
        for message in rows:
            local_day = dt.datetime.fromisoformat(
                message["timestamp"]).astimezone(zone).date()
            if day is not None and local_day != day:
                continue
            key = (message["timestamp"], message["role"], message["text"],
                   message["conversation_id"])
            if key in seen:
                continue
            seen.add(key)
            selected = True
            messages.append(message)
            dates.setdefault(str(local_day), []).append(message)
        if selected:
            sources.append(label)
    messages.sort(key=lambda item:
                  (item["timestamp"], item["conversation_id"], item["role"]))

    def digest(rows: list[dict]) -> str:
        normalized = sorted(rows,
                            key=lambda item: json.dumps(item, sort_keys=True))
        return hashlib.sha256(
            json.dumps(normalized, sort_keys=True,
                       ensure_ascii=False).encode()).hexdigest()

    return {
        "date":
            str(day) if day else None,
        "directory":
            str(configured),
        "source_kinds":
            sorted({
                message.get("source", "ChatGPT 导出或手工快照") for message in messages
            }),
        "exists":
            configured.is_dir(),
        "file_count":
            len(set(files)),
        "sources":
            sources,
        "messages":
            messages,
        "errors":
            errors,
        "warnings":
            warnings,
        "source_fingerprint":
            digest(messages),
        "dates": {
            key: {
                "messages": len(rows),
                "fingerprint": digest(rows)
            } for key, rows in sorted(dates.items())
        },
        "coverage_start":
            messages[0]["timestamp"] if messages else None,
        "coverage_end":
            messages[-1]["timestamp"] if messages else None,
    }


def diagnostics(root: Path, config: dict) -> dict:
    """返回不包含聊天正文的来源检查结果。"""
    report = scan(root, config)
    return {key: value for key, value in report.items() if key != "messages"}
