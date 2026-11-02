"""验证论文阅读结构、原图路径与验收状态的真实边界。"""

# pylint: disable=protected-access

import datetime as dt
import json
from pathlib import Path

import pytest

from second_proprio import papers
from second_proprio import runner
from second_proprio.store import Store


def _readme(folder: Path) -> str:
    """生成带可读本地图的最小验收文档。"""
    (folder / "figures").mkdir(parents=True, exist_ok=True)
    (folder / "figures/method.svg").write_text('<svg xmlns="http://www.w3.org/'
                                               '2000/svg"></svg>')
    text = """# Test paper

**作者与单位**：Example Author（原文未标注单位）。
**arXiv**：2610.12345v1
**时间**：2026-10-29 12:00:00 UTC

## 中文摘要

本文研究在保持接触的条件下生成机器人手部动作，并比较所提出的控制方法。

## Key insight

作者提出用接触约束保持动作可执行性；解读判断：收益仍需跨物体实验验证。

## 方法管线

```mermaid
flowchart LR
    A[观测] --> B[策略] --> C[动作]
```

## 方法详解

![原文方法图](figures/method.svg)

原文式 (1)：$a=\\pi(o)$，其中 o 为观测，a 为动作。

## 实验

在同一任务与数据设置下比较，未给足够统计信息，无法判断泛化收益。
"""
    (folder / "README.md").write_text(text)
    return text


def test_paper_validation_checks_images_inside_the_paper(tmp_path):
    """模板正确仍须检查真实文件，且不能通过链接读取版本目录外文件。"""
    folder = tmp_path / "paper"
    text = _readme(folder)
    runner.verify_paper_readme(folder)
    (tmp_path / "outside.svg").write_text("external")
    (folder / "README.md").write_text(
        text.replace("figures/method.svg", "../outside.svg"))
    with pytest.raises(ValueError, match="越界"):
        runner.verify_paper_readme(folder)
    (folder / "README.md").write_text(text)
    (folder / "figures/method.svg").unlink()
    with pytest.raises(ValueError, match="不可用"):
        runner.verify_paper_readme(folder)


def test_failed_paper_validation_does_not_consume_budget(tmp_path, monkeypatch):
    """模型声称完成但漏掉摘要时，不增加配额或把论文标为完成。"""
    config = {
        "paths": {
            "state": ".state",
            "papers": "papers"
        },
        "arxiv": {
            "max_papers_per_day": 3
        },
    }
    folder = tmp_path / "papers/topic/2610.12345/versions/v1"
    text = _readme(folder)
    record = {"id": "2610.12345", "paper_dir": str(folder)}
    monkeypatch.setattr(papers, "collect", lambda *_args: [])
    monkeypatch.setattr(papers, "prepare", lambda *_args, **_kwargs: [record])
    monkeypatch.setattr(
        runner, "codex", lambda *_args: {
            "status": "completed",
            "artifacts": [],
            "summary": "完成"
        })
    (folder / "README.md").write_text(text.replace("## 中文摘要", "## 简短概述"))
    (folder / "review.json").write_text('{"status":"done"}')
    store = Store(tmp_path / ".state/queue.sqlite3")
    try:
        with pytest.raises(ValueError, match="缺少摘要"):
            runner._papers(
                tmp_path,
                config,
                store,  # pylint: disable=protected-access
                dt.datetime(2026, 10, 29, tzinfo=dt.timezone.utc))
        assert not store.get_setting("paper_count:2026-10-29")
        assert json.loads(
            (folder / "review.json").read_text())["status"] == "needs_review"
        (folder / "README.md").write_text(text)
        (folder / "review.json").write_text(
            json.dumps({
                "reason": "接触控制实验与主题相关",
                "affiliation_evidence": "原文署名区未注明单位",
            }))
        runner._papers(
            tmp_path,
            config,
            store,  # pylint: disable=protected-access
            dt.datetime(2026, 10, 29, tzinfo=dt.timezone.utc))
        review = json.loads((folder / "review.json").read_text())
        assert review["status"] == "done"
        assert review["affiliation_evidence"] == "原文署名区未注明单位"
        assert review["reason"] == "接触控制实验与主题相关"
        assert store.get_setting("paper_count:2026-10-29") == "1"
    finally:
        store.close()


def test_html_affiliations_are_evidence_not_inferred_mapping(
        tmp_path, monkeypatch):
    """HTML 把共同单位挂在单人名下时，保留原片段但不假定归属已核实。"""
    html = b'''<article><div class="ltx_authors">
      <span class="ltx_creator ltx_role_author">
      <span class="ltx_personname">Author A</span>
      <span class="ltx_role_affiliation">Affiliation: University One</span>
      <span class="ltx_role_affiliation">Affiliation: University Two</span>
      </span><span class="ltx_creator ltx_role_author">
      <span class="ltx_personname">Author B</span></span></div></article>'''
    monkeypatch.setattr(papers, "_fetch", lambda *_args, **_kwargs: html)
    record = {"id": "2610.12345", "version": 1}
    result = papers._prepare_html(
        tmp_path,
        {},
        record,  # pylint: disable=protected-access
        tmp_path)
    evidence = result["author_affiliation_evidence"]
    assert evidence["association_verified"] is False
    assert evidence["author_blocks"] == [{
        "author_text": "Author A",
        "affiliation_text": ["University One", "University Two"],
    }]
