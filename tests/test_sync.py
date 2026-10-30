"""验证任务同步的隐私边界、失败保留与协议校验。"""

import copy
import json
import shlex
import subprocess

import pytest

from second_proprio import sync


@pytest.fixture(name="config")
def config_fixture():
    """提供包含明确私密诱饵的最小同步配置。"""
    return {
        "version": 1,
        "timezone": "Asia/Shanghai",
        "paths": {
            "state": ".state",
            "private": "private"
        },
        "scheduler": {
            "enabled": True,
            "start_date": "2026-10-29"
        },
        "schedules": {
            kind: {
                "weekdays": list(range(7)),
                "times": ["08:00"]
            } for kind in ("papers", "course", "review", "care")
        },
        "server": {
            "enabled": True,
            "ssh_host": "tencent-vps",
            "root": "/home/ubuntu/second-proprio",
            "connect_timeout_seconds": 8,
        },
        "arxiv": {
            "lookback_days":
                7,
            "max_papers_per_day":
                3,
            "request_interval_seconds":
                3.1,
            "topics": [{
                "id": "dexterous",
                "name": "灵巧手",
                "keywords": ["hand"],
                "categories": ["cs.RO"],
                "exclude_keywords": [],
                "private_note": "不上传的研究私记",
            }],
        },
        "relationship": {
            "mood": "不上传的心情",
            "note": "私密关系记录"
        },
        "model": {
            "api_key": "不要上传此密钥"
        },
        "review": {
            "source": "/private/chat-history.json"
        },
    }


def task(kind="review"):
    """生成同时包含公有字段和私密字段的任务。"""
    return {
        "id": sync.task_id(kind, "2026-10-29T22:30:00+08:00"),
        "kind": kind,
        "scheduled_at": "2026-10-29T22:30:00+08:00",
        "config_revision": "local-revision",
        "status": "pending",
        "payload": {
            "notes": "private notes"
        },
        "result": {
            "body": "private result"
        },
        "last_error": "private traceback",
    }


class FakeStore:
    """记录同步行为而不依赖真实 SQLite。"""

    def __init__(self, tasks):
        self.tasks = tasks
        self.merged = None

    def export_tasks(self):
        """返回待导出的任务。"""
        return copy.deepcopy(self.tasks)

    def merge_tasks(self, tasks):
        """记录接收到的安全信封。"""
        self.merged = tasks

    def close(self):
        """模拟关闭连接。"""


def test_config_and_task_redaction(config):
    """同步只传白名单配置与元数据，不传个人内容。"""
    public = sync.sanitized_config(config)
    encoded = json.dumps(public, ensure_ascii=False)
    assert "不上传" not in encoded
    assert "api_key" not in encoded
    assert "chat-history" not in encoded
    assert "relationship" not in public
    assert public["arxiv"]["topics"][0]["categories"] == ["cs.RO"]
    envelope = sync.task_envelope(task())
    assert not envelope["payload"]
    assert "result" not in envelope
    assert "last_error" not in envelope


def test_public_paper_fields_exclude_paths_and_instructions():
    """公共论文元数据允许传输，路径和执行指令不进入协议。"""
    sample = task("papers")
    sample["payload"] = {
        "candidates": [{
            "id": "2601.00001",
            "topic_id": "dexterous-manipulation",
            "title": "Dexterous hand",
            "version": 1,
            "categories": ["cs.RO"],
            "paper_dir": "/private/local/path",
            "command": "touch /tmp/unexpected",
            "api_key": "secret",
        }]
    }
    result = sync.task_envelope(sample)
    assert result["payload"]["candidates"] == [{
        "id": "2601.00001",
        "topic_id": "dexterous-manipulation",
        "title": "Dexterous hand",
        "version": 1,
        "categories": ["cs.RO"],
    }]


def test_success_sends_safe_json_and_quoted_command(tmp_path, config,
                                                    monkeypatch):
    """远端路径由 shell 引用保护，输出先校验再合并。"""
    config["server"]["root"] = "/home/ubuntu/project $(touch nope)"
    store = FakeStore([task()])
    monkeypatch.setattr(sync, "Store", lambda _: store)
    observed = {}
    desired = sync.revision(sync.sanitized_config(config))

    def run(command, **kwargs):
        observed.update({"command": command, "input": kwargs["input"]})
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({
                "config_revision": desired,
                "tasks": [task()]
            }),
            "",
        )

    monkeypatch.setattr(sync.subprocess, "run", run)
    state = sync.sync(tmp_path, config)
    assert state["status"] == "ok"
    assert state["remote_revision"] == desired
    assert "private notes" not in observed["input"]
    command = observed["command"]
    assert command[-2] == "tencent-vps"
    remote_args = shlex.split(command[-1])
    assert remote_args[0] == (
        "/home/ubuntu/project $(touch nope)/.venv/bin/python")
    assert remote_args[-2] == "/home/ubuntu/project $(touch nope)"
    assert store.merged == [sync.task_envelope(task())]


def test_failure_keeps_confirmed_remote_revision(tmp_path, config, monkeypatch):
    """SSH 失败后不能把待上传配置标记为服务器已生效。"""
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    (state_dir / "sync-status.json").write_text(
        json.dumps({
            "remote_revision": "previous-server-revision",
            "last_success": "2026-10-28T00:00:00+00:00",
        }))
    store = FakeStore([task()])
    monkeypatch.setattr(sync, "Store", lambda _: store)
    monkeypatch.setattr(
        sync.subprocess, "run",
        lambda command, **_: subprocess.CompletedProcess(
            command, 255, "", "Permission denied (publickey)."))
    state = sync.sync(tmp_path, config)
    assert state["status"] == "error"
    assert state["remote_revision"] == "previous-server-revision"
    assert state["last_success"] == "2026-10-28T00:00:00+00:00"
    assert state["desired_revision"] != state["remote_revision"]
    assert store.merged is None


@pytest.mark.parametrize("invalid_response", [
    {
        "config_revision": "stale",
        "tasks": []
    },
    {
        "tasks": []
    },
    ["not an object"],
])
def test_invalid_responses_are_not_merged(tmp_path, config, monkeypatch,
                                          invalid_response):
    """无效响应不能改变本机队列。"""
    store = FakeStore([])
    monkeypatch.setattr(sync, "Store", lambda _: store)
    monkeypatch.setattr(
        sync.subprocess, "run",
        lambda command, **_: subprocess.CompletedProcess(
            command, 0, json.dumps(invalid_response), ""))
    state = sync.sync(tmp_path, config)
    assert state["status"] == "error"
    assert store.merged is None


@pytest.mark.parametrize("key,value", [
    ("kind", "shell"),
    ("status", "execute"),
    ("scheduled_at", "2026-10-29T12:00:00"),
    ("id", ""),
])
def test_task_protocol_rejects_invalid_fields(key, value):
    """未知任务或无时区时间不能作为有效工作项接收。"""
    sample = task()
    sample[key] = value
    with pytest.raises(ValueError):
        sync.task_envelope(sample)


def test_disabled_server_does_not_launch_ssh(tmp_path, config, monkeypatch):
    """未启用服务器时不尝试联网。"""
    config["server"]["enabled"] = False

    def forbidden(*_args, **_kwargs):
        pytest.fail("禁用状态不应启动 SSH")

    monkeypatch.setattr(sync.subprocess, "run", forbidden)
    assert sync.sync(tmp_path, config)["status"] == "disabled"


def test_option_like_host_is_rejected(tmp_path, config, monkeypatch):
    """SSH 别名不能被解析成 ssh 命令行选项。"""
    config["server"]["ssh_host"] = "-oProxyCommand=touch /tmp/nope"

    def forbidden(*_args, **_kwargs):
        pytest.fail("无效主机不能启动 SSH")

    monkeypatch.setattr(sync.subprocess, "run", forbidden)
    assert sync.sync(tmp_path, config)["status"] == "error"


@pytest.mark.parametrize("key,value", [
    ("id", None),
    ("id", "../escape"),
    ("id", "2601.00001v1"),
    ("topic_id", None),
    ("topic_id", "../../private"),
    ("version", None),
    ("version", 0),
    ("version", -1),
    ("version", True),
    ("version", "1"),
    ("title", 42),
    ("categories", "cs.RO"),
])
def test_public_metadata_rejects_missing_or_invalid_identity(key, value):
    """坏候选在交换阶段拒绝，不能留到论文执行器才崩溃。"""
    candidate = {
        "id": "2601.00001",
        "topic_id": "dexterous",
        "version": 1,
    }
    if value is None:
        candidate.pop(key, None)
    else:
        candidate[key] = value
    sample = task("papers")
    sample["payload"] = {"candidates": [candidate]}
    with pytest.raises(ValueError):
        sync.task_envelope(sample)


def test_batch_identity_validation_happens_before_any_merge(
        tmp_path, config, monkeypatch):
    """后一个任务 ID 造假时，前一个有效任务也不能先行合并。"""
    store = FakeStore([])
    monkeypatch.setattr(sync, "Store", lambda _: store)
    incorrect = task("course")
    incorrect["id"] = "does-not-match-scheduled-time"
    response = {
        "config_revision": sync.revision(sync.sanitized_config(config)),
        "tasks": [task(), incorrect],
    }
    monkeypatch.setattr(
        sync.subprocess, "run",
        lambda command, **_: subprocess.CompletedProcess(
            command, 0, json.dumps(response), ""))
    result = sync.sync(tmp_path, config)
    assert result["status"] == "error"
    assert store.merged is None
