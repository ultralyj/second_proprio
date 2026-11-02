"""读取、校验和原子保存 JSON 配置。"""

import hashlib
import json
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def resolve_review_source(root: Path, config: dict) -> Path:
    """解析只读聊天来源；外部绝对目录不会改变私密资料存储位置。

    Args:
        root: 项目根目录。
        config: 业务配置。

    Returns:
        显式配置的绝对来源目录，或私密目录内的相对来源目录。

    Raises:
        ValueError: 路径为空、相对路径越出私密目录或目标不是目录。
    """
    value = config["review"]["source_directory"]
    if not value.strip() or "\x00" in value:
        raise ValueError("请填写聊天记录目录")
    source = Path(value).expanduser()
    if not source.is_absolute():
        source = resolve_path(root, value)
        private = resolve_path(root, config["paths"]["private"])
        if not source.is_relative_to(private):
            raise ValueError("相对聊天来源目录必须位于私密目录内")
    source = source.resolve()
    if source.exists() and not source.is_dir():
        raise ValueError("聊天来源必须是目录，请选择 JSON 文件所在的文件夹")
    return source


def validate(root: Path, value: dict) -> None:
    """校验配置及数据目录边界，不写入配置文件。

    Args:
        root: 项目根目录，提供当前版本的 JSON Schema。
        value: 待校验的完整业务配置。

    Raises:
        jsonschema.ValidationError: 字段类型或取值不符合 Schema。
        ValueError: 时区无效，或数据目录越界、重叠、占用项目根目录。
    """
    schema = json.loads((root / "config/schema.json").read_text("utf-8"))
    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()).validate(value)
    directories = []
    for relative in value["paths"].values():
        directory = resolve_path(root, relative)
        if directory == root.resolve():
            raise ValueError("数据目录不能使用项目根目录")
        if any(
                directory.is_relative_to(other) or
                other.is_relative_to(directory) for other in directories):
            raise ValueError("论文、课程、私密记录和运行状态目录不能相同或嵌套")
        directories.append(directory)
    resolve_review_source(root, value)
    try:
        ZoneInfo(value["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"无效时区：{value['timezone']}") from error


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
