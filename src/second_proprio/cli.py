"""本机与服务器共享的命令行入口。"""

import argparse
import datetime as dt
import json
from pathlib import Path
import sys

import jsonschema

from second_proprio import config as configuration
from second_proprio import deployment
from second_proprio import papers
from second_proprio import personal
from second_proprio import rpc
from second_proprio import runner
from second_proprio import scheduler
from second_proprio import sync as synchronization
from second_proprio.store import KINDS, Store, task_id


def _parser() -> argparse.ArgumentParser:
    """构造显式参数接口，后台任务不读取交互输入。"""
    parser = argparse.ArgumentParser(description="second proprio 个人技能仓库")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "sync", "status", "tick", "collect",
                 "install-local", "render-systemd", "server-tick",
                 "server-exchange"):
        commands.add_parser(name)
    daemon = commands.add_parser("daemon")
    daemon.add_argument("--app-executable")
    worker = commands.add_parser("desktop-worker")
    worker.add_argument("spec", type=Path)
    run = commands.add_parser("run")
    run.add_argument("kind", nargs="?", choices=sorted(KINDS))
    run.add_argument("--date", type=dt.date.fromisoformat)
    run.add_argument("--retry-failed", action="store_true")
    apply = commands.add_parser("configure")
    apply.add_argument("source", type=Path)
    imported = commands.add_parser("import-chat")
    imported.add_argument("source", type=Path)
    mood = commands.add_parser("mood")
    mood.add_argument("score", type=int, nargs="?")
    mood.add_argument("--date", type=dt.date.fromisoformat)
    mood.add_argument("--note", default="")
    mood.add_argument("--action", default="")
    return parser


def _remote_config(root: Path, config: dict) -> dict:
    """借用完整 Schema 校验脱敏配置，返回不含本机字段的原值。"""
    if set(config) != {
            "version", "timezone", "scheduler", "schedules", "paths", "arxiv"
    }:
        raise ValueError("服务器配置只能包含脱敏字段")
    base = json.loads(
        (root / "config/settings.example.json").read_text("utf-8"))
    for key in ("version", "timezone", "schedules", "paths", "arxiv"):
        base[key] = config[key]
    if set(config["scheduler"]) != {"enabled", "start_date"}:
        raise ValueError("服务器调度配置字段不合法")
    base["scheduler"].update(config["scheduler"])
    configuration.validate(root, base)
    return config


def _server(root: Path, exchange: bool) -> dict:
    """服务器只登记任务和获取公共元数据，不调用模型。"""
    path = root / "config/server.json"
    incoming = []
    if exchange:
        raw = sys.stdin.read(5 * 1024 * 1024 + 1)
        if len(raw.encode("utf-8")) > 5 * 1024 * 1024:
            raise ValueError("同步请求超过 5 MiB")
        request = json.loads(raw)
        config = _remote_config(root, request["config"])
        if len(request.get("tasks", [])) > 10000:
            raise ValueError("任务数量超过协议上限")
        incoming = [
            synchronization.task_envelope(task)
            for task in request.get("tasks", [])
        ]
        for task in incoming:
            if task["id"] != task_id(task["kind"], task["scheduled_at"]):
                raise ValueError("任务标识不匹配")
    else:
        config = _remote_config(root, json.loads(path.read_text("utf-8")))
    with runner.lock(root, config):
        if exchange:
            configuration.atomic_json(path, config)
        store = Store(root / config["paths"]["state"] / "queue.sqlite3")
        try:
            store.merge_tasks(incoming, accept_completed=True)
            now = scheduler.now_local(config)
            scheduler.enqueue(store, config, now)
            # SSH 交换仅传任务，元数据抓取留给独立计时器，避免长连接阻塞。
            if not exchange:
                for task in store.export_tasks():
                    if task["kind"] != "papers" or task["status"] != "pending":
                        continue
                    key = f"collected:{task['id']}"
                    if not store.get_setting(key):
                        candidates = papers.collect(root, config)
                        report_path = root / config["paths"][
                            "state"] / "paper-collection-report.json"
                        report = json.loads(report_path.read_text("utf-8"))
                        if not report.get("complete"):
                            raise RuntimeError("服务器论文采集未完整成功；保留待重试")
                        with store.connection:
                            store.connection.execute(
                                "UPDATE tasks SET payload_json=? WHERE id=?",
                                (json.dumps({"candidates": candidates},
                                            ensure_ascii=False), task["id"]))
                        store.set_setting(key, now.isoformat())
            return {
                "config_revision":
                    synchronization.revision(config),
                "tasks": [
                    synchronization.task_envelope(task)
                    for task in store.export_tasks()
                ]
            }
        finally:
            store.close()


def _dispatch(args: argparse.Namespace) -> object:  # pylint: disable=too-many-return-statements
    """执行固定命令；配置或来源错误向调用方返回失败。"""
    root = args.root.resolve()
    if args.command == "daemon":
        from second_proprio import daemon  # pylint: disable=import-outside-toplevel
        daemon.main(root, args.app_executable)
        return {"status": "stopped"}
    if args.command == "desktop-worker":
        from second_proprio import desktop_worker  # pylint: disable=import-outside-toplevel
        desktop_worker.main(args.spec)
        return {"status": "finished"}
    if args.command in {"server-tick", "server-exchange"}:
        return _server(root, args.command == "server-exchange")
    if rpc.socket_path(root).exists():
        if args.command == "status":
            return rpc.call(root, "tasks.list")
        if args.command == "configure":
            current = rpc.call(root, "config.get")
            return rpc.call(
                root, "config.save", {
                    "config": json.loads(args.source.read_text("utf-8")),
                    "expected_revision": current["revision"]
                })
        if args.command == "sync":
            return rpc.call(root, "config.sync")
        if args.command in {"tick", "install-local"}:
            raise RuntimeError("桌面后台已接管调度，请通过 App 管理服务")
        if args.command == "run":
            if not args.kind:
                raise ValueError("后台模式下请明确任务类型")
            if args.retry_failed:
                failures = rpc.call(root, "tasks.list")["tasks"]
                return {
                    "results": [
                        rpc.call(root, "tasks.retry", {"id": task["id"]})
                        for task in failures
                        if task["kind"] == args.kind and
                        task["status"] == "failed"
                    ]
                }
            return rpc.call(
                root, "tasks.submit", {
                    "kind": args.kind,
                    **({
                        "date": str(args.date)
                    } if args.date else {})
                })
        if args.command == "collect":
            return rpc.call(root, "tasks.submit", {"kind": "papers"})
        if args.command == "import-chat":
            return rpc.call(root, "review.import",
                            {"path": str(args.source.resolve())})
        if args.command == "mood":
            cfg = configuration.load(root)
            day = str(args.date or scheduler.now_local(cfg).date())
            old = rpc.call(root, "relationship.get", {"date": day})
            value = old.get("record", old)
            return rpc.call(
                root, "relationship.save", {
                    "date":
                        day,
                    "mood":
                        args.score
                        if args.score is not None else value.get("mood"),
                    "note":
                        args.note or value.get("note", ""),
                    "actions":
                        value.get("actions", []) +
                        ([args.action] if args.action else [])
                })
    config = (configuration.apply(root, args.source)
              if args.command == "configure" else configuration.load(root))
    if args.command == "validate":
        return {"valid": True, "revision": configuration.revision(config)}
    if args.command == "install-local":
        return deployment.install_launchd(root, sys.executable, config)
    if args.command == "render-systemd":
        paths = deployment.render_systemd(root, config)
        target = root / config["paths"]["state"] / "deploy/server.json"
        configuration.atomic_json(target,
                                  synchronization.sanitized_config(config))
        return {
            "files": [str(p) for p in paths] + [str(target)],
            "deployed": False
        }
    if args.command == "status":
        store = Store(root / config["paths"]["state"] / "queue.sqlite3")
        try:
            sync_path = root / config["paths"]["state"] / "sync-status.json"
            return {
                "tasks":
                    store.export_tasks(),
                "sync":
                    json.loads(sync_path.read_text("utf-8"))
                    if sync_path.exists() else {
                        "status": "尚未同步"
                    }
            }
        finally:
            store.close()
    if args.command == "mood":
        now = scheduler.now_local(config)
        day = args.date or now.date()
        path = personal.save_mood(root, config, day, args.score, args.note,
                                  args.action)
        quiet = {**config, "runtime": {**config["runtime"], "notify": False}}
        personal.care(root, quiet, dt.datetime.combine(day, now.timetz()))
        return {"saved": str(path)}
    with runner.lock(root, config):
        store = Store(root / config["paths"]["state"] / "queue.sqlite3")
        try:
            now = scheduler.now_local(config)
            if args.command in {"sync", "configure"}:
                return synchronization.sync(root, config)
            if args.command == "collect":
                return papers.collect(root, config)
            if args.command == "import-chat":
                path = personal.import_chat(root, config, args.source)
                data = json.loads(path.read_text("utf-8"))
                affected = {
                    dt.datetime.fromisoformat(message["timestamp"]).astimezone(
                        now.tzinfo).date() for message in data["messages"]
                }
                for task in store.export_tasks():
                    day = dt.datetime.fromisoformat(
                        task["scheduled_at"]).astimezone(now.tzinfo).date()
                    if task["kind"] == "review" and day in affected:
                        store.finish(task["id"], "pending")
                return {"imported": str(path), "review": "相关复盘将在下一轮处理"}
            sync_result = None
            if args.command == "tick":
                sync_result = synchronization.sync(root, config)
                now = scheduler.now_local(config)
            scheduler.enqueue(store, config, now)
            if args.command == "run" and args.kind:
                selected_day = args.date or now.date()
                schedule_time = sorted(
                    config["schedules"][args.kind]["times"])[0]
                scheduled = dt.datetime.combine(
                    selected_day, dt.time.fromisoformat(schedule_time),
                    now.tzinfo)
                # 手动调用未来时刻的当天任务时，明确以现在为执行时刻。
                scheduled = min(scheduled, now)
                identifier = store.add(args.kind, scheduled.isoformat(),
                                       configuration.revision(config))
                if args.retry_failed:
                    for task in store.export_tasks():
                        if task["kind"] == args.kind and task[
                                "status"] == "failed":
                            store.finish(task["id"], "pending")
                if args.kind in {"care", "review"}:
                    store.finish(identifier, "pending")
            results = []
            if config["runtime"]["auto_run"] or args.command == "run":
                results = runner.consume(root, config, store, now,
                                         getattr(args, "kind", None))
            runner.dashboard(root, config, store, now)
            return {"sync": sync_result, "results": results}
        finally:
            store.close()


def main() -> None:
    """输出机器可读 JSON；故障保留非零退出码，便于调度监控。"""
    args = _parser().parse_args()
    try:
        result = _dispatch(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and any(
                row.get("status") == "failed"
                for row in result.get("results", [])):
            raise SystemExit(1)
    except (OSError, ValueError, RuntimeError,
            jsonschema.ValidationError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        raise SystemExit(1) from error
