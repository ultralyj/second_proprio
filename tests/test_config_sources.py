"""验证首页配置和外部只读复盘来源的路径边界。"""

import json
from pathlib import Path
import shutil

import jsonschema
import pytest

from second_proprio import config as configuration


@pytest.fixture(name="project")
def fixture_project(tmp_path):
    """只复制公开配置与 Schema。"""
    source = Path(__file__).resolve().parents[1] / "config"
    (tmp_path / "config").mkdir()
    shutil.copy2(source / "schema.json", tmp_path / "config/schema.json")
    value = json.loads((source / "settings.example.json").read_text("utf-8"))
    return tmp_path, value


def test_explicit_external_directory_does_not_relax_content_paths(project):
    """外部聊天来源可以尚不存在，公开文件接口仍拒绝越界。"""
    root, config = project
    source = root.parent / "external-chat-records"
    config["review"]["source_directory"] = str(source)
    configuration.validate(root, config)
    assert configuration.resolve_review_source(root, config) == source
    assert not source.exists()
    with pytest.raises(ValueError):
        configuration.resolve_path(root, str(source))


@pytest.mark.parametrize("path", ["papers", "../chat", " "])
def test_relative_source_must_remain_private(project, path):
    """相对路径不能绕过私密目录限制。"""
    root, config = project
    config["review"]["source_directory"] = path
    with pytest.raises(ValueError):
        configuration.validate(root, config)


def test_source_must_be_directory(project):
    """把 JSON 文件误填为目录时就地报错。"""
    root, config = project
    file = root / "records.json"
    file.write_text("[]", encoding="utf-8")
    config["review"]["source_directory"] = str(file)
    with pytest.raises(ValueError, match="目录"):
        configuration.validate(root, config)


def test_blank_home_question_rejected_and_old_config_accepted(project):
    """旧配置无需迁移即可使用默认问题，空问题无法保存。"""
    root, config = project
    config["desktop"].pop("home_question", None)
    configuration.validate(root, config)
    config["desktop"]["home_question"] = "  "
    with pytest.raises(jsonschema.ValidationError):
        configuration.validate(root, config)


@pytest.mark.parametrize("key,path", [("private", "papers"),
                                      ("private", "papers/notes"),
                                      ("papers", "private/papers"),
                                      ("state", ".")])
def test_data_directories_cannot_overlap(project, key, path):
    """私密资料、发布成果和运行状态不能互相嵌套或占用仓库根目录。"""
    root, config = project
    config["paths"][key] = path
    with pytest.raises(ValueError, match="目录"):
        configuration.validate(root, config)


def test_directory_symlink_alias_cannot_bypass_separation(project):
    """不同字符串解析为同一个目录时也必须阻止配置生效。"""
    root, config = project
    (root / "papers").mkdir()
    (root / "alias").symlink_to(root / "papers", target_is_directory=True)
    config["paths"]["private"] = "alias"
    with pytest.raises(ValueError, match="不能相同或嵌套"):
        configuration.validate(root, config)


def test_invalid_timezone_returns_validation_error(project):
    """不存在的时区以业务校验错误返回，避免泄露底层 KeyError。"""
    root, config = project
    config["timezone"] = "Invalid/NotAZone"
    with pytest.raises(ValueError, match="无效时区"):
        configuration.validate(root, config)
