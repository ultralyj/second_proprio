"""验证个人论文评分的版本共享、边界及隐私存放位置。"""

import json
from pathlib import Path

import pytest

from second_proprio import config as configuration
from second_proprio import desktop_content


@pytest.fixture(name="project")
def fixture_project(tmp_path):
    """创建两版论文和独立私密目录。"""
    root = Path(__file__).resolve().parents[1]
    config = json.loads(
        (root / "config/settings.example.json").read_text("utf-8"))
    for version in (1, 2):
        configuration.atomic_json(
            tmp_path / f"papers/2610.12345/v{version}/metadata.json", {
                "id": "2610.12345",
                "version": version,
                "title": "测试论文"
            })
    return tmp_path, config


def test_rating_is_private_and_shared_by_paper_versions(project):
    """评分独立于公开元数据，后续版本共享且可以清除。"""
    root, config = project
    metadata = root.rglob("metadata.json")
    originals = {path: path.read_bytes() for path in metadata}
    desktop_content.dispatch(root, config, "content.rate", {
        "arxiv_id": "2610.12345",
        "score": 4
    })
    listed = desktop_content.list_content(root, config, "papers")["items"]
    assert [item["rating"] for item in listed] == [4, 4]
    assert (root / "private/paper-ratings.json").exists()
    assert all(path.read_bytes() == data for path, data in originals.items())
    desktop_content.dispatch(root, config, "content.rate", {
        "arxiv_id": "2610.12345",
        "score": None
    })
    assert all(item["rating"] is None for item in desktop_content.list_content(
        root, config, "papers")["items"])


@pytest.mark.parametrize("score", [True, False, 0, 6, 2.5, "4"])
def test_invalid_rating_is_rejected_without_writing(project, score):
    """非法评分不落盘，也不改写论文。"""
    root, config = project
    with pytest.raises(ValueError):
        desktop_content.save_paper_rating(root, config, {
            "arxiv_id": "2610.12345",
            "score": score
        })
    assert not (root / "private").exists()


def test_missing_paper_cannot_create_rating(project):
    """拒绝不存在的 ID 和路径注入。"""
    root, config = project
    for identifier in ("missing", "../../config/settings.json", None):
        with pytest.raises(ValueError):
            desktop_content.save_paper_rating(root, config, {
                "arxiv_id": identifier,
                "score": 3
            })
    assert not (root / "private").exists()


@pytest.mark.parametrize("filename", ["source.pdf", "paper.pdf", None])
def test_paper_pdf_entry_resolves_existing_original_only(project, filename):
    """原文入口兼容两种已有文件名，缺失时不返回无效链接。"""
    root, config = project
    directory = root / "papers/2610.12345/v1"
    if filename:
        (directory / filename).write_bytes(b"%PDF-test")
    item = next(item for item in desktop_content.list_content(
        root, config, "papers")["items"] if item["version"] == 1)
    assert item["pdf_path"] == (str(
        (directory / filename).relative_to(root)) if filename else None)
