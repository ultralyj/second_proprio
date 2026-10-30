"""读取、校验和原子保存 JSON 配置。"""

import hashlib
import json
import os
from pathlib import Path
import tempfile

import jsonschema


def atomic_json(path: Path, value: object) -> None:
    """在同一文件系统原子替换 JSON，避免中断产生半个文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def revision(value: object) -> str:
    """计算顺序无关的配置修订号。"""
    data = json.dumps(value,
                      sort_keys=True,
                      ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def resolve_path(root: Path, relative: str) -> Path:
    """解析仓库内路径，拒绝越界和指向仓库外部的符号链接。"""
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError(f"路径必须位于项目内：{relative}")
    return candidate


def validate(root: Path, value: dict) -> None:
    """根据版本化 Schema 校验配置及目录约束。"""
    schema = json.loads((root / "config/schema.json").read_text("utf-8"))
    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()).validate(value)
    for relative in value["paths"].values():
        resolve_path(root, relative)
    private = resolve_path(root, value["paths"]["private"])
    source = resolve_path(root, value["review"]["source_directory"])
    if not source.is_relative_to(private):
        raise ValueError("聊天来源目录必须位于私密目录内")
    from zoneinfo import ZoneInfo  # pylint: disable=import-outside-toplevel
    ZoneInfo(value["timezone"])


def load(root: Path) -> dict:
    """读取用户配置；首次运行时使用模板，非法配置不会被执行。"""
    path = root / "config/settings.json"
    if not path.exists():
        path = root / "config/settings.example.json"
    value = json.loads(path.read_text("utf-8"))
    validate(root, value)
    return value


def apply(root: Path, source: Path) -> dict:
    """只在候选配置通过校验后替换当前配置。"""
    value = json.loads(source.read_text("utf-8"))
    validate(root, value)
    atomic_json(root / "config/settings.json", value)
    # 自定义私密目录同步纳入 Git 本地排除，避免误加入版本控制。
    exclude = root / ".git/info/exclude"
    if (root / ".git").is_dir():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text("utf-8") if exclude.exists() else ""
        additions = [
            f"/{value['paths'][key].strip('/')}/" for key in ("private",
                                                              "state")
        ]
        with exclude.open("a", encoding="utf-8") as stream:
            for entry in additions:
                if entry not in existing.splitlines():
                    stream.write("\n" + entry + "\n")
    return value
