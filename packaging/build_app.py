"""构建独立 Python 运行时及 Apple Silicon 桌面应用。"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def main() -> None:
    """从锁定依赖构建应用，不包含个人资料或教学 Torch 环境。"""
    root = Path(__file__).resolve().parents[1]
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--name",
        "second-proprio-engine", "--paths",
        str(root / "src"), "--distpath",
        str(root / "packaging/runtime"), "--workpath",
        str(root / ".state/build/engine"), "--specpath",
        str(root / ".state/build"), "--collect-all",
        "jsonschema_specifications", "--add-data",
        f"{root / 'config/schema.json'}:resources/config", "--add-data",
        f"{root / 'config/settings.example.json'}:resources/config",
        "--add-data", f"{root / '.agents/skills'}:resources/skills",
        str(root / "packaging/engine_entry.py")
    ]
    subprocess.run(command, cwd=root, check=True)
    environment = os.environ.copy()
    environment["PATH"] = (str(Path.home() / ".cargo/bin") + os.pathsep +
                           environment.get("PATH", ""))
    subprocess.run(["npm", "run", "tauri", "--", "build", "--bundles", "app"],
                   cwd=root / "desktop",
                   env=environment,
                   check=True)
    app = (root / "desktop/src-tauri/target/release/bundle/macos" /
           "second proprio.app")
    # 仅供本机使用的临时签名同时绑定资源与通知所用的 Bundle ID。
    subprocess.run([
        "/usr/bin/codesign", "--force", "--deep", "--sign", "-", "--identifier",
        "com.secondproprio.desktop",
        str(app)
    ],
                   check=True)
    subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict",
         str(app)],
        check=True)
    print(f"构建完成：{app}")


if __name__ == "__main__":
    main()
