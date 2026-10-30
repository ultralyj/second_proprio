"""验证桌面服务的响应、单项控制、配置和持久恢复语义。"""

import asyncio
import copy
import os
from pathlib import Path
import shutil
import sys
import tempfile

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
    shutil.copytree(root / "config", tmp_path / "config")
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
