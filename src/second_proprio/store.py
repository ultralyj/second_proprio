"""SQLite 持久队列；完成确认与远端任务合并分离。"""

import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3

KINDS = frozenset({"papers", "course", "review", "care"})
STATUSES = frozenset({
    "pending", "running", "completed", "failed", "waiting_source", "skipped",
    "cancel_requested", "cancelled", "waiting_review"
})


def timestamp() -> str:
    """返回带时区的 UTC 时间。"""
    return dt.datetime.now(dt.timezone.utc).isoformat()


def task_id(kind: str, scheduled_at: str) -> str:
    """生成跨机器一致的任务标识，与配置版本无关。"""
    instant = dt.datetime.fromisoformat(scheduled_at)
    if instant.tzinfo is None:
        raise ValueError("任务时间必须包含时区")
    canonical = instant.astimezone(dt.timezone.utc).isoformat()
    return hashlib.sha256(f"{kind}|{canonical}".encode()).hexdigest()[:24]


class Store:
    """维护可幂等写入、显式确认的任务状态。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                scheduled_at TEXT NOT NULL, config_revision TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}', last_error TEXT,
                retry_at TEXT, processed_on TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(tasks)")
        }
        extra = {
            "failures": "INTEGER NOT NULL DEFAULT 0",
            "source": "TEXT NOT NULL DEFAULT 'schedule'",
            "stage": "TEXT NOT NULL DEFAULT ''",
            "started_at": "TEXT",
            "finished_at": "TEXT"
        }
        with self.connection:
            for name, sql_type in extra.items():
                if name not in columns:
                    self.connection.execute(
                        f"ALTER TABLE tasks ADD COLUMN {name} {sql_type}")
            if "failures" not in columns:
                self.connection.execute(
                    "UPDATE tasks SET failures=attempts WHERE status='failed'")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS task_events "
                "(sequence INTEGER PRIMARY KEY AUTOINCREMENT,"
                "task_id TEXT,at TEXT,stage TEXT,message TEXT)")

    def add(self,
            kind: str,
            scheduled_at: str,
            config_revision: str,
            payload: dict | None = None) -> str:
        """登记任务；相同类型与计划时刻只保留一次。"""
        if kind not in KINDS:
            raise ValueError(f"未知任务类型：{kind}")
        identifier = task_id(kind, scheduled_at)
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO tasks "
                "(id,kind,scheduled_at,config_revision,payload_json,created_at,"
                "updated_at) VALUES (?,?,?,?,?,?,?)",
                (identifier, kind, scheduled_at, config_revision,
                 json.dumps(payload or {},
                            ensure_ascii=False), timestamp(), timestamp()))
        return identifier

    def export_tasks(self) -> list[dict]:
        """导出任务；跨机器传输前必须由同步层白名单过滤。"""
        rows = self.connection.execute(
            "SELECT * FROM tasks ORDER BY scheduled_at").fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            value["result"] = json.loads(value.pop("result_json"))
            result.append(value)
        return result

    def merge_tasks(self,
                    tasks: list[dict],
                    accept_completed: bool = False) -> None:
        """合并远端信封；本机不会信任远端声称的生成完成状态。"""
        for task in tasks:
            kind = task["kind"]
            scheduled = task["scheduled_at"]
            identifier = task_id(kind, scheduled)
            if task["id"] != identifier or task.get("status") not in STATUSES:
                raise ValueError("远端任务标识或状态不合法")
            payload = task.get("payload", {}) if kind == "papers" else {}
            self.add(kind, scheduled, task["config_revision"], payload)
            if payload:
                with self.connection:
                    self.connection.execute(
                        "UPDATE tasks SET payload_json=? WHERE id=?",
                        (json.dumps(payload, ensure_ascii=False), identifier))
            if accept_completed and task["status"] in {"completed", "skipped"}:
                self.finish(identifier, task["status"])

    def finish(self,
               identifier: str,
               status: str,
               result: dict | None = None,
               error: str | None = None,
               retry_at: str | None = None,
               processed_on: str | None = None) -> None:
        """原子确认处理结果或登记可重试失败。"""
        if status not in STATUSES:
            raise ValueError(f"未知状态：{status}")
        with self.connection:
            if status == "failed":
                self.connection.execute(
                    "UPDATE tasks SET failures=failures+1 WHERE id=?",
                    (identifier,))
            self.connection.execute(
                "UPDATE tasks SET status=?,result_json=?,last_error=?,"
                "retry_at=?,processed_on=?,updated_at=? WHERE id=?",
                (status, json.dumps(result or {}, ensure_ascii=False), error,
                 retry_at, processed_on, timestamp(), identifier))

    def claim(self, identifier: str) -> None:
        """由持有进程锁的执行器认领任务。"""
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET status='running',attempts=attempts+1,"
                "updated_at=? WHERE id=?", (timestamp(), identifier))

    def recover(self) -> None:
        """独占锁取得后，将上次进程留下的运行中任务重新入队。"""
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET status='pending' WHERE status='running'")

    def get_setting(self, key: str, default: str = "") -> str:
        """读取调度游标。"""
        row = self.connection.execute("SELECT value FROM settings WHERE key=?",
                                      (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        """保存调度游标或每日计数。"""
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
                (key, value))

    def get(self, identifier: str) -> dict:
        """按标识读取任务，不接受未知任务。"""
        for task in self.export_tasks():
            if task["id"] == identifier:
                return task
        raise ValueError("任务不存在")

    def event(self, identifier: str, stage: str, message: str) -> None:
        """记录结构化阶段，避免将模型工具正文视作状态。"""
        with self.connection:
            self.connection.execute("UPDATE tasks SET stage=? WHERE id=?",
                                    (stage, identifier))
            self.connection.execute(
                "INSERT INTO task_events(task_id,at,stage,message) "
                "VALUES (?,?,?,?)", (identifier, timestamp(), stage, message))

    def events(self,
               identifier: str | None = None,
               after: int = 0) -> list[dict]:
        """读取有界的事件增量。"""
        rows = self.connection.execute(
            "SELECT * FROM task_events WHERE sequence>? "
            "AND (? IS NULL OR task_id=?) ORDER BY sequence LIMIT 500",
            (after, identifier, identifier)).fetchall()
        return [dict(row) for row in rows]

    def retry(self, identifier: str) -> None:
        """保留执行历史，开始新的失败预算。"""
        self.finish(identifier, "pending", self.get(identifier)["result"])
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET failures=0,finished_at=NULL WHERE id=?",
                (identifier,))

    def close(self) -> None:
        """关闭数据库连接。"""
        self.connection.close()
