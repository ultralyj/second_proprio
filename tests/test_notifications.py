"""验证通知命令的失败隔离、参数传递与日志隐私。"""

import errno
import logging
import subprocess

import pytest

from second_proprio import personal


@pytest.fixture(name="config")
def fixture_config(monkeypatch):
    """固定通知支持平台；每个测试自行替换真实子进程。"""
    monkeypatch.setattr(personal.sys, "platform", "darwin")
    return {"runtime": {"notify": True}}


def test_success_means_command_accepted_and_uses_separate_arguments(
        config, monkeypatch, caplog):
    """标题和正文独立传参，零退出码不作为实际送达证明。"""
    title = '私密标题"; do shell script "unexpected"'
    message = "不应进入日志的个人正文"
    observed = {}

    def run(command, **kwargs):
        observed.update({"command": command, "kwargs": kwargs})
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(personal.subprocess, "run", run)
    with caplog.at_level(logging.DEBUG, logger="second_proprio.personal"):
        assert personal.notify(config, title, message) is True
    assert observed["command"][-2:] == [title, message]
    assert title not in observed["command"][2]
    assert observed["kwargs"] == {
        "check": False,
        "capture_output": True,
        "timeout": 15,
    }
    assert title not in caplog.text
    assert message not in caplog.text
    assert "送达" not in caplog.text


def test_nonzero_exit_is_logged_without_private_stderr(config, monkeypatch,
                                                       caplog):
    """发送失败返回 False，诊断日志不泄露系统复述的个人文本。"""
    title = "私密标题"
    message = "个人关系正文，不应出现在诊断中"
    stderr = f"execution error: {title} {message}".encode()
    monkeypatch.setattr(
        personal.subprocess, "run",
        lambda command, **_: subprocess.CompletedProcess(
            command, 1, b"", stderr))
    assert personal.notify(config, title, message) is False
    assert "event=notification_command_failed" in caplog.text
    assert "exit_code=1" in caplog.text
    assert "stderr_present=True" in caplog.text
    assert "at=" in caplog.text
    assert title not in caplog.text
    assert message not in caplog.text
    assert "execution error" not in caplog.text


def test_timeout_is_logged_and_does_not_escape(config, monkeypatch, caplog):
    """超时异常含通知参数时也不把参数记录到日志。"""
    title = "超时场景私密标题"
    message = "超时场景私密正文"

    def run(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 15, stderr=message.encode())

    monkeypatch.setattr(personal.subprocess, "run", run)
    assert personal.notify(config, title, message) is False
    assert "event=notification_command_timeout" in caplog.text
    assert "timeout_seconds=15" in caplog.text
    assert title not in caplog.text
    assert message not in caplog.text


def test_os_error_is_logged_without_filename_or_exception_text(
        config, monkeypatch, caplog):
    """程序缺失或无权限不让失败通知再次中断任务队列。"""
    title = "系统错误私密标题"
    message = "系统错误私密正文"

    def run(*_args, **_kwargs):
        raise OSError(errno.EACCES, message, "/private/personal-location")

    monkeypatch.setattr(personal.subprocess, "run", run)
    assert personal.notify(config, title, message) is False
    assert "event=notification_command_os_error" in caplog.text
    assert f"errno={errno.EACCES}" in caplog.text
    assert title not in caplog.text
    assert message not in caplog.text
    assert "/private/personal-location" not in caplog.text


@pytest.mark.parametrize("enabled,platform", [(False, "darwin"),
                                              (True, "linux")])
def test_disabled_or_unsupported_does_not_launch(config, monkeypatch, caplog,
                                                 enabled, platform):
    """关闭通知或不支持的平台不启动命令，也不记为发送故障。"""
    config["runtime"]["notify"] = enabled
    monkeypatch.setattr(personal.sys, "platform", platform)

    def forbidden(*_args, **_kwargs):
        pytest.fail("不应执行通知命令")

    monkeypatch.setattr(personal.subprocess, "run", forbidden)
    assert personal.notify(config, "标题", "正文") is False
    assert not caplog.records
