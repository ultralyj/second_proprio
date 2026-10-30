"""在本机与轻量调度服务器之间同步经过脱敏的任务信封。"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from typing import Any

from second_proprio.store import Store
from second_proprio.store import task_id

_KINDS = frozenset({"papers", "course", "review", "care"})
_STATUSES = frozenset(
    {"pending", "running", "completed", "failed", "waiting_source", "skipped"})
_PUBLIC_PAPER_FIELDS = frozenset({
    "id", "version", "title", "abstract", "url", "published", "updated",
    "categories", "topic_id", "reason"
})
_PAPER_ID = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})")
_TOPIC_ID = re.compile(r"[a-z0-9][a-z0-9-]*")


def revision(config: dict[str, Any]) -> str:
    """返回配置规范化 JSON 的 SHA256。"""
    encoded = json.dumps(config,
                         ensure_ascii=False,
                         sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sanitized_config(config: dict[str, Any]) -> dict[str, Any]:
    """构造服务器所需的最小配置，不携带本机路径或个人资料。

    Args:
        config: 已通过本机配置校验的完整配置。

    Returns:
        仅含调度、公共论文检索参数和服务器相对路径的配置。
    """
    scheduler = config["scheduler"]
    result = {
        "version": config.get("version", 1),
        "timezone": config.get("timezone", "Asia/Shanghai"),
        "scheduler": {
            "enabled": scheduler.get("enabled", True),
            "start_date": scheduler["start_date"],
        },
        "schedules": {
            kind: {
                "enabled": config["schedules"][kind].get("enabled", True),
                "weekdays": list(config["schedules"][kind]["weekdays"]),
                "times": list(config["schedules"][kind]["times"]),
            } for kind in sorted(_KINDS)
        },
        "paths": {
            "papers": "papers",
            "courses": "courses",
            "private": "private",
            "state": ".state",
        },
    }
    arxiv = config.get("arxiv", {})
    result["arxiv"] = {
        key: copy.deepcopy(arxiv[key])
        for key in ("lookback_days", "max_papers_per_day",
                    "request_interval_seconds")
        if key in arxiv
    }
    result["arxiv"]["topics"] = [{
        key: copy.deepcopy(topic[key])
        for key in ("id", "name", "categories", "keywords", "exclude_keywords")
        if key in topic
    }
                                 for topic in arxiv.get("topics", [])]
    return result


def _public_paper(value: Any) -> dict[str, Any]:
    """仅保留论文元数据白名单中的 JSON 值。"""
    if not isinstance(value, dict):
        raise ValueError("论文元数据必须是 JSON 对象。")
    for key, pattern in (("id", _PAPER_ID), ("topic_id", _TOPIC_ID)):
        if not isinstance(value.get(key), str) or not pattern.fullmatch(
                value[key]):
            raise ValueError(f"论文元数据缺少有效的 {key}。")
    version = value.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("论文版本必须是正整数。")
    result = {}
    for key in _PUBLIC_PAPER_FIELDS:
        if key not in value:
            continue
        item = value.get(key)
        if key == "version":
            result[key] = version
        elif key == "categories":
            if not isinstance(item, list) or not all(
                    isinstance(category, str) for category in item):
                raise ValueError("论文分类必须是字符串列表。")
            result[key] = list(item)
        elif isinstance(item, str):
            result[key] = item
        else:
            raise ValueError(f"论文元数据 {key} 必须是字符串。")
    return result


def task_envelope(task: dict[str, Any]) -> dict[str, Any]:
    """校验并提取任务信封，排除结果、私密文本与任意执行指令。

    Args:
        task: 本机数据库记录或服务器传回的任务。

    Returns:
        可安全交换的任务状态及公共论文候选。

    Raises:
        ValueError: 必填字段、类型、时间或状态不符合协议。
    """
    if not isinstance(task, dict):
        raise ValueError("任务必须是 JSON 对象。")
    if task.get("kind") not in _KINDS:
        raise ValueError("服务器返回了不支持的任务类型。")
    if task.get("status", "pending") not in _STATUSES:
        raise ValueError("服务器返回了不支持的任务状态。")
    for key in ("id", "scheduled_at", "config_revision"):
        if not isinstance(task.get(key), str) or not task[key]:
            raise ValueError(f"任务缺少有效的 {key} 字段。")
    if len(task["id"]) > 256:
        raise ValueError("任务标识过长。")
    scheduled = datetime.datetime.fromisoformat(task["scheduled_at"].replace(
        "Z", "+00:00"))
    if scheduled.tzinfo is None:
        raise ValueError("任务计划时间必须包含时区。")
    if task["id"] != task_id(task["kind"], task["scheduled_at"]):
        raise ValueError("任务标识与类型、计划时间不一致。")
    result = {
        "id": task["id"],
        "kind": task["kind"],
        "scheduled_at": task["scheduled_at"],
        "config_revision": task["config_revision"],
        "status": task.get("status", "pending"),
        "payload": {},
    }
    for key in ("created_at", "updated_at"):
        if isinstance(task.get(key), str):
            result[key] = task[key]
    if isinstance(task.get("attempts"), int) and task["attempts"] >= 0:
        result["attempts"] = task["attempts"]
    payload = task.get("payload", {})
    if task["kind"] == "papers" and isinstance(payload, dict):
        if "candidates" in payload:
            candidates = payload["candidates"]
            if not isinstance(candidates, list) or len(candidates) > 1000:
                raise ValueError("论文候选列表无效或超过同步上限。")
            result["payload"]["candidates"] = [
                _public_paper(candidate) for candidate in candidates
            ]
    return result


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """在目标目录内原子替换 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w",
                                         encoding="utf-8",
                                         dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _state(path: Path) -> dict[str, Any]:
    """读取已有同步状态，缺失或损坏时返回空状态。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def sync(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """经 SSH 交换任务并记录配置同步状态。

    Args:
        root: 本机项目根目录。
        config: 已校验的本机完整配置。

    Returns:
        含 status、desired_revision、remote_revision 和错误信息的状态。
        同步失败时保留上次确认的服务器版本，不宣称新配置已生效。
    """
    state_dir = root / config["paths"]["state"]
    state_path = state_dir / "sync-status.json"
    current = _state(state_path)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    public_config = sanitized_config(config)
    desired_revision = revision(public_config)
    current.update({
        "last_attempt": now,
        "desired_revision": desired_revision,
        "status": "disabled" if not config["server"]["enabled"] else "pending",
    })
    if not config["server"]["enabled"]:
        _write_json(state_path, current)
        return current
    store = None
    try:
        server = config["server"]
        host = server["ssh_host"]
        if not isinstance(host, str) or not re.fullmatch(
                r"[A-Za-z0-9_][A-Za-z0-9_.@:\[\]-]*", host):
            raise ValueError("SSH 主机别名无效。")
        remote_root = Path(server["root"])
        if not remote_root.is_absolute() or "\n" in str(remote_root):
            raise ValueError("服务器项目目录必须是绝对路径。")
        store = Store(state_dir / "queue.sqlite3")
        outgoing = [task_envelope(task) for task in store.export_tasks()]
        for task in outgoing:
            if task["status"] == "running":
                task["status"] = "pending"
        payload = json.dumps({
            "config": public_config,
            "tasks": outgoing
        },
                             ensure_ascii=False)
        remote_command = " ".join([
            shlex.quote(str(remote_root / ".venv/bin/python")),
            "-m",
            "second_proprio",
            "--root",
            shlex.quote(str(remote_root)),
            "server-exchange",
        ])
        timeout = int(server.get("connect_timeout_seconds", 8))
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={timeout}",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UpdateHostKeys=no",
            "-o",
            "LogLevel=ERROR",
            host,
            remote_command,
        ]
        completed = subprocess.run(
            command,
            input=payload,
            text=True,
            capture_output=True,
            check=False,
            timeout=max(timeout + 10, 30),
        )
        if completed.returncode:
            detail = completed.stderr.strip()[:1500]
            raise ValueError(f"SSH 同步失败（{completed.returncode}）：{detail}")
        if len(completed.stdout.encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("服务器同步结果超过 4 MiB 上限。")
        response = json.loads(completed.stdout)
        if not isinstance(response, dict):
            raise ValueError("服务器响应必须是 JSON 对象。")
        if response.get("config_revision") != desired_revision:
            raise ValueError("服务器尚未确认本次配置版本。")
        tasks = response.get("tasks")
        if not isinstance(tasks, list):
            raise ValueError("服务器响应缺少 tasks 列表。")
        incoming = [task_envelope(task) for task in tasks]
        store.merge_tasks(incoming)
        current.update({
            "status": "ok",
            "last_success": now,
            "remote_revision": desired_revision,
            "received_tasks": len(incoming),
            "sent_tasks": len(outgoing),
        })
        current.pop("error", None)
    except (OSError, ValueError, TypeError, KeyError,
            subprocess.SubprocessError) as error:
        current.update({"status": "error", "error": str(error)})
    finally:
        if store is not None:
            store.close()
    _write_json(state_path, current)
    return current
