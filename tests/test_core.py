"""验证离线调度、状态合并、配置更新和个人记录边界。"""

import copy
import argparse
import datetime as dt
import json
from pathlib import Path
import shutil
import sys
import time
from zoneinfo import ZoneInfo

import jsonschema
import pytest

from second_proprio import config as configuration
from second_proprio import personal
from second_proprio import scheduler
from second_proprio import cli
from second_proprio import runner
from second_proprio.store import Store


@pytest.fixture(name="project")
def fixture_project(tmp_path):
    """在临时目录建立真实配置副本。"""
    source = Path(__file__).parents[1] / "config"
    shutil.copytree(source, tmp_path / "config")
    config = configuration.load(tmp_path)
    config["runtime"]["notify"] = False
    return tmp_path, config


def test_invalid_config_preserves_previous(project):
    """非法候选配置不会覆盖最后有效配置。"""
    root, config = project
    original = root / "config/settings.json"
    configuration.atomic_json(original, config)
    invalid = copy.deepcopy(config)
    invalid["schedules"]["papers"]["times"] = ["25:72"]
    candidate = root / "invalid.json"
    configuration.atomic_json(candidate, invalid)
    with pytest.raises(jsonschema.ValidationError):
        configuration.apply(root, candidate)
    assert json.loads(original.read_text()) == config


def test_offline_jobs_merge_and_rerun_is_idempotent(project):
    """离线数日后课程只留一节、提醒只留当天且重复扫无重复任务。"""
    root, config = project
    now = dt.datetime(2026, 11, 2, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    store = Store(root / ".state/queue.sqlite3")
    try:
        scheduler.enqueue(store, config, now)
        first_count = len(store.export_tasks())
        scheduler.enqueue(store, config, now)
        assert len(store.export_tasks()) == first_count
        ready = scheduler.ready_tasks(store, config, now)
        assert len([task for task in ready if task["kind"] == "course"]) == 1
        care = [task for task in ready if task["kind"] == "care"]
        assert len(care) == 1
        assert care[0]["scheduled_at"].startswith("2026-11-02T20:00")
        assert len([task for task in ready if task["kind"] == "papers"]) == 3
    finally:
        store.close()


def test_remote_completion_does_not_hide_local_work(project):
    """服务器完成标志不能使本机跳过从未产生的成果。"""
    root, _ = project
    store = Store(root / ".state/queue.sqlite3")
    try:
        identifier = store.add("papers", "2026-10-29T12:30:00+08:00",
                               "revision")
        remote = store.export_tasks()[0] | {"status": "completed"}
        store.merge_tasks([remote])
        assert store.export_tasks()[0]["status"] == "pending"
        store.claim(identifier)
        store.recover()
        assert store.export_tasks()[0]["status"] == "pending"
        store.finish(identifier, "completed", {"artifacts": ["paper.md"]})
        store.merge_tasks([remote | {"status": "pending"}])
        assert store.export_tasks()[0]["result"]["artifacts"] == ["paper.md"]
    finally:
        store.close()


def test_anniversary_and_no_previous_day_mood(project):
    """纪念日按日期增加，心情不继承前一天。"""
    root, config = project
    day = dt.date(2026, 10, 29)
    assert personal.relationship_days(config, day) == 973
    assert personal.relationship_days(config, day + dt.timedelta(days=1)) == 974
    personal.save_mood(root, config, day, 4, action="一起散步")
    now = dt.datetime(2026, 10, 30, 14, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = personal.care(root, config, now)
    assert "待填写" in (root / result["artifacts"][0]).read_text()
    with pytest.raises(ValueError):
        personal.save_mood(root, config, day, 6)


def test_chat_import_selects_active_branch_and_timezone(project):
    """同一对话的废弃分支不进入当天复盘，日期按北京时间划分。"""
    root, config = project
    stamp = dt.datetime(2026, 10, 28, 17, tzinfo=dt.timezone.utc).timestamp()

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

    source = root / "export.json"
    configuration.atomic_json(source, [{
        "id": "chat",
        "title": "来源样例",
        "current_node": "chosen",
        "mapping": {
            "root": node("开始"),
            "chosen": node("已经完成实验", "root"),
            "discarded": node("删除的分支", "root")
        }
    }])
    personal.import_chat(root, config, source)
    context = personal.review_context(root, config, dt.date(2026, 10, 29))
    assert context["status"] == "ready"
    evidence = (root / context["context"]).read_text()
    assert "已经完成实验" in evidence
    assert "删除的分支" not in evidence
    missing = personal.review_context(root, config, dt.date(2026, 10, 30))
    assert missing["status"] == "waiting_source"
    assert "请告诉我今天的记录在哪里" in (root / missing["output"]).read_text()


def test_path_escape_rejected(project):
    """输出目录不得通过相对路径或符号链接越出项目。"""
    root, _ = project
    with pytest.raises(ValueError):
        configuration.resolve_path(root, "../outside")
    (root / "escape").symlink_to(root.parent)
    with pytest.raises(ValueError):
        configuration.resolve_path(root, "escape/outside")


def test_status_and_mood_work_while_generation_is_running(project):
    """生成长文时仍可查看状态和记录当天心情，不等待模型执行器。"""
    root, config = project
    with runner.lock(root, config):
        status = cli._dispatch(  # pylint: disable=protected-access
            argparse.Namespace(root=root, command="status"))
        assert status["tasks"] == []
        result = cli._dispatch(  # pylint: disable=protected-access
            argparse.Namespace(root=root,
                               command="mood",
                               date=None,
                               score=3,
                               note="测试记录",
                               action=""))
        assert json.loads(Path(result["saved"]).read_text("utf-8"))["mood"] == 3


def test_cli_completion_recovery_requires_valid_final_and_completed_turn(
        tmp_path):
    """退出清理滞后可恢复；缺完成事件、无效结果或工具文本不能冒充完成。"""
    log = tmp_path / "events.jsonl"
    result = {
        "status": "completed",
        "artifacts": ["lesson.md"],
        "summary": "完成"
    }
    message = {
        "type": "item.completed",
        "item": {
            "type": "agent_message",
            "text": json.dumps(result)
        }
    }
    log.write_text(json.dumps(message) + "\n", "utf-8")
    assert runner.completed_result(log) is None

    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "turn.completed"}) + "\n")
    assert runner.completed_result(log) == result
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "turn.failed"}) + "\n")
    assert runner.completed_result(log) is None
    message["item"]["type"] = "command_execution"
    log.write_text(
        json.dumps(message) + '\n{"type":"turn.completed"}\n', "utf-8")
    assert runner.completed_result(log) is None
    message["item"] = {
        "type": "agent_message",
        "text": '{"status":"completed"}'
    }
    log.write_text(
        json.dumps(message) + '\n{"type":"turn.completed"}\n', "utf-8")
    assert runner.completed_result(log) is None


def test_cli_cleanup_hang_recovers_verified_output(project):
    """CLI 已报告完成但清理阻塞时，验证成果后终止清理并返回成功。"""
    root, config = project
    executable = root / "fake_codex.py"
    executable.write_text(
        f"#!{sys.executable}\n"
        '\"\"\"模拟完成后清理阻塞的命令行工具。\"\"\"\n'
        "import json\nimport sys\nimport time\n"
        "from pathlib import Path\n"
        "sys.stdin.read()\n"
        "root=Path(sys.argv[sys.argv.index('-C')+1])\n"
        "(root/'artifact.txt').write_text('verified')\n"
        "result={'status':'completed','artifacts':['artifact.txt'],"
        "'summary':'ok'}\n"
        "print(json.dumps({'type':'item.completed',"
        "'item':{'type':'agent_message',"
        "'text':json.dumps(result)}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed'}),flush=True)\n"
        "time.sleep(60)\n", "utf-8")
    executable.chmod(0o700)
    config["runtime"].update(codex_command=str(executable), timeout_seconds=20)
    started = time.monotonic()
    result = runner.codex(root, config, "fixture", "cleanup")
    assert result["status"] == "completed"
    assert time.monotonic() - started < 20
