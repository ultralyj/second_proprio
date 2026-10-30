"""安装可独立于界面存活的用户级桌面服务，并保留迁移回退材料。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from typing import Iterator

from second_proprio import config as configuration

LABEL = "com.second-proprio.desktop-service"
LEGACY_LABEL = "com.second-proprio.worker"
VERSION = "0.2.0"


def support_directory() -> Path:
    """返回当前用户的独立运行时目录。"""
    return Path.home() / "Library/Application Support/second-proprio"


def _launchctl(*arguments: str) -> subprocess.CompletedProcess:
    """执行有超时的用户服务管理命令。"""
    return subprocess.run(["/bin/launchctl", *arguments],
                          capture_output=True,
                          text=True,
                          check=False,
                          timeout=20)


def _state(root: Path) -> Path:
    """从配置解析状态目录，拒绝越界路径。"""
    return configuration.resolve_path(
        root,
        configuration.load(root)["paths"]["state"])


@contextmanager
def migration_lock(root: Path) -> Iterator[None]:
    """仅在旧执行器空闲时迁移，避免接管正在写入的任务。"""
    state = _state(root)
    state.mkdir(parents=True, exist_ok=True)
    with (state / "worker.lock").open("a", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("旧后台仍在执行任务；完成后才能安全切换。") from error
        try:
            if legacy_model_processes(root):
                raise RuntimeError("旧 Codex 子进程仍未退出；保留原服务并等待。")
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def legacy_model_processes(root: Path) -> list[int]:
    """仅识别明确以当前项目为执行目录的 Codex 进程。"""
    output = subprocess.run(["/bin/ps", "-axo", "pid=,command="],
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=10)
    result = []
    for line in output.stdout.splitlines():
        try:
            pid, command = line.strip().split(maxsplit=1)
            arguments = shlex.split(command)
        except ValueError:
            continue
        if not arguments or Path(arguments[0]).name != "codex":
            continue
        if "exec" not in arguments or "-C" not in arguments:
            continue
        index = arguments.index("-C") + 1
        if index < len(arguments) and Path(arguments[index]).resolve() == root:
            result.append(int(pid))
    return result


def backup_state(root: Path, destination: Path) -> None:
    """使用 SQLite 在线备份，复制配置和旧服务定义而不读取聊天正文。"""
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("settings.json", "schema.json"):
        source = root / "config" / name
        if source.exists():
            shutil.copy2(source, destination / name)
    database = _state(root) / "queue.sqlite3"
    if database.exists():
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source:
            with sqlite3.connect(destination / "queue.sqlite3") as target:
                source.backup(target)
    old = Path.home() / "Library/LaunchAgents" / f"{LEGACY_LABEL}.plist"
    if old.exists():
        shutil.copy2(old, destination / old.name)


def _engine_source() -> Path:
    """获取 PyInstaller 目录模式的完整运行时源目录。"""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("请从已打包的 App 安装后台运行时。")
    executable = Path(sys.executable).resolve()
    if not (executable.parent / "_internal").is_dir():
        raise RuntimeError("后端运行时资源不完整。")
    return executable.parent


def install_runtime(source: Path) -> Path:
    """按构建指纹保留不可变运行时，界面替换不影响在途任务。"""
    executable = source / "second-proprio-engine"
    if not executable.is_file() or not (source / "_internal").is_dir():
        raise ValueError("不是完整的后端运行时目录。")
    fingerprint = hashlib.sha256(executable.read_bytes()).hexdigest()[:12]
    target = support_directory(
    ) / "runtime" / f"{VERSION}-{fingerprint}" / source.name
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target.with_name(target.name + ".installing")
        if temporary.exists():
            shutil.rmtree(temporary)
        shutil.copytree(source, temporary, symlinks=True)
        os.replace(temporary, target)
    return target / executable.name


def render_service(root: Path,
                   engine: Path,
                   launch_at_login: bool = True) -> dict:
    """构造使用固定运行时路径的原生 launchd 配置。"""
    state = _state(root)
    paths = [
        "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin",
        "/sbin"
    ]
    codex = configuration.load(root)["runtime"]["codex_command"]
    if Path(codex).is_absolute():
        paths.insert(0, str(Path(codex).parent))
    bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/"
                   "CodexCLI.app/Contents/MacOS")
    if bundled.is_dir():
        paths.insert(0, str(bundled))
    return {
        "Label": LABEL,
        "ProgramArguments": [str(engine), "--root",
                             str(root), "daemon"],
        "WorkingDirectory": str(root),
        "RunAtLoad": launch_at_login,
        "KeepAlive": {
            "SuccessfulExit": False,
            "AfterInitialDemand": True
        },
        "ThrottleInterval": 15,
        "ExitTimeOut": 30,
        "ProcessType": "Background",
        "Umask": 0o077,
        "EnvironmentVariables": {
            "PATH": ":".join(paths),
            "PYTHONUNBUFFERED": "1"
        },
        "StandardOutPath": str(state / "logs/desktop.stdout.log"),
        "StandardErrorPath": str(state / "logs/desktop.stderr.log"),
    }


def apply_login_policy(root: Path, enabled: bool) -> dict:
    """更新下次登录的启动策略，不中断当前服务或任务。"""
    target = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    if not target.exists():
        return {"status": "not_installed", "launch_at_login": enabled}
    value = plistlib.loads(target.read_bytes())
    if value.get("WorkingDirectory") != str(root.resolve()):
        raise ValueError("后台服务不属于当前工作区。")
    value["RunAtLoad"] = enabled
    value["KeepAlive"] = {"SuccessfulExit": False, "AfterInitialDemand": True}
    temporary = target.with_suffix(".plist.tmp")
    temporary.write_bytes(plistlib.dumps(value))
    temporary.chmod(0o600)
    os.replace(temporary, target)
    return {"status": "saved", "launch_at_login": enabled}


def request(root: Path, method: str, params: dict | None = None) -> dict:
    """发送仅用于部署检查的短请求。"""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(5)
        stream.connect(str(_state(root) / "desktop.sock"))
        stream.sendall((json.dumps({
            "id": "installer",
            "method": method,
            "params": params or {}
        }) + "\n").encode())
        response = json.loads(stream.makefile("rb").readline(2 * 1024 * 1024))
    if "error" in response:
        raise RuntimeError(str(response["error"]))
    return response["result"]


def wait_until_disconnected(root: Path) -> bool:
    """等待旧进程关闭接口，避免停止返回后立即连接到正在退出的服务。"""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            request(root, "system.status")
        except (OSError, ValueError, RuntimeError):
            return True
        time.sleep(0.1)
    return False


def rollback_service(root: Path, backup: Path, target: Path) -> dict:
    """部署失败时恢复旧登录项，保留新服务的诊断和队列。"""
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}")
    previous = backup / "desktop-service.plist"
    legacy = backup / "legacy-disabled.plist"
    if previous.exists():
        shutil.copy2(previous, target)
        restored = _launchctl("bootstrap", domain, str(target))
    elif legacy.exists():
        target.unlink(missing_ok=True)
        old_target = target.with_name(f"{LEGACY_LABEL}.plist")
        shutil.copy2(legacy, old_target)
        restored = _launchctl("bootstrap", domain, str(old_target))
    else:
        target.unlink(missing_ok=True)
        return {"status": "not_available", "root": str(root)}
    return {
        "status": "restored" if restored.returncode == 0 else "error",
        "message": restored.stderr.strip()
    }


def start(root: Path, app_executable: Path) -> dict:
    """部署或启动服务；旧任务未结束时返回可显示的等待状态。"""
    root = root.resolve()
    config = configuration.load(root)
    app_executable = app_executable.resolve()
    if not app_executable.is_file():
        raise ValueError("App 可执行文件不存在。")
    engine = install_runtime(_engine_source())
    state = _state(root)
    configuration.atomic_json(state / "desktop-app.json",
                              {"app_executable": str(app_executable)})
    configuration.atomic_json(support_directory() / "desktop.json",
                              {"workspace": str(root)})
    try:
        status = request(root, "system.status")
        if not status.get("stopping"):
            return {"status": "started", "message": "后台已连接", "service": status}
        if not wait_until_disconnected(root):
            return {"status": "error", "message": "后台仍在退出，请稍后再启动。"}
    except (OSError, ValueError, RuntimeError):
        pass
    domain = f"gui/{os.getuid()}"
    target = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    old_target = target.with_name(f"{LEGACY_LABEL}.plist")
    for existing in (target, old_target):
        if existing.exists():
            value = plistlib.loads(existing.read_bytes())
            if value.get("WorkingDirectory") != str(root):
                raise RuntimeError("同名后台服务属于另一个工作区，未覆盖。")
    try:
        with migration_lock(root):
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = state / "migration-backups" / stamp
            backup_state(root, backup)
            if target.exists():
                shutil.copy2(target, backup / "desktop-service.plist")
            if _launchctl("print", f"{domain}/{LEGACY_LABEL}").returncode == 0:
                stopped = _launchctl("bootout", f"{domain}/{LEGACY_LABEL}")
                if stopped.returncode:
                    raise RuntimeError("旧后台服务无法停止，未切换。")
            # 保留备份，移走旧登录项，避免下次登录同时加载两套服务。
            if old_target.exists():
                shutil.move(str(old_target),
                            str(backup / "legacy-disabled.plist"))
            if _launchctl("print", f"{domain}/{LABEL}").returncode == 0:
                _launchctl("bootout", f"{domain}/{LABEL}")
            target.parent.mkdir(parents=True, exist_ok=True)
            (state / "logs").mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".plist.tmp")
            temporary.write_bytes(
                plistlib.dumps(
                    render_service(
                        root, engine,
                        config.get("desktop", {}).get("launch_at_login",
                                                      True))))
            temporary.chmod(0o600)
            os.replace(temporary, target)
            configuration.atomic_json(
                support_directory() / "runtime.json", {
                    "version": VERSION,
                    "engine": str(engine),
                    "workspace": str(root),
                    "app_executable": str(app_executable),
                    "backup": str(backup)
                })
    except RuntimeError as error:
        return {"status": "waiting_old_worker", "message": str(error)}
    _launchctl("enable", f"{domain}/{LABEL}")
    loaded = _launchctl("bootstrap", domain, str(target))
    if loaded.returncode:
        return {
            "status": "error",
            "message": loaded.stderr.strip(),
            "backup": str(backup),
            "rollback": rollback_service(root, backup, target)
        }
    _launchctl("kickstart", f"{domain}/{LABEL}")
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        try:
            return {
                "status": "started",
                "service": request(root, "system.status"),
                "backup": str(backup)
            }
        except (OSError, ValueError, RuntimeError):
            time.sleep(0.25)
    return {
        "status": "error",
        "message": "后台未能连接，请查看 desktop.stderr.log。",
        "backup": str(backup),
        "rollback": rollback_service(root, backup, target)
    }


def stop(root: Path) -> dict:
    """当前任务结束后停用服务，保留任务与数据。"""
    try:
        tasks = request(root, "tasks.list").get("tasks", [])
        if any(
                task.get("status") in {"running", "cancel_requested"}
                for task in tasks):
            return {"status": "error", "message": "请先完成或取消当前任务，再停止后台。"}
    except (OSError, ValueError, RuntimeError):
        pass
    domain = f"gui/{os.getuid()}/{LABEL}"
    stopped = _launchctl("bootout", domain)
    if stopped.returncode and _launchctl("print", domain).returncode == 0:
        return {"status": "error", "message": stopped.stderr.strip()}
    _launchctl("disable", domain)
    if not wait_until_disconnected(root):
        return {"status": "error", "message": "后台尚未完全退出，请查看日志。"}
    return {"status": "stopped", "message": "后台已停止，记录和任务均保留。"}


def main(arguments: list[str] | None = None) -> None:
    """为桌面端返回结构化部署结果。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--app", type=Path)
    args = parser.parse_args(arguments)
    try:
        result = start(args.root, args.app) if args.action == "start" else stop(
            args.root)
    except (OSError, ValueError, RuntimeError,
            subprocess.SubprocessError) as error:
        result = {"status": "error", "message": str(error)}
    print(json.dumps(result, ensure_ascii=False))
