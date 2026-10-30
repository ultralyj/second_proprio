"""采集 arXiv 候选，并准备可追溯的论文原文和插图。"""

from __future__ import annotations

import datetime
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as element_tree

from bs4 import BeautifulSoup

_ID_PATTERN = re.compile(
    r"(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v(\d+))?")
_LAST_REQUEST = 0.0
_ALLOWED_HOSTS = {"arxiv.org", "export.arxiv.org", "rss.arxiv.org"}
_MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024


def _write_json(path: Path, value: object) -> None:
    """原子替换 JSON，避免中断留下不完整文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path, default: object) -> object:
    """读取状态；损坏状态抛错，避免静默覆盖人工维护的数据。"""
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _directory(root: Path, config: dict, name: str) -> Path:
    """验证输出目录位于项目根目录内。"""
    result = (root / config.get("paths", {}).get(name, name)).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError(f"目录必须位于项目内：{name}")
    return result


def _fetch(root: Path, config: dict, url: str, *, ttl: int = 3600) -> bytes:
    """限速获取公开资源，并缓存成功响应。

    Args:
        root: 项目根目录。
        config: 项目 JSON 配置。
        url: arXiv 官方资源地址。
        ttl: 缓存有效秒数；负数表示永久缓存。

    Returns:
        资源的原始字节。

    Raises:
        ValueError: 资源地址或体积超出允许范围。
        OSError: 网络失败，有限重试后仍不可用。
    """
    global _LAST_REQUEST  # pylint: disable=global-statement
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
        raise ValueError("仅允许 HTTPS arXiv 官方资源")
    cache = _directory(root, config, "state") / "arxiv-cache"
    key = hashlib.sha256(url.encode()).hexdigest()
    body_file = cache / (key + ".bin")
    if body_file.exists() and (ttl < 0 or
                               time.time() - body_file.stat().st_mtime < ttl):
        return body_file.read_bytes()
    interval = max(
        3.0,
        float(config.get("arxiv", {}).get("request_interval_seconds", 3.1)))
    for attempt in range(3):
        time.sleep(max(0, interval - (time.monotonic() - _LAST_REQUEST)))
        _LAST_REQUEST = time.monotonic()
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "second-proprio/0.1 (personal research reader)"
            })
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                if urllib.parse.urlsplit(
                        response.url).hostname not in _ALLOWED_HOSTS:
                    raise ValueError("资源重定向到非 arXiv 地址")
                data = response.read(_MAX_DOWNLOAD_BYTES + 1)
            if len(data) > _MAX_DOWNLOAD_BYTES:
                raise ValueError("资源超过 30 MiB，需人工或专用下载流程处理")
            cache.mkdir(parents=True, exist_ok=True)
            temporary = body_file.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(body_file)
            return data
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(min(20, 3 * (attempt + 1)))
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))
    raise OSError("arXiv 请求失败")


def _text(node: element_tree.Element, name: str) -> str:
    """读取忽略命名空间的直接子元素文本。"""
    for child in node:
        if child.tag.rsplit("}", 1)[-1] == name:
            return " ".join("".join(child.itertext()).split())
    return ""


def _records(data: bytes) -> list[dict]:
    """统一解析 Atom 与 RSS，保留元数据来源可见的版本。"""
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("不接受包含实体声明的 XML")
    document = element_tree.fromstring(data)
    records = []
    for node in document.iter():
        kind = node.tag.rsplit("}", 1)[-1]
        if kind not in ("entry", "item"):
            continue
        identifier = _text(node, "id") or _text(node, "guid") or _text(
            node, "link")
        match = _ID_PATTERN.search(identifier)
        if not match:
            continue
        paper_id, version = match.groups()
        categories = []
        for child in node:
            if child.tag.rsplit("}", 1)[-1] in ("category", "primary_category"):
                category = child.attrib.get("term") or (child.text or
                                                        "").strip()
                categories.extend(category.split())
        abstract = _text(node, "summary") or _text(node, "description")
        abstract = BeautifulSoup(abstract, "html.parser").get_text(" ",
                                                                   strip=True)
        authors = [
            _text(child, "name")
            for child in node
            if child.tag.rsplit("}", 1)[-1] == "author"
        ]
        records.append({
            "id": paper_id,
            "version": int(version or 1),
            "version_verified": bool(version),
            "title": _text(node, "title"),
            "abstract": abstract,
            "authors": [author for author in authors if author],
            "url": f"https://arxiv.org/abs/{paper_id}v{version or 1}",
            "published": _text(node, "published") or _text(node, "pubDate"),
            "updated": _text(node, "updated"),
            "categories": sorted(set(categories)),
            "metadata_source": kind,
        })
    return records


def _relevance(record: dict, topic: dict) -> list[str]:
    """返回关键词命中理由；本步骤不等于语义相关性审核。"""
    content = (record.get("title", "") + " " +
               record.get("abstract", "")).casefold()
    if any(word.casefold() in content
           for word in topic.get("exclude_keywords", [])):
        return []
    if record.get("categories") and not set(record["categories"]).intersection(
            topic.get("categories", [])):
        return []
    return [
        word for word in topic.get("keywords", []) if word.casefold() in content
    ]


def _paper_directory(root: Path, config: dict, record: dict) -> Path:
    """构造稳定的论文版本目录，阻止配置中的路径穿越。"""
    topic_id = record["topic_id"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", topic_id):
        raise ValueError("topic id 仅允许小写字母、数字与连字符")
    paper_id = record["id"]
    if not _ID_PATTERN.fullmatch(paper_id):
        raise ValueError("无效的 arXiv ID")
    return (_directory(root, config, "papers") / topic_id /
            paper_id.replace("/", "_") / "versions" /
            f"v{int(record['version'])}")


def collect(root: Path, config: dict) -> list[dict]:
    """采集并合并论文候选，网络局部失败不会丢弃已有队列。

    Args:
        root: 项目根目录。
        config: 包含 paths 与 arxiv 的 JSON 配置。

    Returns:
        按主题、arXiv ID 与版本去重的候选列表。关键词命中仅用于初筛。
    """
    root = Path(root).resolve()
    settings = config.get("arxiv", {})
    state = _directory(root, config, "state")
    candidate_file = state / "paper-candidates.json"
    previous = _read_json(candidate_file, [])
    merged = {
        (item["topic_id"], item["id"], item["version"]): item
        for item in previous
    }
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now - datetime.timedelta(days=int(settings.get("lookback_days", 7)))
    failures = []
    for topic in settings.get("topics", []):
        records = []
        categories = topic.get("categories", [])
        if not categories or not topic.get("keywords"):
            failures.append({"topic": topic.get("id"), "error": "分类与关键词不可为空"})
            continue
        rss_url = "https://rss.arxiv.org/atom/" + "+".join(categories)
        try:
            rss_records = _records(_fetch(root, config, rss_url))
            rss_records = [
                item for item in rss_records if _relevance(item, topic)
            ]
            # RSS 可能省略版本，因此用 ID 查询核对后才允许生成版本目录。
            for offset in range(0, len(rss_records), 50):
                identifiers = ",".join(
                    item["id"] for item in rss_records[offset:offset + 50])
                url = "https://export.arxiv.org/api/query?" + (
                    urllib.parse.urlencode({
                        "id_list": identifiers,
                        "max_results": 50
                    }))
                records.extend(_records(_fetch(root, config, url)))
        except (OSError, ValueError, element_tree.ParseError) as error:
            failures.append({
                "topic": topic["id"],
                "source": "rss",
                "error": str(error)
            })
        category_query = " OR ".join(
            f"cat:{category}" for category in categories)
        keyword_query = " OR ".join(
            f'all:"{word.replace(chr(34), " ")}"' for word in topic["keywords"])
        query = (f"({category_query}) AND ({keyword_query}) AND "
                 f"submittedDate:[{start:%Y%m%d%H%M} TO {now:%Y%m%d%H%M}]")
        try:
            for offset in range(0, 1000, 100):
                url = "https://export.arxiv.org/api/query?" + (
                    urllib.parse.urlencode({
                        "search_query": query,
                        "sortBy": "submittedDate",
                        "sortOrder": "descending",
                        "start": offset,
                        "max_results": 100,
                    }))
                batch = _records(_fetch(root, config, url))
                records.extend(batch)
                if len(batch) < 100:
                    break
                if offset == 900:
                    failures.append({
                        "topic": topic["id"],
                        "error": "命中超过 1000 条，需缩小检索范围"
                    })
        except (OSError, ValueError, element_tree.ParseError) as error:
            failures.append({
                "topic": topic["id"],
                "source": "api",
                "error": str(error)
            })
        for record in records:
            matched = _relevance(record, topic)
            if not matched or not record["version_verified"]:
                continue
            record.update({
                "topic_id": topic["id"],
                "reason": "关键词初筛命中：" + "、".join(matched),
                "semantic_review": "pending",
                "collected_at": now.isoformat()
            })
            record["paper_dir"] = str(
                _paper_directory(root, config, record).relative_to(root))
            key = (topic["id"], record["id"], record["version"])
            merged[key] = {**merged.get(key, {}), **record}
    result = sorted(merged.values(),
                    key=lambda item: (item.get("updated") or item.get(
                        "published", ""), item["id"], item["version"]),
                    reverse=True)
    _write_json(candidate_file, result)
    _write_json(
        state / "paper-collection-report.json", {
            "collected_at": now.isoformat(),
            "candidate_count": len(result),
            "errors": failures,
            "complete": not failures,
            "note": "关键词初筛不等于高度相关；提交日期也不等于公告日期。",
        })
    return result


def _prepare_html(root: Path, config: dict, record: dict,
                  directory: Path) -> dict:
    """保存 HTML、结构化正文与图像清单；逐图失败保留原因。"""
    url = f"https://arxiv.org/html/{record['id']}v{record['version']}"
    data = _fetch(root, config, url, ttl=-1)
    soup = BeautifulSoup(data, "html.parser")
    article = soup.find("article")
    if article is None:
        raise ValueError("HTML 没有论文 article，不能作为完整正文")
    (directory / "source.html").write_bytes(data)
    for unwanted in article.find_all(["script", "style"]):
        unwanted.decompose()
    # 保留原始 TeX，便于解释符号并与 PDF 交叉核对。
    for math in article.find_all("math"):
        latex = math.get("alttext")
        if latex:
            math.replace_with(" $" + latex + "$ ")
    (directory / "source.txt").write_text(article.get_text("\n", strip=True),
                                          encoding="utf-8")
    figures = []
    for number, figure in enumerate(article.find_all("figure"), start=1):
        caption = figure.find("figcaption")
        caption_text = caption.get_text(" ", strip=True) if caption else "未提供图注"
        for part, picture in enumerate(figure.find_all(["img", "object"]),
                                       start=1):
            image_url = urllib.parse.urljoin(
                url,
                picture.get("src") or picture.get("data", ""))
            suffix = Path(urllib.parse.urlsplit(image_url).path).suffix.lower()
            if suffix not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"):
                suffix = ".bin"
            relative = f"figures/figure-{number:02d}-{part:02d}{suffix}"
            entry = {
                "figure_id": figure.get("id"),
                "caption": caption_text,
                "source_url": image_url,
                "path": relative
            }
            try:
                image = _fetch(root, config, image_url, ttl=-1)
                destination = directory / relative
                destination.parent.mkdir(exist_ok=True)
                destination.write_bytes(image)
                entry["status"] = "saved"
            except (OSError, ValueError) as error:
                entry.update({"status": "failed", "error": str(error)})
            figures.append(entry)
    _write_json(directory / "figures.json", figures)
    return {
        "source_format": "html",
        "source_url": url,
        "figures": figures,
        "figure_coverage": "HTML 中 figure 的 img/object 资源；不保证覆盖 PDF 全部图像"
    }


def _prepare_source(root: Path, config: dict, record: dict,
                    directory: Path) -> dict:
    """安全读取源码包，不运行 TeX，也不信任包内路径与符号链接。"""
    url = f"https://arxiv.org/src/{record['id']}v{record['version']}"
    data = _fetch(root, config, url, ttl=-1)
    texts = []
    figures = []
    total = 0
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.ReadError as error:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            raw = stream.read(_MAX_DOWNLOAD_BYTES + 1)
        if len(raw) > _MAX_DOWNLOAD_BYTES:
            raise ValueError("单文件源码解压后过大") from error
        texts.append(raw.decode("utf-8", errors="replace"))
    else:
        with archive:
            for index, member in enumerate(archive):
                if index > 1000:
                    raise ValueError("源码文件数量过多")
                if not member.isfile():
                    continue
                suffix = Path(member.name).suffix.lower()
                if suffix not in (".tex", ".png", ".jpg", ".jpeg", ".pdf",
                                  ".eps", ".svg"):
                    continue
                total += member.size
                if (member.size > _MAX_DOWNLOAD_BYTES or
                        total > 60 * 1024 * 1024):
                    raise ValueError("源码解压大小超出限制")
                stream = archive.extractfile(member)
                if stream is None:
                    continue
                raw = stream.read(_MAX_DOWNLOAD_BYTES + 1)
                if suffix == ".tex":
                    texts.append(f"\n% 原始文件：{member.name}\n" +
                                 raw.decode("utf-8", errors="replace"))
                else:
                    relative = f"figures/source-{index:03d}{suffix}"
                    destination = directory / relative
                    destination.parent.mkdir(exist_ok=True)
                    destination.write_bytes(raw)
                    figures.append({
                        "path": relative,
                        "source_name": member.name,
                        "status": "saved",
                        "caption": "源码素材，需从 TeX 核对图号和图注"
                    })
    if not texts:
        raise ValueError("源码包未发现可读 TeX 正文")
    (directory / "source.txt").write_text("\n".join(texts), encoding="utf-8")
    _write_json(directory / "figures.json", figures)
    return {
        "source_format": "tex",
        "source_url": url,
        "figures": figures,
        "figure_coverage": "仅保留支持的源码素材；图号关联尚需审核"
    }


def _prepare_pdf(root: Path, config: dict, record: dict,
                 directory: Path) -> dict:
    """PDF 兜底只抽取文本并保存原件，插图未提取时明确标记。"""
    from pypdf import PdfReader  # pylint: disable=import-outside-toplevel

    url = f"https://arxiv.org/pdf/{record['id']}v{record['version']}"
    data = _fetch(root, config, url, ttl=-1)
    if not data.startswith(b"%PDF"):
        raise ValueError("下载响应不是 PDF")
    (directory / "paper.pdf").write_bytes(data)
    reader = PdfReader(io.BytesIO(data))
    text = "\n\n".join(f"## 原文第 {number} 页\n{page.extract_text() or ''}"
                       for number, page in enumerate(reader.pages, start=1))
    if len(text.strip()) < 100:
        raise ValueError("PDF 缺少可读文本，需要 OCR 或视觉阅读")
    (directory / "source.txt").write_text(text, encoding="utf-8")
    _write_json(directory / "figures.json", [])
    return {
        "source_format": "pdf",
        "source_url": url,
        "figures": [],
        "figure_coverage": "PDF 原件已保留；插图尚未提取，需视觉复核和裁图"
    }


def prepare(root: Path, config: dict, limit: int = 3) -> list[dict]:
    """为未解读候选准备全文与图像，返回可交给语义审核的记录。

    Args:
        root: 项目根目录。
        config: 项目 JSON 配置。
        limit: 本次最多准备的论文数量。

    Returns:
        不超过 limit 条记录，paper_dir 使用相对于项目的路径。
        失败论文记录 preparation.json，但不冒充成功全文。
    """
    root = Path(root).resolve()
    candidates = _read_json(
        _directory(root, config, "state") / "paper-candidates.json", [])
    prepared = []
    attempted = 0
    failed = []
    for record in candidates:
        if attempted >= max(0, limit):
            break
        directory = _paper_directory(root, config, record)
        review = _read_json(directory / "review.json", {})
        readme = directory / "README.md"
        if review.get("status") == "rejected" or (review.get("status") == "done"
                                                  and readme.exists() and
                                                  readme.stat().st_size > 0):
            continue
        attempted += 1
        directory.mkdir(parents=True, exist_ok=True)
        _write_json(directory / "metadata.json", record)
        errors = []
        details = None
        for handler in (_prepare_html, _prepare_source, _prepare_pdf):
            try:
                details = handler(root, config, record, directory)
                break
            except (OSError, ValueError, EOFError, tarfile.TarError,
                    ImportError) as error:
                errors.append({"stage": handler.__name__, "error": str(error)})
            except Exception as error:  # pylint: disable=broad-exception-caught
                # 第三方 PDF 解码异常仅隔离当前候选，后续论文仍可处理。
                errors.append({"stage": handler.__name__, "error": str(error)})
        status = {
            "prepared_at":
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status":
                "ready_for_review" if details else "failed",
            "errors":
                errors,
            **(details or {})
        }
        _write_json(directory / "preparation.json", status)
        if details:
            prepared.append({
                **record, "paper_dir": str(directory.relative_to(root)),
                "preparation": status
            })
        else:
            failed.append({
                "id": record["id"],
                "version": record["version"],
                "errors": errors
            })
    _write_json(
        _directory(root, config, "state") / "paper-preparation-report.json", {
            "attempted":
                attempted,
            "prepared":
                len(prepared),
            "errors":
                failed,
            "complete":
                not failed,
            "prepared_at":
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
    return prepared
