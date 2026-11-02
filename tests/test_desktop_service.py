"""验证桌面服务的响应、单项控制、配置和持久恢复语义。"""

import asyncio
import copy
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from second_proprio import config as configuration
from second_proprio import daemon
from second_proprio import rpc
from second_proprio import scheduler


@pytest.fixture(name="service")
def fixture_service():
    """在独立临时工作区建立服务，禁用真实调度与通知。"""
    temporary = tempfile.TemporaryDirectory(  # pylint: disable=consider-using-with
        prefix="sp-service-",
        dir="/tmp")
    tmp_path = Path(temporary.name)
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "config").mkdir()
    for name in ("schema.json", "settings.example.json"):
        shutil.copyfile(root / "config" / name, tmp_path / "config" / name)
    config = configuration.load(tmp_path)
    config["scheduler"]["enabled"] = False
    config["runtime"].update(auto_run=False, notify=False)
    config["server"]["enabled"] = False
    configuration.atomic_json(tmp_path / "config/settings.json", config)
    value = daemon.Service(tmp_path)
    yield value
    value.store.close()
    temporary.cleanup()


def test_retry_targets_one_task_and_resets_only_failure_budget(service):
    """手动重试仅修改选中任务，累计执行历史仍保留。"""

    async def check():
        first = service.submit({"kind": "papers"})["task"]
        again = service.submit({"kind": "papers"})
        assert again["deduplicated"]
        assert again["task"]["id"] == first["id"]
        second = service.submit({"kind": "review"})["task"]
        for task in (first, second):
            service.store.claim(task["id"])
            service.store.finish(task["id"], "failed", error="模拟网络失败")
        await service.dispatch("tasks.retry", {"id": first["id"]})
        assert service.task(first["id"])["failures"] == 0
        assert service.task(first["id"])["attempts"] == 1
        assert service.task(second["id"])["status"] == "failed"
        assert service.task(second["id"])["failures"] == 1

    asyncio.run(check())


def test_waiting_source_does_not_exhaust_failure_budget(service):
    """等待来源的轮询不能消耗模型失败预算。"""
    task = service.submit({"kind": "review"})["task"]
    for _ in range(8):
        service.store.claim(task["id"])
        service.store.finish(task["id"], "waiting_source")
    service.store.finish(task["id"], "failed")
    saved = service.task(task["id"])
    assert saved["attempts"] == 8
    assert saved["failures"] == 1
    assert saved["id"] in [
        row["id"] for row in scheduler.ready_tasks(
            service.store, service.config, scheduler.now_local(service.config))
    ]


@pytest.mark.parametrize("status",
                         ["failed", "waiting_source", "waiting_review"])
def test_attention_acknowledgement_persists_without_completing_task(
        service, status):
    """确认只隐藏一条首页事项，重启后仍保留真实状态、成果与历史。"""
    identifier = service.submit({"kind": "papers"})["task"]["id"]
    other = service.submit({"kind": "review"})["task"]["id"]
    for task_id in (identifier, other):
        service.store.claim(task_id)
        service.store.finish(task_id, status, {"artifacts": ["draft.md"]},
                             "待处理原因", "2026-10-31T08:00:00+08:00")
    before = service.task(identifier)
    events = service.store.events(identifier)
    tasks = asyncio.run(service.dispatch("tasks.list", {}))["tasks"]
    task = next(item for item in tasks if item["id"] == identifier)
    params = {"id": identifier, "attention_version": task["attention_version"]}
    result = asyncio.run(service.dispatch("tasks.acknowledge", params))
    assert result["task"]["attention_dismissed"]
    assert service.task(identifier) == before
    assert service.store.events(identifier) == events
    assert asyncio.run(service.dispatch("tasks.acknowledge", params)) == result
    restarted = daemon.Service(service.root)
    try:
        detail = asyncio.run(
            restarted.dispatch("tasks.detail", {"id": identifier}))
        assert detail["task"]["attention_dismissed"]
        tasks = asyncio.run(restarted.dispatch("tasks.list", {}))["tasks"]
        assert next(item for item in tasks
                    if item["id"] == identifier)["attention_dismissed"]
        assert not next(item for item in tasks
                        if item["id"] == other)["attention_dismissed"]
    finally:
        restarted.store.close()


def test_attention_acknowledgement_ignores_logs_and_polling(service):
    """确认后读取列表、追加日志和更新时间不会让同一事项重新出现。"""
    identifier = service.submit({"kind": "papers"})["task"]["id"]
    service.store.finish(identifier, "waiting_review", {"candidate": "first"})
    task = service.attention_task(service.task(identifier))
    service.acknowledge_attention({
        "id": identifier,
        "attention_version": task["attention_version"]
    })
    service.store.event(identifier, "waiting_review", "补充检查记录")
    with service.store.connection:
        service.store.connection.execute(
            "UPDATE tasks SET updated_at=?,retry_at=? WHERE id=?",
            ("2026-10-30T09:00:00Z", "2026-10-31T09:00:00Z", identifier))
    for _ in range(3):
        tasks = asyncio.run(service.dispatch("tasks.list", {}))["tasks"]
        assert tasks[0]["attention_version"] == task["attention_version"]
        assert tasks[0]["attention_dismissed"]


def test_attention_acknowledgement_does_not_hide_new_failure(service):
    """重试后的新失败重新显示，旧页面不能顺带确认新的失败。"""
    identifier = service.submit({"kind": "papers"})["task"]["id"]
    service.store.claim(identifier)
    service.store.finish(identifier, "failed", error="网络失败")
    task = service.attention_task(service.task(identifier))
    params = {"id": identifier, "attention_version": task["attention_version"]}
    service.acknowledge_attention(params)
    asyncio.run(service.dispatch("tasks.retry", {"id": identifier}))
    pending = service.attention_task(service.task(identifier))
    assert not pending["attention_dismissed"]
    assert pending["attention_version"] == ""
    service.store.claim(identifier)
    service.store.finish(identifier, "failed", error="网络失败")
    current = service.attention_task(service.task(identifier))
    assert current["attention_version"] != task["attention_version"]
    assert not current["attention_dismissed"]
    with pytest.raises(rpc.RpcError, match="已变化"):
        service.acknowledge_attention(params)
    assert not service.attention_task(
        service.task(identifier))["attention_dismissed"]


def test_waiting_source_acknowledgement_survives_automatic_rechecks(service):
    """相同缺失来源经自动补查后仍隐藏，执行次数与临时目录不算新事项。"""
    identifier = service.submit({"kind": "review"})["task"]["id"]
    result = {
        "status": "waiting_source",
        "summary": "请告诉我今天的记录在哪里。",
        "source_errors": [],
        "source_fingerprint": "same-sources",
        "artifacts": ["private/daily/2026-10-30.md"]
    }
    service.store.claim(identifier)
    service.store.finish(identifier, "waiting_source", result)
    original = service.attention_task(service.task(identifier))
    service.acknowledge_attention({
        "id": identifier,
        "attention_version": original["attention_version"]
    })
    for attempt in range(3):
        service.store.claim(identifier)
        service.store.finish(
            identifier, "waiting_source", {
                **result, "run_id": f"retry-{attempt}",
                "workspace": f"/tmp/workspace-{attempt}",
                "draft": f"/tmp/workspace-{attempt}",
                "settings": {
                    "runtime-counter": str(attempt)
                },
                "settings_base": {
                    "runtime-counter": str(attempt - 1)
                }
            })
        with service.store.connection:
            service.store.connection.execute(
                "UPDATE tasks SET finished_at=?,failures=? WHERE id=?",
                (f"2026-10-30T0{attempt}:00:00Z", attempt, identifier))
        current = asyncio.run(service.dispatch("tasks.list", {}))["tasks"][0]
        assert current["attempts"] > original["attempts"]
        assert current["attention_version"] == original["attention_version"]
        assert current["attention_dismissed"]


def test_waiting_source_acknowledgement_detects_changed_evidence(service):
    """来源证据或缺失原因变化后重新显示，旧页面不能确认新事项。"""
    identifier = service.submit({"kind": "review"})["task"]["id"]
    result = {
        "status": "waiting_source",
        "summary": "请告诉我今天的记录在哪里。",
        "source_fingerprint": "first-source"
    }
    service.store.finish(identifier, "waiting_source", result)
    for field, value in [("source_fingerprint", "different-source"),
                         ("summary", "来源无法读取，请检查目录权限。"),
                         ("source_errors", [{
                             "message": "来源文件已损坏"
                         }])]:
        previous = service.attention_task(service.task(identifier))
        params = {
            "id": identifier,
            "attention_version": previous["attention_version"]
        }
        service.acknowledge_attention(params)
        result[field] = value
        service.store.finish(identifier, "waiting_source", result)
        current = service.attention_task(service.task(identifier))
        assert not current["attention_dismissed"]
        assert current["attention_version"] != previous["attention_version"]
        with pytest.raises(rpc.RpcError, match="已变化"):
            service.acknowledge_attention(params)


def test_attention_acknowledgement_rejects_stale_status_and_candidate(service):
    """状态或候选变化都会使旧确认失效，完成的任务也不能再次确认。"""
    identifier = service.submit({"kind": "course"})["task"]["id"]
    service.store.finish(identifier, "waiting_source")
    task = service.attention_task(service.task(identifier))
    params = {"id": identifier, "attention_version": task["attention_version"]}
    service.acknowledge_attention(params)
    for status, result in [("waiting_review", {
            "candidate": "first"
    }), ("waiting_review", {
            "candidate": "second"
    })]:
        service.store.finish(identifier, status, result)
        current = service.attention_task(service.task(identifier))
        assert not current["attention_dismissed"]
        with pytest.raises(rpc.RpcError, match="已变化"):
            service.acknowledge_attention(params)
        params["attention_version"] = current["attention_version"]
        service.acknowledge_attention(params)
    service.store.finish(identifier, "completed")
    with pytest.raises(ValueError, match="没有需要处理"):
        service.acknowledge_attention(params)
    assert service.task(identifier)["status"] == "completed"


def test_attention_acknowledgement_validates_parameters(service):
    """缺少标识、缺少版本和未知任务均不能写入确认记录。"""
    for params in ({}, {
            "id": "unknown"
    }, {
            "id": "unknown",
            "attention_version": "version"
    }, {
            "id": 42,
            "attention_version": "version"
    }):
        with pytest.raises(ValueError):
            asyncio.run(service.dispatch("tasks.acknowledge", params))
    assert not service.store.connection.execute(
        "SELECT 1 FROM settings WHERE key LIKE 'desktop.attention_%'").fetchall(
        )


def test_invalid_and_conflicting_config_preserve_effective_file(service):
    """配置失败不覆盖文件，过期表单不能覆盖外部修改。"""
    before = (service.root / "config/settings.json").read_bytes()
    config = copy.deepcopy(service.config)
    config["schedules"]["papers"]["times"] = ["99:99"]
    with pytest.raises(Exception):
        service.save_config({
            "config": config,
            "expected_revision": configuration.revision(service.config)
        })
    assert (service.root / "config/settings.json").read_bytes() == before
    with pytest.raises(rpc.RpcError, match="其他操作修改"):
        service.save_config({
            "config": service.config,
            "expected_revision": "old"
        })
    config = copy.deepcopy(service.config)
    config["runtime"]["timeout_seconds"] = 600
    result = service.save_config({
        "config": config,
        "expected_revision": configuration.revision(service.config)
    })
    assert result["saved"] and result["effective"] == "next_task"
    assert service.config["runtime"]["timeout_seconds"] == 600


def test_cancellation_is_not_recovered_as_pending(service):
    """明确取消的任务在服务恢复后仍然保持取消状态。"""

    async def check():
        task = service.submit({"kind": "course"})["task"]
        await service.cancel(task["id"])
        await service.recover()
        assert service.task(task["id"])["status"] == "cancelled"
        await service.dispatch("tasks.pause", {"paused": True})
        assert service.paused

    asyncio.run(check())


def test_socket_request_is_same_user_and_nonblocking(service):
    """真实 Unix Socket 可查询与控制，不启动模型或外部网络。"""

    async def check():
        address = rpc.socket_path(service.root)
        server = await asyncio.start_unix_server(service.connection,
                                                 str(address),
                                                 limit=rpc.MAX_MESSAGE + 1)
        address.chmod(0o600)
        async with server:
            value = await asyncio.to_thread(rpc.call, service.root,
                                            "system.status")
            assert value["running"]
            assert value["active_task"] is None
            assert os.stat(address).st_mode & 0o777 == 0o600
            await asyncio.to_thread(rpc.call, service.root, "tasks.pause",
                                    {"paused": True})
            assert service.paused
            with pytest.raises(rpc.RpcError, match="不支持"):
                await asyncio.to_thread(rpc.call, service.root, "shell.execute",
                                        {"command": "bad"})

    asyncio.run(check())


def test_cancel_live_child_retains_draft_and_ui_responds(service, monkeypatch):
    """耗时子进程期间状态查询可响应，取消后保留草稿且不计失败。"""
    from second_proprio import documents  # pylint: disable=import-outside-toplevel

    async def check():
        folder = service.root / "fixture-draft"
        folder.mkdir()
        (folder / "draft.md").write_text("保留的草稿", "utf-8")
        monkeypatch.setattr(
            documents, "create_workspace", lambda *_args: {
                "workspace": str(folder),
                "baseline": {}
            })
        original = asyncio.create_subprocess_exec

        async def fake_worker(*_args, **kwargs):
            return await original(
                sys.executable, "-c",
                "import time; print('ready',flush=True); time.sleep(60)",
                **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_worker)
        task = service.submit({"kind": "papers"})["task"]
        running = asyncio.create_task(service.run_task(task))
        for _ in range(100):
            if service.process:
                break
            await asyncio.sleep(0.01)
        status = await asyncio.wait_for(service.dispatch("system.status", {}),
                                        1)
        assert status["active_task"]["id"] == task["id"]
        await service.cancel(task["id"])
        await asyncio.wait_for(running, 5)
        await asyncio.gather(*list(service.jobs))
        saved = service.task(task["id"])
        assert saved["status"] == "cancelled"
        assert saved["failures"] == 0
        assert (folder / "draft.md").read_text() == "保留的草稿"

    asyncio.run(check())


def test_cancellation_keeps_snapshot_after_worker_cleanup(service, monkeypatch):
    """工作进程清空活动状态后，取消协程仍使用原任务信息清理子进程。"""
    folder = service.root / "cancel-run"
    folder.mkdir()
    workspace = str(service.root / "cancel-workspace")
    configuration.atomic_json(folder / "codex-process.json", {"pid": 42001})
    active = {"id": "cancelled", "folder": str(folder), "workspace": workspace}
    service.active = active
    checked = []
    signals = []

    def matching(pid, expected):
        checked.append((pid, expected))
        return True

    async def finished():
        service.active = None
        service.process = None
        process.returncode = -signal.SIGTERM
        await asyncio.sleep(0)
        return process.returncode

    process = SimpleNamespace(returncode=None,
                              terminate=Mock(),
                              kill=Mock(),
                              wait=AsyncMock(side_effect=finished))
    service.process = process
    monkeypatch.setattr(daemon, "_matching_process", matching)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append(
        (pid, sig)))
    asyncio.run(service.terminate_active(dict(active), process))
    assert service.active is None
    assert service.process is None
    assert checked == [(42001, workspace), (42001, workspace)]
    assert signals == [(42001, signal.SIGTERM), (42001, signal.SIGKILL)]
    process.terminate.assert_called_once()
    process.wait.assert_awaited_once()


def test_cancel_reserved_task_prevents_worker_start(service, monkeypatch):
    """调度已占位但尚未执行时的取消不能被随后启动流程覆盖。"""
    from second_proprio import documents  # pylint: disable=import-outside-toplevel

    async def check():
        task = service.submit({"kind": "review"})["task"]
        service.active = {"id": task["id"], "folder": "", "workspace": ""}
        prepare = Mock(side_effect=AssertionError("已取消任务不应创建草稿"))
        spawn = AsyncMock(side_effect=AssertionError("已取消任务不应创建进程"))
        monkeypatch.setattr(documents, "create_workspace", prepare)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        await service.cancel(task["id"])
        await service.run_task(task)
        await asyncio.gather(*list(service.jobs))
        saved = service.task(task["id"])
        assert saved["status"] == "cancelled"
        assert saved["attempts"] == 0
        assert saved["failures"] == 0
        assert service.active is None
        assert service.process is None
        prepare.assert_not_called()
        spawn.assert_not_awaited()

    asyncio.run(check())


def test_repeated_deferred_cancel_cannot_target_next_task(service, monkeypatch):
    """重复取消只登记一次，延迟执行的取消协程不能终止后续任务。"""

    async def check():
        first = service.submit({"kind": "review"})["task"]
        service.store.claim(first["id"])
        folder = service.root / "deferred-cancel-run"
        folder.mkdir()
        workspace = str(service.root / "original-workspace")
        configuration.atomic_json(folder / "codex-process.json", {"pid": 42002})
        service.active = {
            "id": first["id"],
            "folder": str(folder),
            "workspace": workspace
        }
        original = SimpleNamespace(returncode=None,
                                   terminate=Mock(),
                                   kill=Mock(),
                                   wait=AsyncMock(return_value=0))
        service.process = original
        pending = []
        checked = []
        monkeypatch.setattr(service, "background", pending.append)

        def matching(pid, expected):
            checked.append((pid, expected))
            return True

        signals = []
        monkeypatch.setattr(daemon, "_matching_process", matching)
        monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append(
            (pid, sig)))
        try:
            await service.cancel(first["id"])
            await service.cancel(first["id"])
            assert len(pending) == 1
            assert len([
                event for event in service.store.events(first["id"])
                if event["stage"] == "cancelling"
            ]) == 1
            second = service.submit({"kind": "papers"})["task"]
            service.store.claim(second["id"])
            replacement = SimpleNamespace(returncode=None,
                                          terminate=Mock(),
                                          kill=Mock(),
                                          wait=AsyncMock(return_value=0))
            next_active = {
                "id": second["id"],
                "folder": str(service.root / "next-run"),
                "workspace": str(service.root / "next-workspace")
            }
            service.active = next_active
            service.process = replacement
            await pending.pop()
            assert service.active is next_active
            assert service.process is replacement
            replacement.terminate.assert_not_called()
            replacement.kill.assert_not_called()
            replacement.wait.assert_not_awaited()
            original.terminate.assert_called_once()
            assert checked == [(42002, workspace), (42002, workspace)]
            assert all(pid == 42002 for pid, _ in signals)
            assert service.task(second["id"])["status"] == "running"
        finally:
            for coroutine in pending:
                coroutine.close()

    asyncio.run(check())


@pytest.mark.parametrize("exit_during", ["terminate", "kill"])
def test_cancel_tolerates_process_exiting_before_signal(service, monkeypatch,
                                                        exit_during):
    """进程恰在信号发送前退出时，取消仍等待退出并正常结束。"""
    folder = service.root / "exited-run"
    folder.mkdir()
    active = {"id": "exited", "folder": str(folder), "workspace": str(folder)}

    async def finished():
        process.returncode = 0
        return 0

    process = SimpleNamespace(returncode=None,
                              terminate=Mock(),
                              kill=Mock(),
                              wait=AsyncMock(side_effect=finished))
    getattr(process, exit_during).side_effect = ProcessLookupError()
    if exit_during == "kill":

        async def timeout(awaitable, _timeout):
            awaitable.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(asyncio, "wait_for", timeout)
    asyncio.run(service.terminate_active(active, process))
    assert process.returncode == 0
    process.wait.assert_awaited_once()
    getattr(process, exit_during).assert_called_once()


def test_cancel_during_worker_spawn_stops_returned_process(
        service, monkeypatch):
    """取消发生在创建进程的等待中时，返回的进程仍须退出且草稿保留。"""
    from second_proprio import documents  # pylint: disable=import-outside-toplevel

    async def check():
        folder = service.root / "spawn-draft"
        folder.mkdir()
        draft = folder / "draft.md"
        draft.write_text("创建进程期间的草稿", "utf-8")
        monkeypatch.setattr(
            documents, "create_workspace", lambda *_args: {
                "workspace": str(folder),
                "baseline": {}
            })
        original_spawn = asyncio.create_subprocess_exec
        started = asyncio.Event()
        release = asyncio.Event()
        children = []

        async def delayed_worker(*_args, **kwargs):
            child = await original_spawn(sys.executable, "-c",
                                         "import time; time.sleep(60)",
                                         **kwargs)
            children.append(child)
            started.set()
            await release.wait()
            return child

        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_worker)
        task = service.submit({"kind": "review"})["task"]
        running = asyncio.create_task(service.run_task(task))
        try:
            await asyncio.wait_for(started.wait(), 5)
            assert service.process is None
            await service.cancel(task["id"])
            await asyncio.gather(*list(service.jobs))
            release.set()
            await asyncio.wait_for(running, 5)
            await asyncio.gather(*list(service.jobs))
            saved = service.task(task["id"])
            assert saved["status"] == "cancelled"
            assert saved["failures"] == 0
            assert children[0].returncode is not None
            assert draft.read_text("utf-8") == "创建进程期间的草稿"
        finally:
            release.set()
            for child in children:
                if child.returncode is None:
                    child.kill()
                await child.wait()
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)

    asyncio.run(check())


def test_notification_records_acceptance_not_delivery(service):
    """通知结果只记录系统接受，模拟入口不发送真实通知。"""
    executable = service.root / "notify-fixture"
    executable.write_text(
        f"#!{sys.executable}\nimport sys,json\njson.load(sys.stdin)\n"
        "print(json.dumps({'status':'accepted'}))\n", "utf-8")
    executable.chmod(0o700)
    service.app_executable = str(executable)
    service.config["runtime"]["notify"] = True
    service.notify("first", "测试", "正文")
    asyncio.run(service.deliver_notifications())
    row = service.store.connection.execute(
        "SELECT status FROM notifications WHERE id='first'").fetchone()
    assert row[0] == "accepted"


@pytest.mark.parametrize("output,stderr,code,expected", [
    ("", "", 0, "结构化响应"),
    ("not-json", "", 0, "结构化响应"),
    ('{"accepted":false,"message":"通知权限被拒绝"}', "", 1, "通知权限被拒绝"),
    ('{"error":"系统入口失败"}', "", 0, "系统入口失败"),
    ('{"accepted":true}', "原生入口出错", 1, "原生入口出错"),
    ('{"accepted":"true"}', "", 0, "未接受"),
    ("[]", "", 0, "结构化响应"),
])
def test_notification_requires_explicit_success(service, output, stderr, code,
                                                expected):
    """空响应、错误响应和非零退出均失败，并保留入口报告的具体原因。"""
    executable = service.root / "notify-fixture"
    executable.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdin.read()\n"
        f"sys.stdout.write({output!r})\nsys.stderr.write({stderr!r})\n"
        f"sys.exit({code})\n", "utf-8")
    executable.chmod(0o700)
    service.app_executable = str(executable)
    service.config["runtime"]["notify"] = True
    service.notify("fixture", "测试", "正文")
    asyncio.run(service.deliver_notifications())
    row = service.store.connection.execute(
        "SELECT status,error FROM notifications WHERE id='fixture'").fetchone()
    assert row["status"] == "failed"
    assert expected in row["error"]
    assert len(row["error"]) <= 300
    assert not service.notifying


def test_notification_spawn_error_is_recorded(service):
    """通知入口无法执行时记录启动原因，不静默丢弃失败。"""
    executable = service.root / "notify-not-executable"
    executable.write_text("不可执行", "utf-8")
    service.app_executable = str(executable)
    service.config["runtime"]["notify"] = True
    service.notify("fixture", "测试", "正文")
    asyncio.run(service.deliver_notifications())
    row = service.store.connection.execute(
        "SELECT status,error FROM notifications WHERE id='fixture'").fetchone()
    assert row["status"] == "failed"
    assert "启动或通信失败" in row["error"]
    assert not service.notifying


def test_publication_counter_delta_is_idempotent(service):
    """候选采纳与另一个任务发布不会丢失计数，也不会重复计数。"""
    key = "paper_count:2026-10-30"
    first = {
        "status": "completed",
        "publication_id": "one",
        "settings": {
            key: "1"
        },
        "settings_base": {
            key: "0"
        }
    }
    second = {
        "status": "completed",
        "publication_id": "two",
        "settings": {
            key: "1"
        },
        "settings_base": {
            key: "0"
        }
    }
    service.apply_publication(first)
    service.apply_publication(second)
    service.apply_publication(first)
    assert service.store.get_setting(key) == "2"


def test_schedule_change_preserves_unaffected_pending_tasks(service):
    """调整课程时间不会把尚未执行的论文任务一并跳过。"""
    paper = service.store.add("papers", "2026-10-30T12:30:00+08:00", "old")
    course = service.store.add("course", "2026-10-30T08:00:00+08:00", "old")
    candidate = copy.deepcopy(service.config)
    candidate["schedules"]["course"]["times"] = ["09:00"]
    service.save_config({
        "config": candidate,
        "expected_revision": configuration.revision(service.config)
    })
    assert service.task(paper)["status"] == "pending"
    assert service.task(course)["status"] == "skipped"


def test_recover_retains_interrupted_workspace(service):
    """中断后恢复任务保留可继续执行的草稿位置。"""
    task = service.submit({"kind": "papers"})["task"]
    service.store.claim(task["id"])
    workspace = service.state / "draft-runs/fixture/workspace"
    workspace.mkdir(parents=True)
    configuration.atomic_json(service.state / "desktop-active.json", {
        "id": task["id"],
        "workspace": str(workspace),
        "run_id": "fixture"
    })
    asyncio.run(service.recover())
    recovered = service.task(task["id"])
    assert recovered["status"] == "pending"
    assert recovered["result"]["workspace"] == str(workspace)
    assert recovered["result"]["run_id"] == "fixture"


def test_same_candidate_input_skips_model_before_spawn(service, monkeypatch):
    """已放弃或待采纳的相同自动输入不再调用模型。"""
    from second_proprio import documents  # pylint: disable=import-outside-toplevel
    task_id = service.store.add("course", "2026-10-30T08:00:00+08:00", "test")
    task = service.task(task_id)
    baseline = {"courses/01.md": "fixture-hash"}
    workspace = service.state / "draft-runs/fixture/workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(
        documents, "create_workspace", lambda *_args: {
            "workspace": str(workspace),
            "baseline": baseline
        })
    manifest = documents.DocumentStore(service.root, service.config).directory
    configuration.atomic_json(
        manifest / "publications/existing/manifest.json", {
            "id": "existing",
            "status": "rejected",
            "input_fingerprint": daemon.input_fingerprint(task, baseline)
        })
    asyncio.run(service.run_task(task))
    result = service.task(task_id)
    assert result["status"] == "skipped"
    assert result["result"]["candidate_id"] == "existing"
    assert service.process is None


def test_failed_recovery_keeps_active_publication_marker(service, monkeypatch):
    """发布恢复失败保留活动标识，下次启动仍能重放同一事务。"""
    from second_proprio import documents  # pylint: disable=import-outside-toplevel
    marker = service.state / "desktop-active.json"
    configuration.atomic_json(marker, {"run_id": "fixture"})

    def conflict(*_args):
        raise ValueError("模拟发布冲突")

    monkeypatch.setattr(documents, "recover_publications", conflict)
    with pytest.raises(ValueError, match="发布冲突"):
        asyncio.run(service.recover())
    assert marker.exists()


def test_accept_commit_recovered_without_active_worker(service):
    """采纳已写入正文但队列未确认时，只补交对应待采纳任务。"""
    from second_proprio import documents  # pylint: disable=import-outside-toplevel
    task = service.submit({"kind": "papers"})["task"]
    service.store.finish(task["id"], "waiting_review", {"candidate_id": "one"})
    later = service.submit({"kind": "review"})["task"]
    service.store.finish(later["id"], "pending", {"candidate_id": "two"})
    directory = documents.DocumentStore(service.root, service.config).directory
    key = "paper_count:2026-10-30"
    for identifier, target in (("one", task), ("two", later)):
        configuration.atomic_json(
            directory / "publications" / identifier / "manifest.json", {
                "id": identifier,
                "task_id": target["id"],
                "status": "published",
                "files": [],
                "settings": {
                    key: "1"
                },
                "settings_base": {
                    key: "0"
                }
            })
    asyncio.run(service.recover())
    assert service.task(task["id"])["status"] == "completed"
    assert service.task(later["id"])["status"] == "pending"
    assert service.store.get_setting(key) == "1"
    asyncio.run(service.recover())
    assert service.store.get_setting(key) == "1"


def test_config_sync_tracks_current_public_config_and_inflight(service):
    """旧同步成功不会伪装成新配置已同步，开关同样参与公开版本。"""
    service.config["server"]["enabled"] = True
    original = service.config_result()["sync"]["current_revision"]
    configuration.atomic_json(service.state / "sync-status.json", {
        "status": "ok",
        "remote_revision": original
    })
    service.config["schedules"]["course"]["enabled"] = False
    service.syncing = True
    state = service.config_result()["sync"]
    assert state["remote_revision"] == original
    assert state["current_revision"] != original
    assert state["in_progress"]
    service.config["server"]["enabled"] = False
    assert service.config_result()["sync"]["status"] == "disabled"


def test_status_and_single_task_do_not_decode_entire_history(
        service, monkeypatch):
    """高频健康查询与单任务控制不重复读取所有历史任务正文。"""
    task = service.submit({"kind": "papers"})["task"]

    def unexpected():
        pytest.fail("不应全量解码历史任务")

    monkeypatch.setattr(service.store, "export_tasks", unexpected)
    assert service.task(task["id"]) == task
    assert asyncio.run(service.dispatch("system.status", {}))["counts"] == {
        "pending": 1
    }
    with pytest.raises(ValueError, match="任务不存在"):
        service.store.get("missing")


def test_sync_setup_failure_releases_slot_and_later_sync_can_run(
        service, monkeypatch):
    """同步快照准备失败也释放状态，磁盘恢复后可以重新同步。"""
    original = Path.mkdir

    def fail_snapshot(path, *args, **kwargs):
        if "desktop-sync" in path.parts:
            raise OSError("模拟快照目录不可写")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_snapshot)
    asyncio.run(service.synchronize())
    assert not service.syncing
    status_path = service.state / "sync-status.json"
    assert json.loads(status_path.read_text())["status"] == "error"
    monkeypatch.setattr(Path, "mkdir", original)
    monkeypatch.setattr(daemon.sync, "sync", lambda *_args: {"status": "ok"})
    asyncio.run(service.synchronize())
    assert not service.syncing
    assert json.loads(status_path.read_text())["status"] == "ok"


def test_run_directory_failure_does_not_stall_task_slot(service, monkeypatch):
    """启动目录创建失败进入失败预算，下一任务仍可获得执行槽。"""
    original = Path.mkdir

    def fail_run(path, *args, **kwargs):
        if "desktop-runs" in path.parts:
            raise OSError("模拟运行目录不可写")
        return original(path, *args, **kwargs)

    task = service.submit({"kind": "papers"})["task"]
    service.active = {"id": task["id"], "folder": "", "workspace": ""}
    monkeypatch.setattr(Path, "mkdir", fail_run)
    asyncio.run(service.run_task(task))
    assert service.active is None
    assert service.process is None
    saved = service.task(task["id"])
    assert saved["status"] == "failed"
    assert saved["attempts"] == saved["failures"] == 1
    assert "不可写" in saved["last_error"]


def test_stop_checks_reserved_slot_and_preserves_pause_preference(service):
    """停止检查包含尚未启动的预留任务，空闲退出不改变暂停偏好。"""
    task = service.submit({"kind": "papers"})["task"]
    service.active = {"id": task["id"], "folder": "", "workspace": ""}
    with pytest.raises(rpc.RpcError, match="正在运行"):
        asyncio.run(service.dispatch("system.stop", {"cancel_current": False}))
    assert not service.stopping
    service.active = None
    assert asyncio.run(service.dispatch("system.stop", {}))["stopping"]
    assert not service.paused
    asyncio.run(service.loop())
    assert service.task(task["id"])["status"] == "pending"


@pytest.mark.parametrize("response", [
    None, [], "bad", {}, {
        "error": None
    }, {
        "error": {
            "code": 1,
            "message": []
        }
    }
])
def test_rpc_rejects_malformed_response_as_protocol_error(service, response):
    """异常 JSON 响应返回明确协议错误，不泄露属性或索引异常。"""

    async def check():

        async def respond(reader, writer):
            request = json.loads(await reader.readline())
            value = response
            if isinstance(value, dict):
                value = {"id": request["id"], **value}
            writer.write((json.dumps(value) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(
            respond, str(rpc.socket_path(service.root)))
        async with server:
            with pytest.raises(rpc.RpcError) as caught:
                await asyncio.to_thread(rpc.call, service.root, "system.status")
            assert caught.value.code == "protocol"

    asyncio.run(check())
