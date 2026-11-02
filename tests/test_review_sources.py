"""验证配置来源、真实时间筛选、Codex 日志隔离与源更新防抖。"""

import asyncio
import copy
import datetime as dt
import json
from pathlib import Path
import shutil

import pytest

from second_proprio import config as configuration
from second_proprio import daemon
from second_proprio import desktop_content
from second_proprio import documents
from second_proprio import personal
from second_proprio import review_sources
from second_proprio import runner
from second_proprio.store import Store


@pytest.fixture(name="project")
def fixture_project(tmp_path: Path) -> tuple[Path, dict]:
    """仅复制公开模板，绝不使用正式用户配置或真实聊天来源。"""
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    for name in ("schema.json", "settings.example.json"):
        shutil.copyfile(repository / "config" / name, root / "config" / name)
    config = configuration.load(root)
    config["scheduler"]["enabled"] = False
    config["server"]["enabled"] = False
    config["runtime"].update(auto_run=False, notify=False)
    configuration.atomic_json(root / "config/settings.json", config)
    return root, config


def _message(text: str, stamp: str = "2026-10-29T17:00:00Z") -> dict:
    """构造按北京时间属于次日的消息。"""
    return {
        "role": "user",
        "timestamp": stamp,
        "text": text,
        "conversation_id": "fixture"
    }


def _snapshot(path: Path, text: str) -> None:
    """写入仅用于测试的规范化快照。"""
    configuration.atomic_json(path, {"messages": [_message(text)]})


def test_external_export_uses_message_time_and_active_branch(project, tmp_path):
    """外部原生导出无需导入，选当前分支并按消息时间而非文件日期归档。"""
    root, config = project
    source = tmp_path / "export"
    source.mkdir()
    config["review"]["source_directory"] = str(source)
    stamp = dt.datetime(2026, 10, 29, 17, tzinfo=dt.timezone.utc).timestamp()

    def node(text, parent=None):
        return {
            "parent": parent,
            "message": {
                "create_time": stamp,
                "author": {
                    "role": "user"
                },
                "content": {
                    "parts": [text]
                }
            }
        }

    path = source / "conversations.json"
    configuration.atomic_json(path, [{
        "id": "chat",
        "current_node": "chosen",
        "mapping": {
            "root": node("开始"),
            "chosen": node("实际完成", "root"),
            "discarded": node("废弃分支", "root")
        }
    }])
    before = path.read_bytes()
    report = review_sources.scan(root, config, dt.date(2026, 10, 30))
    assert [message["text"] for message in report["messages"]] == ["开始", "实际完成"]
    assert not review_sources.scan(root, config, dt.date(2026, 10,
                                                         29))["messages"]
    assert path.read_bytes() == before
    assert not (root / "private").exists()


def test_sources_dedupe_report_errors_without_text(project, tmp_path):
    """重复快照去重，单个坏文件不遮蔽可用记录，诊断不泄露正文。"""
    root, config = project
    source = tmp_path / "export"
    source.mkdir()
    config["review"]["source_directory"] = str(source)
    _snapshot(source / "a.json", "私密诱饵")
    _snapshot(source / "b.json", "私密诱饵")
    (source / "bad.json").write_text("{broken", "utf-8")
    report = review_sources.scan(root, config)
    assert len(report["messages"]) == 1
    assert report["errors"][0]["source"].endswith("bad.json")
    diagnostics = review_sources.diagnostics(root, config)
    assert "messages" not in diagnostics
    assert "私密诱饵" not in json.dumps(diagnostics, ensure_ascii=False)


def test_import_and_manual_records_never_write_external_directory(
        project, tmp_path):
    """配置外部来源后，导入和手写补充仍只写入应用私密目录。"""
    root, config = project
    source = tmp_path / "external"
    source.mkdir()
    config["review"]["source_directory"] = str(source)
    file = tmp_path / "export.json"
    _snapshot(file, "新导入")
    imported = personal.import_chat(root, config, file)
    assert imported.is_relative_to(root / "private/chat-imports")
    desktop_content.add_review_source(root, config, {
        "date": "2026-10-30",
        "text": "手动补充"
    })
    assert not list(source.iterdir())
    assert len(review_sources.scan(root, config)["messages"]) == 2


def test_workspace_uses_frozen_target_day_without_external_access(
        project, tmp_path):
    """模型只接收目标日快照，外部记录后续更新不改变运行中的输入。"""
    root, config = project
    source = tmp_path / "external"
    source.mkdir()
    config["review"]["source_directory"] = str(source)
    file = source / "source.json"
    _snapshot(file, "初始记录")
    prepared = documents.create_workspace(root, config, "review-external", {
        "kind": "review",
        "payload": {
            "date": "2026-10-30"
        }
    })
    workspace = Path(prepared["workspace"])
    worker_config = prepared["worker_config"]
    assert worker_config["review"][
        "source_directory"] == "private/review-inputs"
    _snapshot(file, "稍后修改")
    context = personal.review_context(workspace, worker_config,
                                      dt.date(2026, 10, 30))
    evidence = json.loads((workspace / context["context"]).read_text())
    assert evidence["messages"][0]["text"] == "初始记录"
    assert evidence["sources"] == [str(file)]
    assert not (workspace / "private/chat-imports").exists()


def test_missing_source_stays_waiting(project):
    """没有目录或目标日记录时仍等待来源，不生成虚构复盘。"""
    root, config = project
    context = personal.review_context(root, config, dt.date(2026, 10, 30))
    assert context["status"] == "waiting_source"
    assert "请告诉我今天的记录在哪里。" in context["summary"]
    assert "来源目录不存在" in context["source_errors"][0]["message"]


def _rollout(path: Path, source="vscode", *, parent=None) -> None:
    """构造含不同角色、事件和跨日消息的最小日志。"""
    metadata = {"source": source, "id": "session", "cwd": "/tmp/interactive"}
    if parent:
        metadata["parent_thread_id"] = parent
    events = [{"type": "session_meta", "payload": metadata}]
    for role, phase, text in (("user", None, "真实用户记录"), ("assistant",
                                                         "commentary", "忽略进度"),
                              ("assistant", "final_answer",
                               "最终回答"), ("developer", None, "忽略开发指令")):
        events.append({
            "type": "response_item",
            "timestamp": "2026-10-29T17:00:00Z",
            "payload": {
                "type":
                    "message",
                "role":
                    role,
                "phase":
                    phase,
                "content": [{
                    "type": "input_text" if role == "user" else "output_text",
                    "text": text
                }]
            }
        })
    events.extend([
        {
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "encrypted_content": "不能导入"
            }
        },
        {
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "text": "不能导入"
            }
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "output": "不能导入"
            }
        },
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in events) +
        "\n", "utf-8")


def test_codex_stream_filters_agents_tools_and_partial_tail(project, tmp_path):
    """Codex 仅读主会话用户/最终回答，按真实跨日时间筛选。"""
    root, config = project
    source = tmp_path / "sessions"
    config["review"]["source_directory"] = str(source)
    good = source / "2026/10/29/rollout-main.jsonl"
    _rollout(good)
    _rollout(source / "rollout-child.jsonl",
             source={"subagent": {
                 "thread_spawn": {}
             }})
    _rollout(source / "rollout-parent.jsonl", parent="root")
    _rollout(source / "rollout-exec.jsonl", source="exec")
    with good.open("a", encoding="utf-8") as stream:
        stream.write('broken-complete-line\n{"partial":')
    report = review_sources.scan(root, config, dt.date(2026, 10, 30))
    assert sorted(message["text"] for message in report["messages"]) == [
        "最终回答", "真实用户记录"
    ]
    assert report["source_kinds"] == ["Codex 本机对话"]
    assert len(report["warnings"]) == 1
    assert not review_sources.scan(root, config, dt.date(2026, 10,
                                                         29))["messages"]


def test_review_source_changes_are_due_debounced_and_idempotent(
        project, monkeypatch):
    """已完成复盘只在到期且来源稳定后登记一次，取消不会被轮询重开。"""
    root, config = project
    config["review"]["source_debounce_seconds"] = 60
    configuration.atomic_json(root / "config/settings.json", config)
    source = root / "private/chat-imports/test.json"
    _snapshot(source, "原始来源")
    service = daemon.Service(root)
    clock = [0.0]
    now = dt.datetime.fromisoformat("2026-10-30T20:00:00+08:00")
    monkeypatch.setattr(daemon.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(daemon.scheduler, "now_local", lambda _cfg: now)
    task = service.submit({"kind": "review", "date": "2026-10-30"})["task"]
    service.store.finish(task["id"], "completed",
                         {"review_source_fingerprint": "old"})
    try:
        asyncio.run(service.refresh_review_sources())
        clock[0] = 61
        asyncio.run(service.refresh_review_sources())
        assert len(service.store.export_tasks()) == 1
        now = now.replace(hour=23)
        asyncio.run(service.refresh_review_sources())
        assert len(service.store.export_tasks()) == 2
        asyncio.run(service.refresh_review_sources())
        assert len(service.store.export_tasks()) == 2
        rebuilt = service.store.export_tasks()[-1]
        service.store.finish(rebuilt["id"], "cancelled")
        asyncio.run(service.refresh_review_sources())
        assert service.task(rebuilt["id"])["status"] == "cancelled"
        assert len(service.store.export_tasks()) == 2
    finally:
        service.store.close()


def test_config_accepts_external_source_without_moving_output_paths(
        project, tmp_path):
    """保存外部只读来源不需要移动已有私密输出目录。"""
    root, _config = project
    service = daemon.Service(root)
    try:
        candidate = copy.deepcopy(service.config)
        candidate["review"]["source_directory"] = str(tmp_path /
                                                      "not-yet-exported")
        saved = service.save_config({
            "config": candidate,
            "expected_revision": configuration.revision(service.config)
        })
        assert saved["saved"]
        assert saved["config"]["paths"]["private"] == "private"
    finally:
        service.store.close()


def test_review_candidate_preserves_source_fingerprint_on_accept(project):
    """人工修改的复盘变为候选，采纳后仍保留对应来源版本。"""
    root, config = project
    _snapshot(root / "private/chat-imports/source.json", "来源")
    path = root / "private/daily/2026-10-30.md"
    path.parent.mkdir(parents=True)
    path.write_text("用户维护的原文", "utf-8")
    prepared = documents.create_workspace(root, config, "review-candidate", {
        "kind": "review",
        "payload": {
            "date": "2026-10-30"
        }
    })
    (Path(prepared["workspace"]) / "private/daily/2026-10-30.md").write_text(
        "有新来源的复盘", "utf-8")
    result = documents.publish_workspace(
        root, config, "review-candidate", prepared["workspace"],
        prepared["baseline"], {
            "task_id": "fixture",
            "review_source_fingerprint": "source-v2",
            "review_date": "2026-10-30"
        })
    assert result["status"] == "waiting_review"
    assert path.read_text() == "用户维护的原文"
    assert result["review_source_fingerprint"] == "source-v2"
    accepted = documents.DocumentStore(root,
                                       config).accept(result["candidate_id"],
                                                      documents.file_hash(path))
    assert accepted["review_source_fingerprint"] == "source-v2"
    assert accepted["review_date"] == "2026-10-30"


def test_rollout_cache_updates_in_place_and_excludes_missing_time(
        project, tmp_path):
    """同一活动日志变更只保留最新缓存，消息没有时间不能使用 mtime。"""
    root, config = project
    folder = tmp_path / "sessions"
    config["review"]["source_directory"] = str(folder)
    path = folder / "rollout-cache.jsonl"
    _rollout(path)
    before = review_sources.scan(root, config)
    assert len(before["messages"]) == 2
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": "无时间不能归档"
                    }]
                }
            }) + "\n")
    after = review_sources.scan(root, config)
    assert len(after["messages"]) == 2
    assert any("缺少有效时间" in item["message"] for item in after["warnings"])
    assert after["source_fingerprint"] == before["source_fingerprint"]


def test_codex_fingerprint_survives_workspace_and_execution(
        project, tmp_path, monkeypatch):
    """Codex 来源经过快照再解析及生成，指纹与来源类型仍和原始扫描一致。"""
    root, config = project
    folder = tmp_path / "sessions"
    config["review"]["source_directory"] = str(folder)
    _rollout(folder / "rollout-main.jsonl")
    day = dt.date(2026, 10, 30)
    original = review_sources.scan(root, config, day)
    task = {
        "kind": "review",
        "status": "pending",
        "payload": {
            "date": str(day)
        },
        "scheduled_at": "2026-10-30T22:30:00+08:00"
    }
    prepared = documents.create_workspace(root, config, "fingerprint-stable",
                                          task)
    workspace = Path(prepared["workspace"])
    worker_config = prepared["worker_config"]
    context = personal.review_context(workspace, worker_config, day)
    assert context["source_fingerprint"] == original["source_fingerprint"]
    evidence = json.loads((workspace / context["context"]).read_text())
    assert evidence["source_kinds"] == ["Codex 本机对话"]
    assert all(
        message["source"] == "Codex 本机对话" for message in evidence["messages"])

    def generated(_root, _config, _prompt, _label):
        (workspace / context["output"]).write_text("基于真实消息生成的测试复盘", "utf-8")
        return {
            "status": "completed",
            "summary": "测试完成",
            "artifacts": [context["output"]]
        }

    monkeypatch.setattr(runner, "codex", generated)
    store = Store(workspace / ".state/queue.sqlite3")
    try:
        result = runner.execute(workspace, worker_config, store, task,
                                dt.datetime.fromisoformat(task["scheduled_at"]))
        assert result["review_source_fingerprint"] == original[
            "source_fingerprint"]
    finally:
        store.close()


@pytest.mark.parametrize("existing_status", ["completed", "waiting_review"])
def test_matching_review_settles_waiting_source_duplicate(
        project, existing_status):
    """已有同来源成果或候选时销账重复等待，不触发模型或伪造新成果。"""
    root, config = project
    source = root / "private/chat-imports/test.json"
    _snapshot(source, "已纳入当天复盘的来源")
    fingerprint = review_sources.scan(
        root, config)["dates"]["2026-10-30"]["fingerprint"]
    service = daemon.Service(root)
    original = service.store.add("review", "2026-10-30T22:30:00+08:00", "test")
    service.store.finish(original, "waiting_source",
                         {"summary": "请告诉我今天的记录在哪里。"})
    completed = service.store.add("review", "2026-10-30T23:00:00+08:00", "test")
    result = {
        "review_source_fingerprint": fingerprint,
        "artifacts": ["private/daily/2026-10-30.md"],
        "publication_id": "publication"
    }
    if existing_status == "waiting_review":
        result.update(candidate_id="candidate", artifacts=[])
    service.store.finish(completed, existing_status, result)
    service.store.set_setting("desktop.review_source:2026-10-30", fingerprint)
    service.notify(f"source:{original}", "等待来源", "旧的缺来源提醒")
    try:
        asyncio.run(service.refresh_review_sources())
        duplicate = service.task(original)
        assert duplicate["status"] == "skipped"
        assert duplicate["attempts"] == 0
        assert duplicate["result"]["reused_task_id"] == completed
        assert duplicate["result"]["artifacts"] == result["artifacts"]
        assert duplicate["result"]["publication_id"] == "publication"
        if existing_status == "waiting_review":
            assert duplicate["result"]["candidate_id"] == "candidate"
        assert service.task(completed)["status"] == existing_status
        notice = service.store.connection.execute(
            "SELECT status FROM notifications WHERE id=?",
            (f"source:{original}",)).fetchone()
        assert notice[0] == "resolved"
        asyncio.run(service.refresh_review_sources())
        assert len(service.store.export_tasks()) == 2
    finally:
        service.store.close()


@pytest.mark.parametrize("parts", [None, 42, "不应被拆成单字", {"text": "异常结构"}])
def test_invalid_export_parts_do_not_abort_other_messages(project, parts):
    """单条格式异常的导出消息不能中断其余有效来源的读取。"""
    root, config = project
    source = root / config["review"]["source_directory"] / "conversations.json"
    stamp = "2026-10-29T17:00:00Z"
    configuration.atomic_json(source, [{
        "current_node": "valid",
        "mapping": {
            "broken": {
                "message": {
                    "create_time": stamp,
                    "author": {
                        "role": "user"
                    },
                    "content": {
                        "parts": parts
                    }
                }
            },
            "valid": {
                "parent": "broken",
                "message": {
                    "create_time": stamp,
                    "author": {
                        "role": "assistant"
                    },
                    "content": {
                        "parts": ["已经验证的成果"]
                    }
                }
            }
        }
    }])
    report = review_sources.scan(root, config)
    assert [message["text"] for message in report["messages"]] == ["已经验证的成果"]
    assert report["warnings"]
