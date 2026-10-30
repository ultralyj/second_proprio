"""生成和安装本机启动任务，生成轻量服务器部署文件。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
from typing import Any

_LABEL = "com.second-proprio.worker"


def _deploy_dir(root: Path, config: dict[str, Any]) -> Path:
    """创建并返回项目专用部署文件目录。"""
    destination = root / config["paths"]["state"] / "deploy"
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def render_launchd(root: Path, python_exe: str | Path,
                   config: dict[str, Any]) -> Path:
    """生成 macOS 用户登录时启动的 launchd XML。

    Args:
        root: 项目根目录。
        python_exe: 已安装 second_proprio 的虚拟环境 Python。
        config: 已校验的业务配置。

    Returns:
        项目内生成的 plist 文件，尚未注册到系统。
    """
    root = root.resolve()
    python_exe = Path(python_exe).absolute()
    log_dir = root / config["paths"]["state"] / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    paths = [str(python_exe.parent), "/opt/homebrew/bin", "/usr/local/bin"]
    codex = shutil.which("codex")
    if codex:
        paths.insert(0, str(Path(codex).parent))
    paths.extend(["/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    plist = {
        "Label": _LABEL,
        "ProgramArguments": [
            str(python_exe), "-m", "second_proprio", "--root",
            str(root), "tick"
        ],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "StartInterval": config["scheduler"].get("sync_interval_seconds", 300),
        "ProcessType": "Background",
        "ThrottleInterval": 30,
        "ExitTimeOut": 30,
        "Umask": 0o077,
        "EnvironmentVariables": {
            "PATH": ":".join(dict.fromkeys(paths)),
            "PYTHONUNBUFFERED": "1",
        },
        "StandardOutPath": str(log_dir / "launchd.stdout.log"),
        "StandardErrorPath": str(log_dir / "launchd.stderr.log"),
    }
    destination = _deploy_dir(root, config) / f"{_LABEL}.plist"
    destination.write_bytes(plistlib.dumps(plist))
    return destination


def install_launchd(root: Path, python_exe: str | Path,
                    config: dict[str, Any]) -> dict[str, str]:
    """注册当前用户的本机后台任务，不修改系统级服务。

    Args:
        root: 项目根目录。
        python_exe: 项目虚拟环境中的 Python。
        config: 已校验的业务配置。

    Returns:
        已注册的服务标签和 plist 路径。

    Raises:
        RuntimeError: 平台、现有服务归属或 launchctl 操作不符合预期。
    """
    if sys.platform != "darwin":
        raise RuntimeError("install-local 仅支持 macOS。")
    root = root.resolve()
    python_exe = Path(python_exe).absolute()
    if not python_exe.is_file():
        raise RuntimeError(f"Python 不存在：{python_exe}")
    source = render_launchd(root, python_exe, config)
    target_dir = Path.home() / "Library" / "LaunchAgents"
    target = target_dir / source.name
    if target.exists():
        try:
            existing = plistlib.loads(target.read_bytes())
        except (OSError, plistlib.InvalidFileException) as error:
            raise RuntimeError("现有 launchd 文件无法验证归属。") from error
        if existing.get("WorkingDirectory") != str(root):
            raise RuntimeError("同名 launchd 任务属于另一目录，未覆盖。")
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{_LABEL}"
    inspection = subprocess.run(
        ["launchctl", "print", service],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if inspection.returncode == 0:
        stopped = subprocess.run(
            ["launchctl", "bootout", service],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if stopped.returncode:
            raise RuntimeError(f"无法停止旧任务：{stopped.stderr.strip()}")
    target_dir.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".plist.tmp")
    temporary.write_bytes(source.read_bytes())
    temporary.chmod(0o600)
    os.replace(temporary, target)
    installed = subprocess.run(
        ["launchctl", "bootstrap", domain,
         str(target)],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if installed.returncode:
        raise RuntimeError(f"无法注册后台任务：{installed.stderr.strip()}")
    status = {"status": "installed", "label": _LABEL, "path": str(target)}
    (_deploy_dir(root, config) / "local-install.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return status


def _systemd_arg(value: str) -> str:
    """按 systemd 字符串语法引用参数并转义说明符。"""
    value = value.replace("%", "%%")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd(root: Path, config: dict[str, Any]) -> list[Path]:
    """生成服务器服务、计时器和显式部署脚本，不执行 SSH。

    Args:
        root: 本机项目根目录，生成文件存放于其状态目录。
        config: 已校验的业务配置。

    Returns:
        service、timer、远端安装脚本和部署说明的路径列表。

    Raises:
        ValueError: 服务器目录无效或包含不能安全表达的换行。
    """
    remote_root = Path(config["server"]["root"])
    if not remote_root.is_absolute() or "\n" in str(remote_root):
        raise ValueError("服务器项目目录必须是单行绝对路径。")
    deployment = _deploy_dir(root, config)
    service = deployment / "second-proprio.service"
    timer = deployment / "second-proprio.timer"
    installer = deployment / "install-server.sh"
    readme = deployment / "README.md"
    python = remote_root / ".venv/bin/python"
    command = " ".join([
        _systemd_arg(str(python)),
        "-m",
        "second_proprio",
        "--root",
        _systemd_arg(str(remote_root)),
        "server-tick",
    ])
    working_directory = str(remote_root).replace("%", "%%")
    service.write_text(
        "[Unit]\nDescription=second proprio lightweight scheduler\n"
        "After=network-online.target\nWants=network-online.target\n\n"
        "[Service]\nType=oneshot\n"
        f"WorkingDirectory={working_directory}\n"
        f"ExecStart={command}\n"
        "TimeoutStartSec=600\nNice=10\nNoNewPrivileges=true\n"
        "PrivateTmp=true\nUMask=0077\n\n"
        "[Install]\nWantedBy=default.target\n",
        encoding="utf-8",
    )
    timer.write_text(
        "[Unit]\nDescription=second proprio queue check every minute\n\n"
        "[Timer]\nOnBootSec=30s\nOnUnitActiveSec=60s\n"
        "AccuracySec=10s\nUnit=second-proprio.service\n\n"
        "[Install]\nWantedBy=timers.target\n",
        encoding="utf-8",
    )
    installer.write_text(
        "#!/bin/sh\nset -eu\n"
        "# 在已上传项目的 Ubuntu 用户会话中运行；不上传或复制模型凭据。\n"
        f"project_dir={shlex.quote(str(remote_root))}\n"
        'cd "$project_dir"\n'
        "python3 -m venv .venv\n"
        '.venv/bin/python -m pip install --upgrade pip\n'
        '.venv/bin/python -m pip install -e .\n'
        'unit_dir="$HOME/.config/systemd/user"\n'
        'mkdir -p "$unit_dir"\n'
        'install -m 600 .state/deploy/second-proprio.service "$unit_dir/"\n'
        'install -m 600 .state/deploy/second-proprio.timer "$unit_dir/"\n'
        "systemctl --user daemon-reload\n"
        "systemctl --user enable --now second-proprio.timer\n"
        "systemctl --user status second-proprio.timer --no-pager\n",
        encoding="utf-8",
    )
    installer.chmod(0o700)
    readme.write_text(
        "# 服务器部署文件\n\n"
        "这些文件仅已生成，尚未证明远端部署成功。完整步骤见项目的"
        " `docs/deployment.md`。\n\n"
        "先上传脱敏配置和应用源码，再在远端运行 `install-server.sh`。"
        "安装只需要标准运行依赖，不安装课程的 PyTorch 环境。\n\n"
        "无人登录时持续运行需要管理员为 Ubuntu 用户启用 linger："
        "`sudo loginctl enable-linger ubuntu`。随后重启验证 timer 与"
        " `server-tick` 的行为；没有这个验证，不宣称关机离线调度已验收。\n",
        encoding="utf-8",
    )
    return [service, timer, installer, readme]
