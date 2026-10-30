"""在独立草稿工作区执行重任务，仅把结果返回给后台所有者。"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import signal
import threading
import time

from second_proprio import config as configuration
from second_proprio import runner
from second_proprio.store import Store


def _emit(stage: str, message: str) -> None:
    """向父进程发送固定格式阶段事件。"""
    print(json.dumps({
        "stage": stage,
        "message": message
    }, ensure_ascii=False),
          flush=True)


def main(spec_path: Path) -> None:
    """读取可信任务快照，运行并写入可恢复结果。"""
    spec = json.loads(spec_path.read_text("utf-8"))
    root = Path(spec["workspace"])
    config = spec["config"]
    config["runtime"]["notify"] = False
    parent = os.getppid()
    marker = spec_path.parent / "codex-process.json"
    os.environ["SECOND_PROPRIO_CODEX_PID_FILE"] = str(marker)

    def stop(_signum: int, _frame: object) -> None:
        if marker.exists():
            try:
                os.killpg(json.loads(marker.read_text())["pid"], signal.SIGTERM)
            except (OSError, ValueError, KeyError):
                pass
        raise SystemExit(143)

    def watch_parent() -> None:
        while True:
            time.sleep(2)
            if os.getppid() != parent:
                os.kill(os.getpid(), signal.SIGTERM)
                return

    signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=watch_parent, daemon=True).start()
    original_codex = runner.codex

    def generating(*args: object, **kwargs: object) -> dict:
        label = str(args[3]) if len(args) > 3 else "内容"
        _emit("generating", f"生成解读或反馈：{label}")
        result = original_codex(*args, **kwargs)
        _emit("validating", f"校验成果：{label}")
        return result

    runner.codex = generating
    original_collect = runner.papers.collect
    original_prepare = runner.papers.prepare

    def collecting(*args: object, **kwargs: object) -> object:
        _emit("collecting", "采集论文元数据")
        return original_collect(*args, **kwargs)

    def preparing(*args: object, **kwargs: object) -> object:
        _emit("downloading", "下载与核对原文、插图")
        path = root / config["paths"]["state"] / "paper-candidates.json"
        records = json.loads(path.read_text("utf-8")) if path.exists() else []
        excluded = set(spec.get("exclude_papers", []))
        filtered = [
            item for item in records if str(
                runner.papers._paper_directory(root, config, item).relative_to(  # pylint: disable=protected-access
                    root)) not in excluded
        ]
        configuration.atomic_json(path, filtered)
        try:
            return original_prepare(*args, **kwargs)
        finally:
            configuration.atomic_json(path, records)

    runner.papers.collect = collecting
    runner.papers.prepare = preparing
    store = Store(root / config["paths"]["state"] / "queue.sqlite3")
    try:
        for key, value in spec.get("settings", {}).items():
            store.set_setting(key, value)
        for task in spec.get("queued", []):
            store.add(task["kind"], task["scheduled_at"],
                      task["config_revision"], task["payload"])
        now = dt.datetime.fromisoformat(spec["now"])
        _emit("running", "已启动独立执行进程")
        result = runner.execute(root, config, store, spec["task"], now)
    except (OSError, ValueError, RuntimeError) as error:
        result = {"status": "failed", "error": str(error), "artifacts": []}
    finally:
        current_settings = dict(
            store.connection.execute(
                "SELECT key,value FROM settings").fetchall())
        store.close()
    result["settings"] = {
        key: value
        for key, value in current_settings.items()
        if value != spec.get("settings_base", spec.get("settings", {})).get(key)
    }
    result["settings_base"] = {
        key: spec.get("settings_base", spec.get("settings", {})).get(key, "0")
        for key in result["settings"]
    }
    configuration.atomic_json(spec_path.parent / "result.json", result)
    _emit("finished", "生成阶段结束，等待后台验收与发布")
