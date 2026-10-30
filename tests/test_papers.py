"""用可重复的网络替身验证论文采集和资源准备行为。"""

import io
import json
from pathlib import Path
import tarfile
from unittest import mock
import urllib.parse

from second_proprio import papers


def _config() -> dict:
    """返回隔离测试用的最小配置。"""
    return {
        "paths": {
            "state": ".state",
            "papers": "papers"
        },
        "arxiv": {
            "lookback_days":
                7,
            "request_interval_seconds":
                3.1,
            "topics": [{
                "id": "dexterous-manipulation",
                "categories": ["cs.RO"],
                "keywords": ["dexterous"],
                "exclude_keywords": ["surgery"]
            }],
        }
    }


def _feed(version: int = 1, title: str = "Dexterous hand learning") -> bytes:
    """构建与 arXiv 元数据接口一致的 Atom 响应。"""
    return f'''<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>http://arxiv.org/abs/2610.12345v{version}</id>
      <title>{title}</title>
      <summary>A dexterous robot manipulates objects.</summary>
      <published>2026-10-24T12:00:00Z</published>
      <updated>2026-10-28T12:00:00Z</updated><category term="cs.RO"/>
      <author><name>Example Author</name></author></entry></feed>'''.encode()


def _seed(root: Path) -> dict:
    """放入一条已核对版本号的候选。"""
    record = papers._records(_feed())[0]  # pylint: disable=protected-access
    record["topic_id"] = "dexterous-manipulation"
    path = root / ".state" / "paper-candidates.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([record]), encoding="utf-8")
    return record


def test_collect_deduplicates_and_preserves_versions(tmp_path: Path) -> None:
    """RSS 与 API 的相同论文合并，更新版本仍独立保留。"""
    with mock.patch.object(papers, "_fetch", return_value=_feed()):
        first = papers.collect(tmp_path, _config())
        second = papers.collect(tmp_path, _config())
    assert len(first) == len(second) == 1
    with mock.patch.object(papers, "_fetch", return_value=_feed(2)):
        updated = papers.collect(tmp_path, _config())
    assert {record["version"] for record in updated} == {1, 2}
    assert updated[0]["paper_dir"].endswith("versions/v2")


def test_collect_excludes_and_survives_failure(tmp_path: Path) -> None:
    """排除词生效，后续网络故障不会清空历史候选。"""
    with mock.patch.object(papers, "_fetch", return_value=_feed()):
        papers.collect(tmp_path, _config())
    with mock.patch.object(papers,
                           "_fetch",
                           return_value=_feed(2, "Dexterous surgery")):
        assert len(papers.collect(tmp_path, _config())) == 1
    with mock.patch.object(papers, "_fetch", side_effect=OSError("offline")):
        assert len(papers.collect(tmp_path, _config())) == 1
    report = json.loads(
        (tmp_path / ".state/paper-collection-report.json").read_text())
    assert not report["complete"]
    assert len(report["errors"]) == 2


def test_collect_uses_utc_window_and_verifies_rss_version(
        tmp_path: Path) -> None:
    """验证检索请求包含日期窗，RSS 候选通过 ID 接口核对版本。"""
    urls = []

    def fetch(_root, _config_value, url):
        urls.append(url)
        return _feed()

    with mock.patch.object(papers, "_fetch", side_effect=fetch):
        papers.collect(tmp_path, _config())
    queries = [
        urllib.parse.parse_qs(urllib.parse.urlsplit(url).query) for url in urls
    ]
    assert any("id_list" in query for query in queries)
    assert any("submittedDate:[" in query.get("search_query", [""])[0]
               for query in queries)


def test_prepare_saves_local_figure_and_skips_done(tmp_path: Path) -> None:
    """准备本地原图、公式文本并在复跑时跳过已完成记录。"""
    _seed(tmp_path)
    html = b'''<html><article><h1>Dexterous hand</h1>
      <math alttext="a=b"><mi>a</mi></math><figure id="S1.F1">
      <img src="2610.12345v1/x1.png"/>
      <object data="2610.12345v1/chart.svg"></object>
      <figcaption>Figure 1: Pipeline.</figcaption>
      </figure></article></html>'''

    def fetch(_root, _config_value, url, **_kwargs):
        if url.endswith(".svg"):
            return b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
        return b"PNG_DATA" if url.endswith(".png") else html

    with mock.patch.object(papers, "_fetch", side_effect=fetch):
        result = papers.prepare(tmp_path, _config(), 1)
    assert len(result) == 1
    directory = tmp_path / result[0]["paper_dir"]
    assert (directory / "figures/figure-01-01.png").read_bytes() == b"PNG_DATA"
    assert (directory / "figures/figure-01-02.svg").exists()
    assert "$a=b$" in (directory / "source.txt").read_text()
    (directory / "README.md").write_text("已完成解读", encoding="utf-8")
    (directory / "review.json").write_text('{"status":"done"}')
    with mock.patch.object(papers, "_fetch") as fetch_mock:
        assert not papers.prepare(tmp_path, _config(), 1)
        fetch_mock.assert_not_called()


def test_prepare_preserves_partial_failure(tmp_path: Path) -> None:
    """图片失败时正文仍可审阅，状态明确暴露缺图。"""
    _seed(tmp_path)

    def fetch(_root, _config_value, url, **_kwargs):
        if url.endswith(".png"):
            raise OSError("image unavailable")
        return (b'<article>Text<figure>'
                b'<img src="https://arxiv.org/image.png"/>'
                b'</figure></article>')

    with mock.patch.object(papers, "_fetch", side_effect=fetch):
        result = papers.prepare(tmp_path, _config(), 1)
    assert result[0]["preparation"]["figures"][0]["status"] == "failed"


def test_source_archive_cannot_write_outside_paper(tmp_path: Path) -> None:
    """恶意归档路径不参与落盘路径，源码也不会被执行。"""
    record = _seed(tmp_path)
    package = io.BytesIO()
    with tarfile.open(fileobj=package, mode="w:gz") as archive:
        for name, content in [("../../outside.tex", b"\\section{Example}"),
                              ("../../outside.png", b"PNG")]:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        link = tarfile.TarInfo("external-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/tmp/should-not-be-used"
        archive.addfile(link)
    directory = tmp_path / "paper"
    directory.mkdir()
    with mock.patch.object(papers, "_fetch", return_value=package.getvalue()):
        result = papers._prepare_source(  # pylint: disable=protected-access
            tmp_path, _config(), record, directory)
    assert result["source_format"] == "tex"
    assert not (tmp_path / "outside.tex").exists()
    assert not (tmp_path / "outside.png").exists()
    assert (directory / "figures/source-001.png").exists()


def test_failed_preparation_is_not_success(tmp_path: Path) -> None:
    """各格式均失败时留下错误状态，不返回可解读论文。"""
    _seed(tmp_path)
    with mock.patch.object(papers, "_fetch", side_effect=OSError("offline")):
        assert not papers.prepare(tmp_path, _config(), 1)
    path = next((tmp_path / "papers").rglob("preparation.json"))
    report = json.loads(path.read_text())
    assert report["status"] == "failed"
    assert len(report["errors"]) == 3
    summary = json.loads(
        (tmp_path / ".state/paper-preparation-report.json").read_text())
    assert summary["attempted"] == 1
    assert summary["prepared"] == 0
    assert not summary["complete"]


def test_partial_readme_does_not_skip_retry(tmp_path: Path) -> None:
    """中断留下的半成品 README 不应阻止下次重试。"""
    record = _seed(tmp_path)
    directory = papers._paper_directory(  # pylint: disable=protected-access
        tmp_path, _config(), record)
    directory.mkdir(parents=True)
    (directory / "README.md").write_text("半成品", encoding="utf-8")
    with mock.patch.object(papers,
                           "_fetch",
                           return_value=b'<article>Text</article>'):
        assert len(papers.prepare(tmp_path, _config(), 1)) == 1


def test_fetch_cache_and_minimum_interval(tmp_path: Path) -> None:
    """缓存命中不联网，配置过小的间隔仍提升至三秒。"""
    config = _config()
    config["arxiv"]["request_interval_seconds"] = 0
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.url = "https://arxiv.org/html/2610.12345v1"
    response.read.return_value = b"data"
    with (mock.patch.object(papers, "_LAST_REQUEST", 100),
          mock.patch.object(papers.time, "monotonic", return_value=100),
          mock.patch.object(papers.time, "sleep") as sleep,
          mock.patch.object(papers.urllib.request,
                            "urlopen",
                            return_value=response) as opened):
        papers._fetch(tmp_path, config, response.url)  # pylint: disable=protected-access
        papers._fetch(tmp_path, config, response.url)  # pylint: disable=protected-access
    sleep.assert_called_once_with(3.0)
    opened.assert_called_once()


def test_xml_entities_and_unsafe_paths_rejected(tmp_path: Path) -> None:
    """外部实体与越界路径在读取和写入前被拒绝。"""
    import pytest  # pylint: disable=import-outside-toplevel

    with pytest.raises(ValueError):
        papers._records(b'<!DOCTYPE x [<!ENTITY a "bad">]><x/>')  # pylint: disable=protected-access
    config = _config()
    config["paths"]["state"] = "../escape"
    with pytest.raises(ValueError):
        papers.collect(tmp_path, config)
