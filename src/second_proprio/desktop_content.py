"""桌面内容查询、笔记编辑与个人记录的受限接口。"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import re
import uuid
from zoneinfo import ZoneInfo

from second_proprio import config as configuration
from second_proprio import course
from second_proprio import personal
from second_proprio.documents import DocumentStore


def _load(path: Path, default: object) -> object:
    """读取可选 JSON，不创建缺失文件。"""
    return json.loads(path.read_text("utf-8")) if path.exists() else default


def _today(config: dict) -> dt.date:
    """返回业务时区的今天。"""
    return dt.datetime.now(ZoneInfo(config["timezone"])).date()


def _day(config: dict, value: str | None) -> dt.date:
    """校验传入日期，默认使用配置时区。"""
    return dt.date.fromisoformat(value) if value else _today(config)


def _lessons(root: Path, config: dict) -> list[dict]:
    """构造不产生模板副作用的课程列表。"""
    directory = configuration.resolve_path(root, config["paths"]["courses"])
    private = configuration.resolve_path(root, config["paths"]["private"])
    consumed = _load(
        configuration.resolve_path(root, config["paths"]["state"]) /
        "course-feedback.json", {}).get("note_hashes", {})
    items = []
    for lesson in _load(directory / "syllabus.json",
                        {"lessons": []})["lessons"]:
        identifier = lesson["id"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", identifier):
            continue
        path = configuration.resolve_path(
            root, str(Path(config["paths"]["courses"]) / lesson["path"]))
        note = private / "notes" / f"{identifier}.md"
        note_relative = str(note.relative_to(root))
        template = course._note_template(identifier)  # pylint: disable=protected-access
        content = note.read_text("utf-8") if note.exists() else template
        generated = course._generated(lesson, path)  # pylint: disable=protected-access
        digest = course.feedback_hash(content)
        pending = (
            generated and digest != course.feedback_hash(template) and
            not course.feedback_matches(content, consumed.get(note_relative)))
        item = {
            **lesson, "path":
                str(path.relative_to(root)),
            "kind":
                "course",
            "lesson_id":
                identifier,
            "date":
                lesson.get("generated_on", ""),
            "status":
                "generated" if generated else "planned",
            "learned":
                bool(re.search(r"(?m)^- \[[xX]\] 已学完本节\s*$", content)),
            "feedback_pending":
                pending,
            "note_path":
                note_relative,
            "feedback_path":
                str((private / "feedback" / note.name).relative_to(root))
        }
        if lesson.get("example"):
            item["example_path"] = str(
                Path(config["paths"]["courses"]) / lesson["example"])
        items.append(item)
    return items


def list_content(root: Path, config: dict, kind: str, query: str = "") -> dict:
    """列出知识内容，不调用任何生成准备函数。"""
    items = []
    if kind in {"course", "courses"}:
        items = _lessons(root, config)
    elif kind in {"papers", "paper"}:
        directory = configuration.resolve_path(root, config["paths"]["papers"])
        for metadata in directory.glob("*/**/metadata.json"):
            data = _load(metadata, {})
            review = _load(metadata.parent / "review.json", {})
            path = metadata.parent / "README.md"
            items.append({
                "path":
                    str(path.relative_to(root)),
                "title":
                    data.get("title", metadata.parent.name),
                "kind":
                    "papers",
                "status":
                    review.get("status", "prepared"),
                "date":
                    data.get("published", "")[:10],
                "topic":
                    data.get("topic_id", ""),
                "arxiv_id":
                    data.get("id", ""),
                "version":
                    data.get("version", 1),
                "url":
                    data.get("url", ""),
                "reason":
                    data.get("reason", ""),
                "pdf_path":
                    str((metadata.parent / "paper.pdf").relative_to(root)),
                "note_path":
                    str(
                        Path(config["paths"]["private"]) / "paper-notes" /
                        f"{data.get('id', metadata.parent.name)}.md"),
            })
        items.sort(key=lambda item:
                   (item["date"], item["arxiv_id"], item["version"]),
                   reverse=True)
    elif kind in {"review", "daily"}:
        private = configuration.resolve_path(root, config["paths"]["private"])
        for path in sorted((private / "daily").glob("*.md"), reverse=True):
            context = _load(private / "review-context" / f"{path.stem}.json",
                            {})
            items.append({
                "path":
                    str(path.relative_to(root)),
                "title":
                    f"{path.stem} 每日复盘",
                "kind":
                    "review",
                "date":
                    path.stem,
                "status":
                    "ready" if context.get("messages") else "waiting_source",
                "sources":
                    context.get("sources", []),
                "note_path":
                    str((private / "daily-notes" / path.name).relative_to(root))
            })
    elif kind in {"notes", "feedback"}:
        folder = configuration.resolve_path(root,
                                            config["paths"]["private"]) / kind
        items = [{
            "path": str(path.relative_to(root)),
            "title": path.stem,
            "kind": kind,
            "status": "saved",
            "date": ""
        } for path in sorted(folder.glob("*.md"))]
    else:
        raise ValueError("未知内容类型")
    canonical = {
        "paper": "papers",
        "courses": "course",
        "daily": "review"
    }.get(kind, kind)
    by_path = {item["path"]: item for item in items}
    for candidate in DocumentStore(root, config).candidates()["items"]:
        for entry in candidate["files"]:
            relative = entry["path"]
            path = Path(relative)
            if path.suffix != ".md":
                continue
            matched = (canonical == "papers" and
                       path.is_relative_to(Path(config["paths"]["papers"])) or
                       canonical == "course" and path.is_relative_to(
                           Path(config["paths"]["courses"]) / "lessons") or
                       canonical == "review" and path.is_relative_to(
                           Path(config["paths"]["private"]) / "daily"))
            if not matched:
                continue
            if relative in by_path:
                by_path[relative]["status"] = "waiting_review"
                continue
            title = next((line.lstrip("# ")
                          for line in entry.get("content", "").splitlines()
                          if line.startswith("# ")), path.stem)
            item = {
                "path": relative,
                "title": title,
                "kind": canonical,
                "status": "waiting_review",
                "date": candidate["created_at"][:10],
                "candidate_id": candidate["id"]
            }
            items.append(item)
            by_path[relative] = item
    text = query.casefold().strip()
    if text:
        items = [
            item for item in items
            if text in (item["title"] + " " +
                        item.get("arxiv_id", "")).casefold()
        ]
    return {"items": items}


def read_content(root: Path, config: dict, relative: str) -> dict:
    """返回正文；缺失的课程笔记只在响应中提供模板。"""
    result = DocumentStore(root, config).read(relative)
    if result["hash"] is None:
        lesson = next((item for item in _lessons(root, config)
                       if item["note_path"] == relative), None)
        if lesson:
            result["content"] = course._note_template(  # pylint: disable=protected-access
                lesson["lesson_id"])
    return result


def relationship(root: Path, config: dict, date: str | None = None) -> dict:
    """读取单日关心记录及可用历史日期。"""
    day = _day(config, date)
    directory = configuration.resolve_path(
        root, config["paths"]["private"]) / "relationship"
    value = _load(directory / f"{day}.json", {})
    return {
        "date":
            str(day),
        "name":
            config["relationship"]["name"],
        "days":
            personal.relationship_days(config, day),
        "mood":
            value.get("mood"),
        "note":
            value.get("note", ""),
        "actions":
            value.get("actions", []),
        "history_dates":
            sorted((path.stem for path in directory.glob("*.json")),
                   reverse=True)
    }


def save_relationship(root: Path, config: dict, params: dict) -> dict:
    """完整替换当天手动字段，支持清空分数、备注及删除行动。"""
    day = _day(config, params.get("date"))
    mood = params.get("mood")
    note = params.get("note", "")
    actions = params.get("actions", [])
    if mood is not None and (type(mood) is not int or not 1 <= mood <= 5):  # pylint: disable=unidiomatic-typecheck
        raise ValueError("心情必须为 1–5 整数或未填写")
    if not isinstance(note, str) or not isinstance(actions, list) or not all(
            isinstance(action, str) for action in actions):
        raise ValueError("备注和行动必须为文字")
    path = configuration.resolve_path(
        root, config["paths"]["private"]) / "relationship" / f"{day}.json"
    configuration.atomic_json(path, {
        "mood": mood,
        "note": note,
        "actions": actions
    })
    quiet = {**config, "runtime": {**config["runtime"], "notify": False}}
    personal.care(
        root, quiet,
        dt.datetime.combine(day, dt.time(12), ZoneInfo(config["timezone"])))
    return relationship(root, config, str(day))


def mark_learned(root: Path, config: dict, lesson_id: str,
                 learned: bool) -> dict:
    """修改完成勾选，不排队反馈或宣称用户已掌握内容。"""
    if not isinstance(learned, bool):
        raise ValueError("已学状态必须为布尔值")
    lesson = next((item for item in _lessons(root, config)
                   if item["lesson_id"] == lesson_id), None)
    if not lesson or lesson["status"] != "generated":
        raise ValueError("只能标记已经生成的章节")
    document = read_content(root, config, lesson["note_path"])
    marker = "- [x] 已学完本节" if learned else "- [ ] 已学完本节"
    content = document["content"]
    pattern = r"(?m)^- \[[ xX]\] 已学完本节[^\S\n]*$"
    content = re.sub(pattern, marker, content) if re.search(
        pattern, content) else content + "\n" + marker + "\n"
    DocumentStore(root, config).save(lesson["note_path"], content,
                                     document["hash"])
    return {
        "lesson_id": lesson_id,
        "learned": learned,
        "path": lesson["note_path"]
    }


def add_review_source(root: Path, config: dict, params: dict) -> dict:
    """将用户当天补充作为带时区时间的独立本机证据保存。"""
    day = _day(config, params.get("date"))
    text = params.get("text", "")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请填写当天记录")
    identifier = uuid.uuid4().hex
    stamp = dt.datetime.combine(day, dt.time(12), ZoneInfo(config["timezone"]))
    path = configuration.resolve_path(
        root,
        config["review"]["source_directory"]) / f"manual-{identifier}.json"
    configuration.atomic_json(
        path, {
            "source":
                "App 手工补充",
            "messages": [{
                "timestamp": stamp.isoformat(),
                "role": "user",
                "text": text,
                "conversation_id": f"manual-{identifier}",
                "title": f"{day} 手工记录",
            }],
        })
    return {"imported": str(path.relative_to(root)), "dates": [str(day)]}


def dispatch(root: Path, config: dict, method: str, params: dict) -> dict:  # pylint: disable=too-many-return-statements
    """分派有限的内容方法，任何输入都不会被解释为执行命令。"""
    store = DocumentStore(root, config)
    if method == "content.list":
        return list_content(root, config, params["kind"],
                            params.get("query", ""))
    if method == "content.read":
        return read_content(root, config, params["path"])
    if method == "content.save":
        return store.save(params["path"], params["content"],
                          params.get("base_hash"))
    if method == "content.draft":
        return store.save_draft(params["path"], params["content"],
                                params.get("base_hash"))
    if method == "content.history":
        return store.history(params["path"])
    if method == "content.restore":
        return store.restore(params["path"], params["revision"],
                             params.get("base_hash"))
    if method == "content.candidates":
        return store.candidates(params.get("path"))
    if method == "content.accept":
        return store.accept(params["candidate_id"], params.get("base_hash"),
                            params.get("content"), params.get("path"))
    if method == "content.reject":
        return store.reject(params["candidate_id"])
    if method == "course.learned":
        return mark_learned(root, config, params["lesson_id"],
                            params["learned"])
    if method == "relationship.get":
        return relationship(root, config, params.get("date"))
    if method == "relationship.save":
        return save_relationship(root, config, params)
    if method == "review.add":
        return add_review_source(root, config, params)
    if method == "review.import":
        path = personal.import_chat(root, config, Path(params["path"]))
        data = _load(path, {})
        dates = sorted({
            str(
                dt.datetime.fromisoformat(message["timestamp"]).astimezone(
                    ZoneInfo(config["timezone"])).date())
            for message in data["messages"]
        })
        return {"imported": str(path.relative_to(root)), "dates": dates}
    raise ValueError(f"未知内容方法：{method}")
