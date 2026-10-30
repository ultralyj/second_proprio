"""验证后台失败根因、长任务退避和通知次数。"""

import argparse
import datetime as dt
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

from second_proprio import cli
from second_proprio import runner
from second_proprio.store import Store


@pytest.fixture(name="config")
def fixture_config() -> dict:
    """读取正式模板，测试不触发系统通知。"""
    project = Path(__file__).resolve().parents[1]
    value = json.loads((project / "config/settings.example.json").read_text())
    value["runtime"]["notify"] = False
    return value


def test_cli_failure_exposes_terminal_reason(tmp_path: Path,
                                             config: dict) -> None:
    """真实子进程报告路由超时时，错误包含原因及可定位的日志目录。"""
    executable = tmp_path / "fake_codex.py"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json\nimport sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'turn.failed', 'error': "
        "{'message': 'workspace routing discovery timed out'}}))\n"
        "sys.exit(1)\n",
        encoding="utf-8")
    executable.chmod(0o700)
    config["runtime"]["codex_command"] = str(executable)
    with pytest.raises(RuntimeError, match="Codex 工作区路由连接超时；日志："):
        runner.codex(tmp_path, config, "测试连接失败", "routing")


def test_diagnostics_ignore_tool_text_and_stale_turn(tmp_path: Path) -> None:
    """工具中的伪造错误、旧轮次和损坏日志不冒充当前 CLI 错误。"""
    log = tmp_path / "events.jsonl"
    events = [
        {
            "type": "error",
            "message": "403 Forbidden"
        },
        {
            "type": "turn.started"
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "aggregated_output": "workspace routing discovery timed out"
            }
        },
    ]
    log.write_text("broken\n" + "\n".join(json.dumps(e) for e in events),
                   encoding="utf-8")
    assert runner.cli_failure_reason(log) == "未返回有效结果，具体原因见日志"
    with log.open("a", encoding="utf-8") as stream:
        stream.write('\n{"type":"error","message":"HTTP 403 Forbidden"}')
    assert runner.cli_failure_reason(log) == "Codex 连接被服务端拒绝（HTTP 403）"


def test_long_failure_backs_off_from_finish_and_limits_notifications(
        tmp_path: Path, config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """长任务失败后完整等待重试间隔，耗尽时不再承诺自动重试。"""
    now = dt.datetime(2026, 10, 29, 12, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    config["scheduler"].update(max_attempts=3, retry_seconds=1800)
    elapsed = [0.0]
    monkeypatch.setattr(runner.time, "monotonic", lambda: elapsed[0])
    notices = []
    monkeypatch.setattr(
        runner.personal, "notify",
        lambda _config, title, message: notices.append((title, message)))

    def fail(*_args: object) -> None:
        elapsed[0] += 2400
        raise RuntimeError("Codex 工作区路由连接超时；日志：/private/logs/run")

    monkeypatch.setattr(runner, "execute", fail)
    store = Store(tmp_path / ".state/queue.sqlite3")
    try:
        store.add("papers", now.isoformat(), "test")
        runner.consume(tmp_path, config, store, now)
        first = store.export_tasks()[0]
        retry_at = dt.datetime.fromisoformat(first["retry_at"])
        assert retry_at == now + dt.timedelta(seconds=4200)
        assert "论文解读失败 (1/3)" in notices[0][0]
        assert "剩余 2 次" in notices[0][1]
        assert "/private/logs" not in notices[0][1]
        runner.consume(tmp_path, config, store, now + dt.timedelta(minutes=45))
        assert store.export_tasks()[0]["attempts"] == 1
        runner.consume(tmp_path, config, store, retry_at)
        second = store.export_tasks()[0]
        assert second["attempts"] == 2
        assert len(notices) == 1
        runner.consume(tmp_path, config, store,
                       dt.datetime.fromisoformat(second["retry_at"]))
        final = store.export_tasks()[0]
        assert final["status"] == "failed"
        assert final["attempts"] == 3
        assert final["retry_at"] is None
        assert len(notices) == 2
        assert "已达到重试上限" in notices[-1][1]
        assert "手动重试" in notices[-1][1]
        runner.consume(tmp_path, config, store, now + dt.timedelta(days=1))
        assert store.export_tasks()[0]["attempts"] == 3
    finally:
        store.close()


def test_tick_refreshes_time_after_slow_sync(
        tmp_path: Path, config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """同步耗时不压缩随后任务的重试间隔。"""
    before = dt.datetime(2026, 10, 29, 12, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    after = before + dt.timedelta(minutes=5)
    clock = [before]
    observed = []
    config["runtime"]["auto_run"] = True
    monkeypatch.setattr(cli.configuration, "load", lambda _root: config)
    monkeypatch.setattr(cli.scheduler, "now_local", lambda _config: clock[0])

    def sync(*_args: object) -> dict:
        clock[0] = after
        return {"status": "error"}

    monkeypatch.setattr(cli.synchronization, "sync", sync)
    monkeypatch.setattr(cli.scheduler, "enqueue", lambda *_args: None)
    monkeypatch.setattr(cli.runner, "dashboard", lambda *_args: None)
    monkeypatch.setattr(
        cli.runner, "consume",
        lambda _root, _config, _store, now, _kind: observed.append(now) or [])
    cli._dispatch(  # pylint: disable=protected-access
        argparse.Namespace(root=tmp_path, command="tick"))
    assert observed == [after]


def test_desktop_cli_uses_one_approval_sandbox_mode(
        tmp_path: Path, config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """桌面运行只使用自带 workspace-write 的审批参数，避免互斥选项。"""
    executable = tmp_path / "fake_codex.py"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,sys\nfrom pathlib import Path\n"
        "sys.stdin.read()\n"
        "assert '--approve-for-me' in sys.argv\n"
        "assert '--sandbox' not in sys.argv\n"
        "assert '--dangerously-bypass-approvals-and-sandbox' not in sys.argv\n"
        "output=Path(sys.argv[sys.argv.index('-o')+1])\n"
        "output.write_text(json.dumps({'status':'skipped',"
        "'artifacts':[],'summary':'fixture'}))\n",
        encoding="utf-8")
    executable.chmod(0o700)
    config["runtime"]["codex_command"] = str(executable)
    monkeypatch.setenv("SECOND_PROPRIO_CODEX_PID_FILE",
                       str(tmp_path / "pid.json"))
    result = runner.codex(tmp_path, config, "测试参数兼容", "arguments")
    assert result["status"] == "skipped"
    assert (tmp_path / "pid.json").exists()


def test_cli_argument_error_preserves_actual_reason(tmp_path: Path) -> None:
    """JSON 启动前的 CLI 参数报错保留根因，不误报无可用结果。"""
    log = tmp_path / "events.jsonl"
    log.write_text(
        "error: the argument '--sandbox <SANDBOX_MODE>' cannot be used with "
        "'--approve-for-me'\n\nUsage: codex exec [OPTIONS]\n", "utf-8")
    result = runner.cli_failure_reason(log)
    assert result.startswith("Codex CLI 参数错误：")
    assert "cannot be used with '--approve-for-me'" in result
