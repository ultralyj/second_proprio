"""管理单一桌面后台、响应本机控制并隔离执行长任务。"""

from __future__ import annotations

import asyncio
import copy
import ctypes
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid

import jsonschema

from second_proprio import config as configuration
from second_proprio import personal
from second_proprio import rpc
from second_proprio import runner
from second_proprio import scheduler
from second_proprio import sync
from second_proprio.store import KINDS, Store, timestamp

VERSION = "1"
TERMINAL = {"completed", "skipped", "cancelled", "waiting_review"}


def _read(path: Path, default: object) -> object:
    """读取可选 JSON 状态文件。"""
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _same_user(connection: socket.socket) -> bool:
    """核实 Unix 连接来自当前用户。"""
    if sys.platform == "darwin":
        uid = ctypes.c_uint(0)
        gid = ctypes.c_uint(0)
        result = ctypes.CDLL(None).getpeereid(connection.fileno(),
                                              ctypes.byref(uid),
                                              ctypes.byref(gid))
        return result == 0 and uid.value == os.getuid()
    if hasattr(socket, "SO_PEERCRED"):
        import struct  # pylint: disable=import-outside-toplevel
        credentials = connection.getsockopt(socket.SOL_SOCKET,
                                            socket.SO_PEERCRED, 12)
        return struct.unpack("3i", credentials)[1] == os.getuid()
    return False


def _matching_process(pid: int, expected: str) -> bool:
    """核对进程命令后才允许终止，避免复用 PID 误伤。"""
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=5)
    return result.returncode == 0 and expected in result.stdout


def input_fingerprint(task: dict, baseline: dict) -> str:
    """把实际输入与任务类型绑定，忽略请求去重用的随机标识。"""
    payload = {
        key: value
        for key, value in task.get("payload", {}).items()
        if key != "request_id"
    }
    return hashlib.sha256(
        json.dumps({
            "kind": task["kind"],
            "task": payload,
            "baseline": baseline
        },
                   sort_keys=True).encode()).hexdigest()


class Service:  # pylint: disable=too-many-instance-attributes
    """拥有队列写入权；所有业务提交均在同一事件循环中处理。"""

    def __init__(self, root: Path, app_executable: str | None = None):
        self.root = root.resolve()
        self.config = configuration.load(root)
        self.state = root / self.config["paths"]["state"]
        self.state.mkdir(parents=True, exist_ok=True)
        self.state.chmod(0o700)
        self.store = Store(self.state / "queue.sqlite3")
        with self.store.connection:
            self.store.connection.execute(
                "CREATE TABLE IF NOT EXISTS notifications "
                "(id TEXT PRIMARY KEY,title TEXT,body TEXT,created_at TEXT,"
                "status TEXT DEFAULT 'pending',error TEXT)")
        self.app_executable = app_executable
        self.active: dict | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.stopping = False
        self.closed = asyncio.Event()
        self.jobs: set[asyncio.Task] = set()
        self.next_sync = 0.0
        self.last_schedule = 0.0
        self.syncing = False
        self.notifying = False
        self.config_error: str | None = None

    @property
    def paused(self) -> bool:
        """返回持久暂停状态。"""
        return self.store.get_setting("desktop.paused", "false") == "true"

    def background(self, coroutine: object) -> None:
        """跟踪异步工作，停服时可以有序收尾。"""
        task = asyncio.create_task(coroutine)
        self.jobs.add(task)

        def finished(done: asyncio.Task) -> None:
            self.jobs.discard(done)
            if not done.cancelled() and done.exception():
                error = str(done.exception())[:300]
                self.notify(f"service:{type(done.exception()).__name__}",
                            "second proprio · 后台需要处理", error)

        task.add_done_callback(finished)

    def task(self, identifier: str) -> dict:
        """返回 UI 所需的任务字段。"""
        return self.store.get(identifier)

    def notify(self, key: str, title: str, message: str) -> None:
        """将通知加入本地持久收件箱，不把请求成功当作已读。"""
        with self.store.connection:
            self.store.connection.execute(
                "INSERT OR IGNORE INTO notifications(id,title,body,created_at) "
                "VALUES (?,?,?,?)", (key, title, message, timestamp()))

    def config_result(self) -> dict:
        """区分有效本机配置和服务器同步状态。"""
        state = _read(self.state / "sync-status.json", {"status": "unknown"})
        state.update(current_revision=sync.revision(
            sync.sanitized_config(self.config)),
                     in_progress=self.syncing)
        if not self.config["server"]["enabled"]:
            state["status"] = "disabled"
        return {
            "config": self.config,
            "revision": configuration.revision(self.config),
            "sync": state,
            "error": self.config_error
        }

    async def dispatch(self, method: str, params: dict) -> object:  # pylint: disable=too-many-return-statements
        """分发固定接口，禁止任意 shell、SQL 或函数调用。"""
        if method == "system.status":
            tasks = self.store.export_tasks()
            counts = {}
            for task in tasks:
                counts[task["status"]] = counts.get(task["status"], 0) + 1
            notices = [
                dict(row) for row in self.store.connection.execute(
                    "SELECT * FROM notifications "
                    "ORDER BY created_at DESC LIMIT 30")
            ]
            return {
                "running":
                    True,
                "version":
                    VERSION,
                "root":
                    str(self.root),
                "paused":
                    self.paused,
                "active_task":
                    self.task(self.active["id"]) if self.active else None,
                "counts":
                    counts,
                "sync":
                    self.config_result()["sync"],
                "notifications":
                    notices,
                "stopping":
                    self.stopping,
                "config_error":
                    self.config_error
            }
        if method == "config.get":
            return self.config_result()
        if method == "config.validate":
            configuration.validate(self.root, params["config"])
            return {"valid": True}
        if method == "config.save":
            return self.save_config(params)
        if method == "config.sync":
            self.next_sync = 0
            return {"status": "queued"}
        if method == "tasks.list":
            return {
                "tasks": list(reversed(self.store.export_tasks())),
                "paused": self.paused
            }
        if method == "tasks.detail":
            return {
                "task": self.task(params["id"]),
                "events": self.store.events(params["id"])
            }
        if method == "tasks.events":
            return {
                "events":
                    self.store.events(params.get("id"),
                                      int(params.get("after", 0)))
            }
        if method == "tasks.pause":
            if not isinstance(params.get("paused"), bool):
                raise ValueError("暂停状态必须是布尔值")
            self.store.set_setting("desktop.paused",
                                   json.dumps(params["paused"]))
            return {"paused": self.paused}
        if method == "tasks.submit":
            return self.submit(params)
        if method == "tasks.retry":
            task = self.task(params["id"])
            if task["status"] not in {"failed", "cancelled", "waiting_source"}:
                raise ValueError("只有失败、已取消或等待来源的任务可以重试")
            self.store.retry(task["id"])
            self.store.event(task["id"], "pending", "手动重试，已重置失败预算")
            return {"task": self.task(task["id"])}
        if method == "tasks.cancel":
            return await self.cancel(params["id"])
        if method == "system.stop":
            if self.active and not params.get("cancel_current", False):
                raise rpc.RpcError("active_task", "有任务正在运行，请明确取消后停止后台")
            self.store.set_setting("desktop.paused", "true")
            if self.active:
                await self.cancel(self.active["id"])
            self.stopping = True
            return {"stopping": True}
        if method.startswith(
            ("content.", "course.", "review.", "relationship.")):
            from second_proprio import desktop_content  # pylint: disable=import-outside-toplevel
            result = desktop_content.dispatch(self.root, self.config, method,
                                              params)
            if method == "content.accept" and isinstance(result, dict):
                self.apply_publication(result)
            if method == "content.reject" and isinstance(
                    result, dict) and result.get("task_id"):
                self.store.finish(result["task_id"], "skipped",
                                  {"summary": "候选已放弃，原文保留"})
            if method in {"review.import", "review.add"}:
                for task in self.store.export_tasks():
                    day = task["payload"].get(
                        "date") or task["scheduled_at"][:10]
                    if (task["kind"] == "review" and
                            task["status"] == "waiting_source" and
                            day in result.get("dates", [])):
                        self.store.retry(task["id"])
            return result
        raise rpc.RpcError("unknown_method", "不支持的接口")

    def save_config(self, params: dict) -> dict:
        """校验版本和目录，再保存新配置，不等待 SSH。"""
        current = configuration.load(self.root)
        if params.get("expected_revision") != configuration.revision(current):
            raise rpc.RpcError("conflict", "配置已被其他操作修改，请重新载入",
                               self.config_result())
        candidate = params["config"]
        configuration.validate(self.root, candidate)
        if candidate["paths"] != current["paths"] or candidate["review"][
                "source_directory"] != current["review"]["source_directory"]:
            raise ValueError("首版不支持运行中移动数据目录")
        configuration.atomic_json(self.root / "config/settings.json", candidate)
        self.config = copy.deepcopy(candidate)
        if current.get("desktop",
                       {}).get("launch_at_login", True) != candidate.get(
                           "desktop", {}).get("launch_at_login", True):
            from second_proprio import desktop_deployment  # pylint: disable=import-outside-toplevel
            desktop_deployment.apply_login_policy(
                self.root,
                candidate.get("desktop", {}).get("launch_at_login", True))
        self.config_error = None
        if current["schedules"] != candidate["schedules"] or current[
                "timezone"] != candidate["timezone"]:
            now = scheduler.now_local(candidate)
            for task in self.store.export_tasks():
                if task["source"] != "schedule":
                    continue
                planned = dt.datetime.fromisoformat(
                    task["scheduled_at"]).astimezone(now.tzinfo)
                schedule = candidate["schedules"][task["kind"]]
                matches = (planned.weekday() in schedule["weekdays"] and
                           planned.strftime("%H:%M") in schedule["times"])
                if task["status"] == "pending" and not matches:
                    self.store.finish(task["id"], "skipped",
                                      {"summary": "日程修改后重新计算"})
                elif (task["status"] == "skipped" and matches and
                      task["result"].get("summary") == "日程修改后重新计算"):
                    self.store.finish(task["id"], "pending")
            self.last_schedule = 0
        self.next_sync = 0
        return {**self.config_result(), "saved": True, "effective": "next_task"}

    def submit(self, params: dict) -> dict:
        """登记手动任务，重复提交相同运行项不重复执行。"""
        kind = params.get("kind")
        if kind not in KINDS:
            raise ValueError("未知任务类型")
        mode = params.get("mode", "default")
        if mode not in {"default", "feedback"} or (mode == "feedback" and
                                                   kind != "course"):
            raise ValueError("任务模式不合法")
        payload = {"mode": mode}
        if mode == "feedback":
            lesson = params.get("lesson_id")
            if not isinstance(lesson, str) or not lesson:
                raise ValueError("反馈任务需要章节标识")
            payload["lesson_id"] = lesson
        if params.get("date"):
            dt.date.fromisoformat(params["date"])
            payload["date"] = params["date"]
        request = params.get("request_id")
        for task in self.store.export_tasks():
            same = task["kind"] == kind and all(
                task["payload"].get(k) == v for k, v in payload.items())
            if (request and task["payload"].get("request_id") == request
               ) or (same and task["status"] in {
                   "pending", "running", "cancel_requested", "waiting_review"
               }):
                return {"task": task, "deduplicated": True}
        if request:
            payload["request_id"] = str(request)[:128]
        now = scheduler.now_local(self.config)
        identifier = self.store.add(kind, now.isoformat(),
                                    configuration.revision(self.config),
                                    payload)
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE tasks SET source='manual' WHERE id=?", (identifier,))
        self.store.event(identifier, "pending", "手动任务已入队")
        return {"task": self.task(identifier), "deduplicated": False}

    async def cancel(self, identifier: str) -> dict:
        """先记录取消意图，再确认运行进程退出。"""
        task = self.task(identifier)
        if task["status"] in TERMINAL:
            return {"task": task}
        if self.active and self.active["id"] == identifier:
            self.store.finish(identifier, "cancel_requested", task["result"])
            self.store.event(identifier, "cancelling", "正在结束执行进程，草稿保留")
            self.background(self.terminate_active())
        else:
            self.store.finish(identifier, "cancelled", {"summary": "用户取消，草稿保留"})
            self.store.event(identifier, "cancelled", "已取消")
        return {"task": self.task(identifier)}

    async def terminate_active(self) -> None:
        """终止已验证归属的 Codex 进程组及工作进程。"""
        if not self.active:
            return
        folder = Path(self.active["folder"])
        marker = _read(folder / "codex-process.json", {})
        pid = marker.get("pid")
        if isinstance(pid, int) and _matching_process(
                pid, str(self.active["workspace"])):
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        process = self.process
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if isinstance(pid, int) and _matching_process(
                pid, str(self.active["workspace"])):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def apply_publication(self, result: dict) -> None:
        """只在发布完成后提交关联计数与任务状态。"""
        if result.get("status") not in {"completed", "published"}:
            return
        publication = result.get("publication_id")
        marker = f"desktop.applied_publication:{publication}"
        identifier = result.get("task_id")
        with self.store.connection:
            if not publication or not self.store.get_setting(marker):
                for key, value in result.get("settings", {}).items():
                    if not key.startswith(
                        ("paper_count:", "course_generated:")):
                        continue
                    if key.startswith("paper_count:"):
                        previous = result.get("settings_base", {}).get(key, "0")
                        delta = max(0, int(value) - int(previous))
                        value = str(
                            int(self.store.get_setting(key, "0")) + delta)
                    self.store.connection.execute(
                        "INSERT OR REPLACE INTO settings(key,value) "
                        "VALUES (?,?)", (key, str(value)))
                if publication:
                    self.store.connection.execute(
                        "INSERT OR REPLACE INTO settings(key,value) "
                        "VALUES (?,?)", (marker, "true"))
            if identifier:
                self.store.connection.execute(
                    "UPDATE tasks SET status='completed',result_json=?,"
                    "last_error=NULL,retry_at=NULL,updated_at=? WHERE id=?",
                    (json.dumps(result,
                                ensure_ascii=False), timestamp(), identifier))
        if identifier:
            self.store.event(identifier, "completed", "成果已发布")

    async def run_task(self, task: dict) -> None:
        """准备隔离工作区，启动重任务并验收发布其输出。"""
        from second_proprio import documents  # pylint: disable=import-outside-toplevel
        config = copy.deepcopy(self.config)
        now = scheduler.now_local(config)
        run_id = uuid.uuid4().hex
        folder = self.state / "desktop-runs" / run_id
        folder.mkdir(parents=True, exist_ok=True)
        self.active = {
            "id": task["id"],
            "folder": str(folder),
            "workspace": "",
            "run_id": run_id
        }
        self.store.claim(task["id"])
        self.store.event(task["id"], "preparing", "准备独立草稿工作区")
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE tasks SET started_at=?,config_revision=? WHERE id=?",
                (timestamp(), configuration.revision(config), task["id"]))
        try:
            previous = task.get("result", {})
            previous_workspace = Path(previous.get("workspace", "/nonexistent"))
            if (task["kind"] == "papers" and
                    previous_workspace.is_relative_to(self.state) and
                    previous_workspace.is_dir() and
                    task["config_revision"] == configuration.revision(config)):
                prepared = {
                    "workspace":
                        str(previous_workspace),
                    "baseline":
                        _read(previous_workspace.parent / "baseline.json", {})
                }
            else:
                prepared = await asyncio.to_thread(documents.create_workspace,
                                                   self.root, config, run_id,
                                                   task)
            workspace = Path(prepared["workspace"])
            fingerprint = input_fingerprint(task, prepared["baseline"])
            document_store = documents.DocumentStore(self.root, config)
            manifests = [
                _read(path, {})
                for path in (document_store.directory /
                             "publications").glob("*/manifest.json")
            ]
            if task.get("source") != "manual":
                old = next(
                    (item for item in manifests
                     if item.get("input_fingerprint") == fingerprint and
                     item.get("status") in {"waiting_review", "rejected"}),
                    None)
                if old:
                    self.store.finish(
                        task["id"], "skipped", {
                            "summary": "相同输入已有待采纳或放弃的候选，未再次调用模型",
                            "candidate_id": old["id"],
                            "workspace": str(workspace),
                            "run_id": run_id
                        })
                    return
            if not (workspace / ".git").is_dir():
                initialized = await asyncio.to_thread(
                    subprocess.run, ["git", "init", "--quiet",
                                     str(workspace)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10)
                if initialized.returncode:
                    raise RuntimeError("无法建立独立草稿 Git 边界")
            self.active["workspace"] = str(workspace)
            if self.task(task["id"])["status"] == "cancel_requested":
                self.store.finish(
                    task["id"], "cancelled", {
                        "summary": "已取消，草稿保留",
                        "draft": str(workspace),
                        "workspace": str(workspace),
                        "run_id": run_id
                    })
                return
            settings = dict(
                self.store.connection.execute(
                    "SELECT key,value FROM settings").fetchall())
            paper_key = f"paper_count:{now.date()}"
            settings[paper_key] = str(
                max(
                    int(settings.get(paper_key, "0")),
                    int(
                        self.store.get_setting(
                            f"desktop.paper_reserved:{now.date()}", "0"))))
            settings_base = dict(settings)
            if task["kind"] == "papers" and previous_workspace == workspace:
                previous_spec = _read(
                    self.state / "desktop-runs" /
                    previous.get("run_id", "missing") / "spec.json", {})
                shadow_store = Store(workspace / config["paths"]["state"] /
                                     "queue.sqlite3")
                try:
                    for key, value in shadow_store.connection.execute(
                            "SELECT key,value FROM settings"):
                        if key.startswith("paper_count:"):
                            delta = max(
                                0,
                                int(value) - int(
                                    previous_spec.get(
                                        "settings_base",
                                        previous_spec.get("settings", {})).get(
                                            key, "0")))
                            settings[key] = str(
                                int(settings.get(key, "0")) + delta)
                finally:
                    shadow_store.close()
            excluded = []
            for manifest in manifests:
                if manifest.get("status") in {"waiting_review", "rejected"}:
                    excluded.extend(
                        str(Path(item["path"]).parent)
                        for item in manifest.get("files", [])
                        if item["path"].startswith(config["paths"]["papers"] +
                                                   "/") and
                        item["path"].endswith("/README.md"))
            spec = {
                "exclude_papers":
                    excluded,
                "workspace":
                    str(workspace),
                "config":
                    config,
                "task":
                    task,
                "now":
                    now.isoformat(),
                "settings":
                    settings,
                "settings_base":
                    settings_base,
                "queued": [
                    q for q in self.store.export_tasks()
                    if q["kind"] == "papers"
                ]
            }
            configuration.atomic_json(folder / "spec.json", spec)
            executable = [sys.executable] if getattr(
                sys, "frozen",
                False) else [sys.executable, "-m", "second_proprio"]
            command = executable + [
                "--root",
                str(workspace), "desktop-worker",
                str(folder / "spec.json")
            ]
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True)
            self.active["pid"] = self.process.pid
            configuration.atomic_json(self.state / "desktop-active.json",
                                      self.active)
            with (folder / "worker.log").open("wb") as log:
                while line := await self.process.stdout.readline():
                    log.write(line)
                    log.flush()
                    try:
                        event = json.loads(line)
                        if set(event) == {"stage", "message"}:
                            self.store.event(task["id"],
                                             str(event["stage"])[:64],
                                             str(event["message"])[:300])
                    except (ValueError, TypeError):
                        continue
            await self.process.wait()
            if self.task(task["id"])["status"] == "cancel_requested":
                await self.terminate_active()
                self.store.finish(
                    task["id"], "cancelled", {
                        "summary": "已取消，草稿保留",
                        "draft": str(workspace),
                        "workspace": str(workspace),
                        "run_id": run_id
                    })
                self.store.event(task["id"], "cancelled", "取消完成，未发布草稿")
                return
            result = _read(folder / "result.json", {
                "status": "failed",
                "error": "执行进程中断，草稿保留"
            })
            if result["status"] == "failed":
                raise RuntimeError(result.get("error", "执行失败"))
            if result["status"] == "waiting_source":
                retry = (scheduler.now_local(config) + dt.timedelta(
                    seconds=config["scheduler"]["retry_seconds"])).isoformat()
                result.update(workspace=str(workspace),
                              draft=str(workspace),
                              run_id=run_id)
                self.store.finish(task["id"],
                                  "waiting_source",
                                  result,
                                  retry_at=retry)
                self.notify(f"source:{task['id']}", "second proprio · 需要补充来源",
                            result.get("summary", "请补充任务所需来源"))
                return
            if task["kind"] == "papers":
                used = result.get("settings", {}).get(paper_key)
                if used is not None:
                    reserved_key = f"desktop.paper_reserved:{now.date()}"
                    self.store.set_setting(
                        reserved_key,
                        str(
                            max(int(used),
                                int(self.store.get_setting(reserved_key,
                                                           "0")))))
            result["task_id"] = task["id"]
            result["run_id"] = run_id
            result["input_fingerprint"] = fingerprint
            result["regenerate"] = task.get("source") == "manual"
            self.store.event(task["id"], "publishing", "校验版本并发布成果")
            published = documents.publish_workspace(self.root, config, run_id,
                                                    workspace,
                                                    prepared["baseline"],
                                                    result)
            published.setdefault("task_id", task["id"])
            published.setdefault(
                "settings",
                result.get("settings", {})
                if published.get("status") == "completed" else {})
            published.setdefault("summary", result.get("summary", "任务完成"))
            status = published.get("status", "completed")
            if status == "published":
                status = "completed"
            self.store.finish(task["id"],
                              status,
                              published,
                              processed_on=str(now.date()))
            self.apply_publication(published)
            if task["kind"] == "course" and task["payload"].get(
                    "mode") != "feedback":
                self.store.set_setting(f"desktop.course_reserved:{now.date()}",
                                       task["id"])
            if status == "waiting_review":
                self.notify(f"candidate:{task['id']}:{run_id}",
                            "second proprio · 更新待采纳", "已保留你的修改，请比较并采纳候选版本。")
            elif published.get("artifacts") and config.get("desktop", {}).get(
                    "notifications", "content_and_action") != "action_only":
                self.notify(f"done:{task['id']}:{run_id}",
                            "second proprio · 新内容", published["summary"][:200])
        except (OSError, ValueError, RuntimeError,
                jsonschema.ValidationError) as error:
            if self.task(task["id"])["status"] == "cancel_requested":
                self.store.finish(task["id"], "cancelled",
                                  {"summary": "已取消，草稿保留"})
            else:
                failures = self.task(task["id"])["failures"] + 1
                maximum = config["scheduler"]["max_attempts"]
                retry_at = (
                    scheduler.now_local(config) +
                    dt.timedelta(seconds=config["scheduler"]["retry_seconds"])
                ).isoformat() if failures < maximum else None
                self.store.finish(task["id"],
                                  "failed", {
                                      "draft": self.active["workspace"],
                                      "workspace": self.active["workspace"],
                                      "run_id": run_id
                                  },
                                  error=str(error),
                                  retry_at=retry_at)
                self.store.event(task["id"], "failed", str(error)[:300])
                login_error = any(part in str(error).lower()
                                  for part in ("not logged in",
                                               "authentication", "quota",
                                               "401"))
                if failures >= maximum or login_error or config.get(
                        "desktop", {}).get("notifications") == "all":
                    self.notify(f"failed:{task['id']}:{failures}",
                                "second proprio · 任务需要处理",
                                str(error).split("；日志：", 1)[0][:200])
        finally:
            with self.store.connection:
                self.store.connection.execute(
                    "UPDATE tasks SET finished_at=? WHERE id=?",
                    (timestamp(), task["id"]))
            (self.state / "desktop-active.json").unlink(missing_ok=True)
            self.active = None
            self.process = None

    async def synchronize(self) -> None:
        """在快照数据库交换远端信封，主队列仍由本进程独占写入。"""
        self.syncing = True
        config = copy.deepcopy(self.config)
        shadow = self.state / "desktop-sync"
        state = shadow / config["paths"]["state"]
        state.mkdir(parents=True, exist_ok=True)
        destination = Store(state / "queue.sqlite3")
        self.store.connection.backup(destination.connection)
        with destination.connection:
            destination.connection.execute(
                "DELETE FROM tasks WHERE source='manual'")
            destination.connection.execute(
                "UPDATE tasks SET status='pending' WHERE status IN "
                "('running','cancel_requested','waiting_review')")
            destination.connection.execute(
                "UPDATE tasks SET status='skipped' WHERE status='cancelled'")
        destination.close()
        try:
            result = await asyncio.to_thread(sync.sync, shadow, config)
            configuration.atomic_json(self.state / "sync-status.json", result)
            remote = Store(state / "queue.sqlite3")
            try:
                self.store.merge_tasks(remote.export_tasks())
            finally:
                remote.close()
        except (OSError, ValueError, RuntimeError) as error:
            configuration.atomic_json(self.state / "sync-status.json", {
                "status": "error",
                "error": str(error)
            })
        finally:
            self.syncing = False

    async def deliver_notifications(self) -> None:
        """使用无窗口通知入口，失败时留存待处理事项。"""
        self.notifying = True
        try:
            if not self.config["runtime"]["notify"]:
                return
            app = self.app_executable or _read(self.state / "desktop-app.json",
                                               {}).get("app_executable")
            if not app or not Path(app).is_file():
                return
            rows = self.store.connection.execute(
                "SELECT * FROM notifications WHERE status='pending' "
                "ORDER BY created_at LIMIT 20").fetchall()
            if not rows:
                return
            title = rows[0]["title"] if len(
                rows) == 1 else "second proprio · 新进展"
            body = rows[0]["body"] if len(rows) == 1 else "\n".join(
                row["body"][:90] for row in rows[:4])
            process = await asyncio.create_subprocess_exec(
                app,
                "--notify-json",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            try:
                output, _ = await asyncio.wait_for(
                    process.communicate(
                        json.dumps({
                            "title": title,
                            "body": body,
                            "message": body
                        }).encode()), 15)
                accepted = process.returncode == 0
                if output.strip():
                    response = json.loads(output)
                    accepted = accepted and response.get(
                        "accepted",
                        response.get("status") in {"accepted", "sent"})
                error = None if accepted else "系统通知未接受，请检查通知权限"
            except (asyncio.TimeoutError, ValueError):
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                accepted, error = False, "通知入口超时或响应无效"
            with self.store.connection:
                self.store.connection.executemany(
                    "UPDATE notifications SET status=?,error=? WHERE id=?",
                    [("accepted" if accepted else "failed", error, row["id"])
                     for row in rows])
        except OSError:
            pass
        finally:
            self.notifying = False

    async def recover(self) -> None:
        """确认遗留执行进程退出，再恢复异常中断任务。"""
        active = _read(self.state / "desktop-active.json", {})
        if active:
            for marker, expected in ((active, active.get("folder", "")), (_read(
                    Path(active.get("folder", "/nonexistent")) /
                    "codex-process.json", {}), active.get("workspace", ""))):
                pid = marker.get("pid")
                if isinstance(pid, int) and expected and _matching_process(
                        pid, expected):
                    try:
                        os.killpg(pid, signal.SIGTERM)
                        await asyncio.sleep(0.2)
                        if _matching_process(pid, expected):
                            os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        for task in self.store.export_tasks():
            retained = dict(task.get("result", {}))
            if active.get("id") == task["id"]:
                retained.update(workspace=active.get("workspace"),
                                draft=active.get("workspace"),
                                run_id=active.get("run_id"))
            if task["status"] == "cancel_requested":
                self.store.finish(task["id"], "cancelled", {
                    **retained, "summary": "恢复时确认取消"
                })
            elif task["status"] == "running":
                self.store.finish(task["id"], "pending", {
                    **retained, "summary": "上次执行中断，草稿保留"
                })
        from second_proprio import documents  # pylint: disable=import-outside-toplevel
        if hasattr(documents, "recover_publications"):
            for publication in documents.recover_publications(
                    self.root, self.config, active.get("run_id")):
                if publication.get("status") == "waiting_review":
                    self.store.finish(publication["task_id"], "waiting_review",
                                      publication)
                else:
                    self.apply_publication(publication)
        # 手动采纳没有活动 worker；精确补交正文已发布但队列尚未确认的候选。
        directory = documents.DocumentStore(self.root, self.config).directory
        for task in self.store.export_tasks():
            candidate = task["result"].get(
                "candidate_id") or task["result"].get("publication_id")
            if task["status"] != "waiting_review" or not isinstance(
                    candidate, str) or not candidate.isalnum():
                continue
            manifest = _read(
                directory / "publications" / candidate / "manifest.json", {})
            if manifest.get("task_id") != task["id"]:
                continue
            if manifest.get("status") == "published":
                self.apply_publication({
                    "status": "completed",
                    "publication_id": candidate,
                    "task_id": task["id"],
                    "summary": manifest.get("summary", "候选已采纳"),
                    "artifacts": [
                        entry["path"]
                        for entry in manifest.get("files", [])
                        if entry["path"].endswith(".md")
                    ],
                    "settings": manifest.get("settings", {}),
                    "settings_base": manifest.get("settings_base", {})
                })
            elif manifest.get("status") == "rejected":
                self.store.finish(task["id"], "skipped",
                                  {"summary": "候选已放弃，原文保留"})
        (self.state / "desktop-active.json").unlink(missing_ok=True)

    async def loop(self) -> None:
        """轻量轮询不会等待模型、下载和 SSH。"""
        await self.recover()
        try:
            while not self.stopping or self.active:
                now = scheduler.now_local(self.config)
                if time.monotonic() - self.last_schedule >= 5:
                    try:
                        loaded = configuration.load(self.root)
                        if loaded["paths"] != self.config["paths"]:
                            raise ValueError("后台运行期间不能移动数据目录")
                        self.config = loaded
                        self.config_error = None
                    except (OSError, ValueError,
                            jsonschema.ValidationError) as error:
                        self.config_error = str(error)[:300]
                    scheduler.enqueue(self.store, self.config, now)
                    self.last_schedule = time.monotonic()
                ready = scheduler.ready_tasks(self.store, self.config, now)
                for task in ready:
                    if task["kind"] == "care" and not self.stopping:
                        quiet = copy.deepcopy(self.config)
                        quiet["runtime"]["notify"] = False
                        try:
                            days = personal.relationship_days(
                                self.config, now.date())
                            result = personal.care(self.root, quiet, now)
                            self.store.claim(task["id"])
                            self.store.finish(task["id"], "completed", result)
                            self.notify(
                                f"care:{task['id']}",
                                f"关心 {self.config['relationship']['name']} · 第 "
                                f"{days} 天", result["summary"])
                        except (OSError, ValueError) as error:
                            self.store.finish(task["id"],
                                              "failed",
                                              error=str(error))
                    elif (not self.active and not self.paused and
                          not self.stopping) and (
                              self.config["runtime"]["auto_run"] or
                              task["source"] == "manual"):
                        if task["kind"] == "course" and task["payload"].get(
                                "mode"
                        ) != "feedback" and self.store.get_setting(
                                f"desktop.course_reserved:{now.date()}"):
                            self.store.finish(task["id"], "skipped",
                                              {"summary": "今日课程已生成或等待采纳"})
                            continue
                        # 先占用运行槽，避免异步准备前另一个任务同时进入。
                        self.active = {
                            "id": task["id"],
                            "folder": "",
                            "workspace": ""
                        }
                        self.background(self.run_task(task))
                if not self.syncing and time.monotonic(
                ) >= self.next_sync and not self.stopping:
                    self.next_sync = time.monotonic(
                    ) + self.config["scheduler"]["sync_interval_seconds"]
                    self.background(self.synchronize())
                if not self.notifying and not self.stopping:
                    self.background(self.deliver_notifications())
                await asyncio.sleep(1)
        finally:
            self.closed.set()

    async def connection(self, reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
        """校验同用户连接，返回单个结构化结果。"""
        identifier = None
        try:
            if not _same_user(writer.get_extra_info("socket")):
                raise rpc.RpcError("forbidden", "仅允许当前用户访问")
            line = await asyncio.wait_for(reader.readline(), 15)
            if len(line) > rpc.MAX_MESSAGE:
                raise ValueError("请求过大")
            request = json.loads(line)
            identifier = request.get("id")
            if not isinstance(request.get("method"), str) or not isinstance(
                    request.get("params", {}), dict):
                raise ValueError("接口请求格式不合法")
            result = await self.dispatch(request["method"],
                                         request.get("params", {}))
            response = {"id": identifier, "result": result}
        except Exception as error:  # pylint: disable=broad-exception-caught
            response = {
                "id": identifier,
                "error": {
                    "code": getattr(error, "code", "invalid_request"),
                    "message": str(error),
                    "details": getattr(error, "details", None)
                }
            }
        writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()


async def serve(root: Path, app_executable: str | None = None) -> None:
    """取得生命周期锁并开放本机 Socket。"""
    config = configuration.load(root)
    state = root / config["paths"]["state"]
    state.mkdir(parents=True, exist_ok=True)
    with runner.lock(root, config), (state / "daemon.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        service = Service(root, app_executable)
        address = rpc.socket_path(root, config)
        address.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(service.connection,
                                                 str(address),
                                                 limit=rpc.MAX_MESSAGE + 1)
        address.chmod(0o600)
        configuration.atomic_json(state / "daemon.json", {
            "pid": os.getpid(),
            "version": VERSION,
            "started_at": timestamp()
        })
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig,
                                    lambda: setattr(service, "stopping", True))
        try:
            async with server:
                await service.loop()
            if service.jobs:
                await asyncio.gather(*service.jobs, return_exceptions=True)
        finally:
            address.unlink(missing_ok=True)
            service.store.close()


def main(root: Path, app_executable: str | None = None) -> None:
    """运行桌面后台入口。"""
    asyncio.run(serve(root, app_executable))
