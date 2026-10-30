"""验证桌面运行时迁移不会覆写其他服务或丢失队列。"""

from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess

import pytest

from second_proprio import desktop_deployment as deployment


@pytest.fixture(name="workspace")
def fixture_workspace(tmp_path: Path) -> Path:
    """准备与正式配置结构一致的临时工作区。"""
    root = tmp_path / "workspace"
    shutil.copytree(Path(__file__).parents[1] / "config", root / "config")
    return root


def test_launchd_uses_stable_runtime_and_login_policy(workspace: Path) -> None:
    """持久服务使用独立运行时，不依赖开发虚拟环境或轮询定时器。"""
    value = deployment.render_service(workspace, Path("/runtime/engine"), False)
    assert value["ProgramArguments"] == [
        "/runtime/engine", "--root",
        str(workspace), "daemon"
    ]
    assert value["RunAtLoad"] is False
    assert "StartInterval" not in value
    assert value["Umask"] == 0o077


def test_backup_copies_committed_wal_state(workspace: Path,
                                           tmp_path: Path) -> None:
    """SQLite 备份包含 WAL 中尚未合并的数据。"""
    database = workspace / ".state/queue.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE evidence (value TEXT)")
        connection.execute("INSERT INTO evidence VALUES ('retained')")
        connection.commit()
        destination = tmp_path / "backup"
        deployment.backup_state(workspace, destination)
        with sqlite3.connect(destination / "queue.sqlite3") as snapshot:
            assert snapshot.execute(
                "SELECT value FROM evidence").fetchone() == ("retained",)
    assert (destination / "schema.json").is_file()


def test_login_policy_does_not_overwrite_unrelated_service(
        workspace: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """不同工作区的同名服务不能被设置页面覆盖。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / "Library/LaunchAgents" / f"{deployment.LABEL}.plist"
    target.parent.mkdir(parents=True)
    original = {"WorkingDirectory": "/another/project", "RunAtLoad": True}
    target.write_bytes(plistlib.dumps(original))
    with pytest.raises(ValueError, match="不属于"):
        deployment.apply_login_policy(workspace, False)
    assert plistlib.loads(target.read_bytes()) == original


def test_login_policy_is_saved_without_restarting(
        workspace: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """只修改下次登录策略，运行中的服务无需中断。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / "Library/LaunchAgents" / f"{deployment.LABEL}.plist"
    target.parent.mkdir(parents=True)
    target.write_bytes(plistlib.dumps({"WorkingDirectory": str(workspace)}))
    result = deployment.apply_login_policy(workspace, False)
    assert result["status"] == "saved"
    assert plistlib.loads(target.read_bytes())["RunAtLoad"] is False


def test_only_current_workspace_codex_blocks_migration(
        workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """其他聊天的 Codex 进程不会阻塞本项目迁移。"""
    output = f"123 codex exec -C {workspace} --json\n124 codex exec -C /other\n"
    monkeypatch.setattr(
        subprocess, "run", lambda *_args, **_kwargs: subprocess.
        CompletedProcess([], 0, output, ""))
    assert deployment.legacy_model_processes(workspace) == [123]


def test_failed_install_restores_previous_login_item(
        workspace: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """新后台无法启动时恢复旧登录项，不触碰队列和业务配置。"""
    backup = tmp_path / "backup"
    backup.mkdir()
    original = plistlib.dumps({"WorkingDirectory": str(workspace)})
    (backup / "legacy-disabled.plist").write_bytes(original)
    target = tmp_path / f"{deployment.LABEL}.plist"
    target.write_bytes(b"new service")
    calls = []

    def fake_launchctl(*arguments):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(deployment, "_launchctl", fake_launchctl)
    result = deployment.rollback_service(workspace, backup, target)
    assert result["status"] == "restored"
    assert not target.exists()
    assert target.with_name(
        f"{deployment.LEGACY_LABEL}.plist").read_bytes() == original
    assert calls[-1][0] == "bootstrap"


def test_stop_waits_for_dying_socket_to_close(
        workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """关闭请求之后不能把仍在退出的服务误判成下一次启动成功。"""
    calls = []

    def fake_request(_root, method, _params=None):
        calls.append(method)
        if method == "tasks.list":
            return {"tasks": []}
        if calls.count("system.status") == 1:
            return {"running": True, "stopping": True}
        raise ConnectionRefusedError()

    monkeypatch.setattr(deployment, "request", fake_request)
    monkeypatch.setattr(
        deployment, "_launchctl",
        lambda *args: subprocess.CompletedProcess(args, 0, "", ""))
    result = deployment.stop(workspace)
    assert result["status"] == "stopped"
    assert calls.count("system.status") == 2
