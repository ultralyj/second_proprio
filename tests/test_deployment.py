"""验证 launchd 与 systemd 文件的可执行配置及安装边界。"""

from pathlib import Path
import plistlib
import subprocess

import pytest

from second_proprio import deployment


@pytest.fixture(name="config")
def fixture_config():
    """提供部署最小配置。"""
    return {
        "paths": {
            "state": ".state"
        },
        "scheduler": {
            "sync_interval_seconds": 300
        },
        "server": {
            "root": "/home/ubuntu/second-proprio"
        },
    }


def test_launchd_uses_argument_array_and_login_trigger(tmp_path, config):
    """含空格的路径保持单一参数，且登录和周期触发均已配置。"""
    root = tmp_path / "project with spaces"
    root.mkdir()
    interpreter = root / ".venv/bin/python"
    output = deployment.render_launchd(root, interpreter, config)
    plist = plistlib.loads(output.read_bytes())
    assert plist["ProgramArguments"] == [
        str(interpreter), "-m", "second_proprio", "--root",
        str(root), "tick"
    ]
    assert plist["RunAtLoad"] is True
    assert plist["StartInterval"] == 300
    assert plist["WorkingDirectory"] == str(root)
    assert "shell" not in plist
    assert plist["Umask"] == 0o077


def test_configured_sync_interval_is_rendered(tmp_path, config):
    """调度轮询间隔来自 JSON 配置。"""
    config["scheduler"]["sync_interval_seconds"] = 600
    output = deployment.render_launchd(tmp_path, "/usr/bin/python3", config)
    assert plistlib.loads(output.read_bytes())["StartInterval"] == 600


def test_systemd_is_lightweight_and_contains_no_model_login(tmp_path, config):
    """服务器仅运行队列任务，不部署模型或复制登录凭据。"""
    paths = deployment.render_systemd(tmp_path, config)
    service, timer, installer, readme = [
        path.read_text(encoding="utf-8") for path in paths
    ]
    assert "server-tick" in service
    assert "OnUnitActiveSec=60s" in timer
    assert "systemctl --user enable --now" in installer
    assert "loginctl enable-linger ubuntu" in readme
    for content in (service, timer, installer):
        assert "codex login" not in content
        assert "auth.json" not in content
        assert "torch" not in content


def test_systemd_escapes_percent_in_path(tmp_path, config):
    """路径中的百分号不能变成 systemd 说明符。"""
    config["server"]["root"] = "/home/ubuntu/project%name"
    service = deployment.render_systemd(tmp_path, config)[0].read_text()
    assert "project%%name" in service


def test_rendering_does_not_register_services(tmp_path, config, monkeypatch):
    """生成部署材料不会执行安装副作用。"""

    def forbidden(*_args, **_kwargs):
        pytest.fail("生成配置时不得执行系统命令")

    monkeypatch.setattr(deployment.subprocess, "run", forbidden)
    deployment.render_launchd(tmp_path, "/usr/bin/python3", config)
    deployment.render_systemd(tmp_path, config)


def test_install_refuses_other_project_label(tmp_path, config, monkeypatch):
    """不覆盖属于其他项目的同名 launchd 服务。"""
    monkeypatch.setattr(deployment.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    target = Path.home(
    ) / "Library/LaunchAgents/com.second-proprio.worker.plist"
    target.parent.mkdir(parents=True)
    target.write_bytes(plistlib.dumps({"WorkingDirectory": "/another/project"}))
    interpreter = tmp_path / "python"
    interpreter.touch()
    with pytest.raises(RuntimeError, match="另一目录"):
        deployment.install_launchd(tmp_path, interpreter, config)


def test_install_records_only_successful_bootstrap(tmp_path, config,
                                                   monkeypatch):
    """安装成功状态只能在 launchctl 确认之后写入。"""
    monkeypatch.setattr(deployment.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    interpreter = tmp_path / "python"
    interpreter.touch()
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        status = 1 if command[1] == "print" else 0
        return subprocess.CompletedProcess(command, status, "", "")

    monkeypatch.setattr(deployment.subprocess, "run", run)
    state = deployment.install_launchd(tmp_path, interpreter, config)
    assert state["status"] == "installed"
    assert commands[-1][1] == "bootstrap"
    assert (tmp_path / ".state/deploy/local-install.json").is_file()


def test_systemd_working_directory_uses_scalar_path(tmp_path, config):
    """WorkingDirectory 是整个绝对路径，不接受 ExecStart 的双引号语法。"""
    config["server"]["root"] = "/home/ubuntu/second proprio 100%"
    service = deployment.render_systemd(tmp_path, config)[0].read_text()
    assert "WorkingDirectory=/home/ubuntu/second proprio 100%%\n" in service
    assert 'WorkingDirectory="' not in service
