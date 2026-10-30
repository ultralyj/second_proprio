"""保存文档修订、编辑草稿与可恢复的多文件发布事务。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from second_proprio import config as configuration


class ConflictError(ValueError):
    """携带当前版本和用户草稿的编辑冲突。"""

    code = "conflict"

    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


def _now() -> str:
    """返回可排序的 UTC 时间。"""
    return dt.datetime.now(dt.timezone.utc).isoformat()


def file_hash(path: Path) -> str | None:
    """计算存在文件的摘要，缺失文件返回空值。"""
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path, default: Any) -> Any:
    """读取可选 JSON 文件。"""
    return json.loads(path.read_text("utf-8")) if path.exists() else default


def _key(path: str) -> str:
    """将相对路径变成不可越界的内部索引键。"""
    return hashlib.sha256(path.encode()).hexdigest()


def _atomic_bytes(path: Path, data: bytes) -> None:
    """通过同目录替换写入完整文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class DocumentStore:
    """在工作目录内保护文档正文与关联的发布文件。"""

    def __init__(self, root: Path, config: dict):
        self.root = root.resolve()
        self.config = config
        self.state = configuration.resolve_path(root, config["paths"]["state"])
        self.directory = self.state / "documents"

    def document_path(self, relative: str) -> Path:
        """仅接受内容目录下可编辑的 Markdown，拒绝凭据与链接越界。"""
        path = configuration.resolve_path(self.root, relative)
        allowed = [self.config["paths"][key] for key in ("papers", "courses")]
        private = self.config["paths"]["private"]
        allowed.extend(f"{private}/{name}" for name in ("notes", "feedback",
                                                        "daily", "paper-notes",
                                                        "daily-notes"))
        if path.suffix != ".md" or not any(
                path.is_relative_to(configuration.resolve_path(self.root, name))
                for name in allowed):
            raise ValueError("只允许读取或编辑知识正文和私密笔记")
        return path

    def _record(self, relative: str) -> dict:
        """读取单个文档的发布基线。"""
        return _json(self.directory / "index" / f"{_key(relative)}.json", {})

    def read(self, relative: str) -> dict:
        """读取正文和编辑状态，不创建文件或笔记模板。"""
        path = self.document_path(relative)
        record = self._record(relative)
        digest = file_hash(path)
        text = path.read_text("utf-8") if path.exists() else ""
        text = self._render_source(relative, text)
        draft = _json(self.directory / "drafts" / f"{_key(relative)}.json",
                      None)
        return {
            "path":
                relative,
            "content":
                text,
            "hash":
                digest,
            "manual_modified":
                bool(
                    record.get("manual") or
                    (record and digest != record.get("hash"))),
            "editable":
                not path.is_relative_to(
                    configuration.resolve_path(
                        self.root, self.config["paths"]["private"]) /
                    "feedback"),
            "draft":
                draft,
            "candidates":
                self.candidates(relative)["items"],
        }

    def save_draft(self, relative: str, content: str,
                   base_hash: str | None) -> dict:
        """保存未正式提交的文字，冲突时也保留用户输入。"""
        self.document_path(relative)
        if not isinstance(content, str) or len(
                content.encode()) > 4 * 1024 * 1024:
            raise ValueError("正文必须为不超过 4 MiB 的文本")
        configuration.atomic_json(
            self.directory / "drafts" / f"{_key(relative)}.json", {
                "path": relative,
                "content": content,
                "base_hash": base_hash,
                "updated_at": _now(),
            })
        return self.read(relative)

    def _revision(self, relative: str, data: bytes, author: str) -> str:
        """记录不可变正文快照。"""
        identifier = uuid.uuid4().hex
        configuration.atomic_json(
            self.directory / "history" / _key(relative) / f"{identifier}.json",
            {
                "id": identifier,
                "path": relative,
                "created_at": _now(),
                "author": author,
                "hash": hashlib.sha256(data).hexdigest(),
                "content": data.decode("utf-8"),
            })
        return identifier

    def save(self, relative: str, content: str, base_hash: str | None) -> dict:
        """乐观校验后保存人工修订，冲突时只保存草稿。"""
        path = self.document_path(relative)
        self.save_draft(relative, content, base_hash)
        current = self.read(relative)
        if not current["editable"]:
            raise ValueError("生成反馈为只读，请在独立笔记中回应")
        if current["hash"] != base_hash:
            raise ConflictError("文件已发生变化，已保留编辑草稿", current)
        if path.exists() and not self.history(relative)["items"]:
            self._revision(relative, path.read_bytes(), "baseline")
        self._check_managed_code(relative, current["content"], content)
        data = content.encode("utf-8")
        revision = self._revision(relative, data, "user")
        _atomic_bytes(path, data)
        configuration.atomic_json(
            self.directory / "index" / f"{_key(relative)}.json", {
                "hash": file_hash(path),
                "manual": True,
                "revision": revision,
            })
        (self.directory / "drafts" /
         f"{_key(relative)}.json").unlink(missing_ok=True)
        return self.read(relative)

    def _example_source(self, relative: str) -> str | None:
        """寻找课程正文关联的唯一可运行源码。"""
        directory = configuration.resolve_path(self.root,
                                               self.config["paths"]["courses"])
        for lesson in _json(directory / "syllabus.json",
                            {"lessons": []})["lessons"]:
            if str(Path(self.config["paths"]["courses"]) /
                   lesson["path"]) != relative:
                continue
            if lesson.get("example"):
                source = configuration.resolve_path(
                    self.root,
                    str(
                        Path(self.config["paths"]["courses"]) /
                        lesson["example"]))
                return source.read_text(
                    "utf-8").strip() if source.exists() else None
        return None

    def _render_source(self, relative: str, content: str) -> str:
        """在读取结果中同步关联代码，不改变磁盘正文。"""
        code = self._example_source(relative)
        if code is None:
            return content
        pattern = r"(?s)(## 最简代码.*?```(?:python|py)\n).*?(\n```)"
        return re.sub(pattern,
                      lambda match: match[1] + code + match[2],
                      content,
                      count=1)

    def _check_managed_code(self, relative: str, before: str,
                            after: str) -> None:
        """禁止在 Markdown 中独立改写受管的可运行代码。"""
        code = self._example_source(relative)
        if code and code in before and code not in after:
            raise ValueError("关联的可运行代码请通过示例源码入口修改")
        if self._render_source(relative, after) != after:
            raise ValueError("示例源码已经变化，请刷新正文后重试")

    def history(self, relative: str) -> dict:
        """读取按时间倒序排列的正文历史。"""
        self.document_path(relative)
        folder = self.directory / "history" / _key(relative)
        items = [_json(path, {}) for path in folder.glob("*.json")]
        return {
            "items":
                sorted(items, key=lambda item: item["created_at"], reverse=True)
        }

    def restore(self, relative: str, revision: str,
                base_hash: str | None) -> dict:
        """恢复历史内容，并形成新的人工修订。"""
        found = next((item for item in self.history(relative)["items"]
                      if item["id"] == revision), None)
        if found is None:
            raise ValueError("没有找到这次修订")
        return self.save(relative,
                         self._render_source(relative, found["content"]),
                         base_hash)

    def candidates(self, relative: str | None = None) -> dict:
        """列出待采纳候选正文；二进制文件仅提供路径与摘要。"""
        if relative is not None:
            self.document_path(relative)
        items = []
        for path in (self.directory / "publications").glob("*/manifest.json"):
            item = _json(path, {})
            if item.get("status") != "waiting_review":
                continue
            if relative and not any(entry["path"] == relative
                                    for entry in item["files"]):
                continue
            public = {
                key: item.get(key)
                for key in ("id", "status", "created_at", "summary", "task_id")
            }
            public["files"] = []
            for entry in item["files"]:
                value = {
                    key: entry.get(key)
                    for key in ("path", "base_hash", "new_hash")
                }
                if Path(entry["path"]).suffix in {".md", ".py", ".json"}:
                    value["content"] = (path.parent /
                                        entry["blob"]).read_text("utf-8")
                public["files"].append(value)
            items.append(public)
        return {
            "items":
                sorted(items, key=lambda item: item["created_at"], reverse=True)
        }

    def _manifest(self, identifier: str) -> tuple[Path, dict]:
        """按受限标识读取发布清单。"""
        if not identifier.isalnum():
            raise ValueError("不合法的候选标识")
        path = self.directory / "publications" / identifier / "manifest.json"
        if not path.exists():
            raise ValueError("候选不存在")
        return path, _json(path, {})

    def accept(self,
               identifier: str,
               base_hash: str | None,
               content: str | None = None,
               path: str | None = None) -> dict:
        """对比当前基准后整体采纳候选，保留用户编辑后的候选文本。"""
        manifest_path, manifest = self._manifest(identifier)
        if manifest["status"] != "waiting_review":
            raise ValueError("候选已经处理")
        primary = next(
            (entry for entry in manifest["files"] if entry["path"] == path),
            None) if path else None
        primary = primary or next(
            (entry for entry in manifest["files"]
             if entry["path"].endswith(".md")), manifest["files"][0])
        current = file_hash(
            configuration.resolve_path(self.root, primary["path"]))
        if current != base_hash:
            raise ConflictError("采纳前正文再次变化，请重新比较", {
                "path": primary["path"],
                "hash": current,
            })
        for entry in manifest["files"]:
            target = configuration.resolve_path(self.root, entry["path"])
            if file_hash(target) != entry["base_hash"]:
                raise ConflictError("关联文件发生变化，请重新生成候选", {
                    "path": entry["path"],
                    "hash": file_hash(target),
                })
        if content is not None:
            if not primary["path"].endswith(".md"):
                raise ValueError("只能编辑候选 Markdown 正文")
            data = content.encode("utf-8")
            _atomic_bytes(manifest_path.parent / primary["blob"], data)
            primary["new_hash"] = hashlib.sha256(data).hexdigest()
            manifest["author"] = "user"
        manifest["status"] = "prepared"
        configuration.atomic_json(manifest_path, manifest)
        return self._commit(manifest_path, manifest)

    def reject(self, identifier: str) -> dict:
        """放弃候选但保留草稿和输入指纹，避免自动重复生成。"""
        path, manifest = self._manifest(identifier)
        if manifest["status"] != "waiting_review":
            raise ValueError("候选已经处理")
        manifest["status"] = "rejected"
        configuration.atomic_json(path, manifest)
        return {"status": "rejected", "task_id": manifest.get("task_id")}

    def _commit(self, manifest_path: Path, manifest: dict) -> dict:
        """以持久清单恢复多文件提交，遇到外部冲突停止而不覆盖。"""
        manifest["status"] = "committing"
        configuration.atomic_json(manifest_path, manifest)
        for entry in manifest["files"]:
            target = configuration.resolve_path(self.root, entry["path"])
            current = file_hash(target)
            if current not in {entry["base_hash"], entry["new_hash"]}:
                manifest["status"] = "blocked"
                configuration.atomic_json(manifest_path, manifest)
                raise ConflictError("发布期间文件发生变化，已保留事务", entry)
            if current != entry["new_hash"]:
                if target.suffix == ".md" and target.exists():
                    self._revision(entry["path"], target.read_bytes(),
                                   "baseline")
                data = (manifest_path.parent / entry["blob"]).read_bytes()
                _atomic_bytes(target, data)
            if target.suffix in {".md", ".py"}:
                record = self._record(entry["path"])
                if record.get("hash") != entry["new_hash"]:
                    revision = self._revision(entry["path"],
                                              target.read_bytes(),
                                              manifest.get("author", "ai"))
                    configuration.atomic_json(
                        self.directory / "index" /
                        f"{_key(entry['path'])}.json", {
                            "hash":
                                entry["new_hash"],
                            "manual":
                                bool(
                                    record.get("manual") or
                                    manifest.get("author") == "user" or
                                    (entry["base_hash"] is not None and
                                     record.get("hash") != entry["base_hash"])),
                            "revision":
                                revision,
                        })
        manifest["status"] = "published"
        configuration.atomic_json(manifest_path, manifest)
        return {
            "status": "completed",
            "publication_id": manifest["id"],
            "task_id": manifest.get("task_id"),
            "artifacts": [
                entry["path"]
                for entry in manifest["files"]
                if entry["path"].endswith(".md")
            ],
            "settings": manifest.get("settings", {}),
            "settings_base": manifest.get("settings_base", {})
        }

    def recover(self) -> list[dict]:
        """恢复已经决定发布但尚未完成的事务。"""
        recovered = []
        for path in (self.directory / "publications").glob("*/manifest.json"):
            manifest = _json(path, {})
            if manifest.get("status") in {"prepared", "committing"}:
                recovered.append(self._commit(path, manifest))
        return recovered


def create_workspace(root: Path,
                     config: dict,
                     run_id: str,
                     task: dict | None = None) -> dict:
    """复制生成所需资源到独立目录，不共享可写链接或实时数据库。"""
    if not run_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("不合法的任务标识")
    state = configuration.resolve_path(root, config["paths"]["state"])
    workspace = state / "draft-runs" / run_id / "workspace"
    if workspace.exists():
        raise ValueError("该草稿工作目录已存在")
    workspace.mkdir(parents=True, mode=0o700)
    baseline = {}
    task = task or {"kind": "course"}
    kind = task["kind"]
    payload = task.get("payload", {})
    folders = [".agents", "config"]
    files = [root / "AGENTS.md"]
    private = Path(config["paths"]["private"])
    source_folder = configuration.resolve_path(
        root, config["review"]["source_directory"])
    review_day = None
    if kind == "papers":
        folders.append(config["paths"]["papers"])
        files.append(state / "paper-candidates.json")
    elif kind == "course":
        folders.append(config["paths"]["courses"])
        if payload.get("mode") == "feedback" and payload.get("lesson_id"):
            identifier = payload["lesson_id"]
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", identifier):
                raise ValueError("不合法的章节标识")
            files.extend(root / private / name / f"{identifier}.md"
                         for name in ("notes", "feedback"))
        else:
            folders.extend(
                str(private / name) for name in ("notes", "feedback"))
        files.append(state / "course-feedback.json")
    elif kind == "review":
        stamp = task.get("scheduled_at")
        review_day = (dt.date.fromisoformat(payload["date"])
                      if payload.get("date") else
                      dt.datetime.fromisoformat(stamp).astimezone(
                          ZoneInfo(config["timezone"])).date() if stamp else
                      dt.datetime.now(ZoneInfo(config["timezone"])).date())
        folders.append(config["review"]["source_directory"])
        files.append(root / private / "daily" / f"{review_day}.md")
    else:
        raise ValueError("该任务不需要生成工作区")
    for folder in folders:
        directory = configuration.resolve_path(root, folder)
        if directory.exists() and not directory.is_symlink():
            files.extend(
                path for path in directory.rglob("*") if path.is_file())
    for source in files:
        if source.is_symlink() or not source.exists() or any(
                parent.is_symlink()
                for parent in source.parents
                if parent.is_relative_to(root)):
            continue
        relative = str(source.relative_to(root))
        target = configuration.resolve_path(workspace, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind != "papers" and source.suffix.lower() == ".pdf":
            continue
        if review_day and source.is_relative_to(source_folder):
            if source.suffix != ".json":
                continue
            data = _json(source, {})
            selected = []
            for message in data.get("messages", []):
                try:
                    stamp = dt.datetime.fromisoformat(message["timestamp"])
                    if stamp.astimezone(ZoneInfo(
                            config["timezone"])).date() == review_day:
                        selected.append(message)
                except (ValueError, TypeError, KeyError):
                    continue
            if not selected:
                continue
            configuration.atomic_json(target, {**data, "messages": selected})
        else:
            shutil.copy2(source, target)
        baseline[relative] = file_hash(target)
    configuration.atomic_json(workspace / "config/settings.json", config)
    configuration.atomic_json(workspace.parent / "baseline.json", baseline)
    configuration.atomic_json(
        workspace.parent / "scope.json", {
            "kind": kind,
            "mode": payload.get("mode"),
            "lesson_id": payload.get("lesson_id"),
            "review_day": str(review_day) if review_day else None,
        })
    return {"workspace": str(workspace), "baseline": baseline}


def _publishable(relative: str, config: dict) -> bool:
    """仅允许模型发布任务产物，拒绝配置、程序与凭据变更。"""
    path = Path(relative)
    for name in ("courses", "papers"):
        if path.is_relative_to(Path(config["paths"][name])):
            return path.suffix.lower() in {
                ".md", ".py", ".json", ".pdf", ".png", ".svg", ".jpg", ".jpeg",
                ".webp", ".gif", ".tex", ".txt", ".html", ".xml"
            }
    private = Path(config["paths"]["private"])
    if path.is_relative_to(
            private / "review-context") and path.suffix == ".json":
        return True
    if any(
            path.is_relative_to(private / name)
            for name in ("feedback", "daily", "notes")):
        return path.suffix == ".md"
    state = Path(config["paths"]["state"])
    return path in {
        state / name
        for name in ("course-feedback.json", "paper-candidates.json",
                     "paper-collection-report.json",
                     "paper-preparation-report.json")
    }


def _within_scope(relative: str, config: dict, scope: dict) -> bool:
    """按任务模式收窄发布范围，不允许论文任务修改个人资料。"""
    path = Path(relative)
    kind = scope.get("kind")
    private = Path(config["paths"]["private"])
    state = Path(config["paths"]["state"])
    if kind == "papers":
        return path.is_relative_to(Path(config["paths"]["papers"])) or path in {
            state / name for name in ("paper-candidates.json",
                                      "paper-collection-report.json",
                                      "paper-preparation-report.json")
        }
    if kind == "review":
        day = scope.get("review_day")
        return path in {
            private / "daily" / f"{day}.md",
            private / "review-context" / f"{day}.json"
        }
    if kind == "course":
        if scope.get("mode") == "feedback":
            return path in {
                private / "feedback" / f"{scope.get('lesson_id')}.md",
                state / "course-feedback.json"
            }
        return (path.is_relative_to(Path(config["paths"]["courses"])) or any(
            path.is_relative_to(private / name)
            for name in ("feedback", "notes")) or
                path == state / "course-feedback.json")
    return False


def publish_workspace(root: Path, config: dict, run_id: str,
                      workspace: Path | str, baseline: dict,
                      result: dict) -> dict:
    """将独立生成结果变为已发布成果或待人工采纳的完整候选组。"""
    workspace = Path(workspace)
    store = DocumentStore(root, config)
    fingerprint = result.get("input_fingerprint")
    if fingerprint and not result.get("regenerate", False):
        for old in (store.directory / "publications").glob("*/manifest.json"):
            item = _json(old, {})
            if item.get("input_fingerprint") == fingerprint and item.get(
                    "status") in {"waiting_review", "rejected"}:
                return {
                    "status": "waiting_review",
                    "candidate_id": item["id"],
                    "artifacts": [],
                    "summary": "相同输入已有待处理或放弃的候选"
                }
    identifier = uuid.uuid4().hex
    folder = store.directory / "publications" / identifier
    entries = []
    needs_review = False
    scope = _json(workspace.parent / "scope.json", {"kind": "course"})
    for source in sorted(workspace.rglob("*")):
        if not source.is_file() or source.is_symlink():
            continue
        relative = str(source.relative_to(workspace))
        if not _publishable(relative, config) or not _within_scope(
                relative, config, scope):
            continue
        new_hash = file_hash(source)
        if baseline.get(relative) == new_hash:
            continue
        target = configuration.resolve_path(root, relative)
        current = file_hash(target)
        if target.is_relative_to(
                configuration.resolve_path(root, config["paths"]["private"]) /
                "notes") and (target.exists() or
                              baseline.get(relative) is not None):
            continue
        record = store._record(relative)  # pylint: disable=protected-access
        if current != baseline.get(relative):
            needs_review = True
        if target.suffix in {
                ".md", ".py"
        } and current and (not record or record.get("manual") or
                           current != record.get("hash")):
            needs_review = True
        blob = f"files/{len(entries)}"
        (folder / "files").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, folder / blob)
        entries.append({
            "path": relative,
            "base_hash": current,
            "generation_base_hash": baseline.get(relative),
            "new_hash": new_hash,
            "blob": blob
        })
    # 先发布正文，最后发布目录与反馈确认元数据。
    entries.sort(
        key=lambda item: (Path(item["path"]).suffix == ".json", item["path"]))
    manifest = {
        "id": identifier,
        "run_id": result.get("run_id", run_id),
        "task_id": result.get("task_id", run_id),
        "created_at": _now(),
        "summary": result.get("summary", "生成内容"),
        "files": entries,
        "settings": result.get("settings", {}),
        "settings_base": result.get("settings_base", {}),
        "author": "ai",
        "input_fingerprint": fingerprint,
        "status": "waiting_review" if needs_review else "prepared"
    }
    path = folder / "manifest.json"
    configuration.atomic_json(path, manifest)
    if needs_review:
        return {
            "status": "waiting_review",
            "candidate_id": identifier,
            "publication_id": identifier,
            "artifacts": [],
            "summary": "保留当前正文，生成内容等待采纳"
        }
    return store._commit(path, manifest)  # pylint: disable=protected-access


def recover_publications(root: Path,
                         config: dict,
                         run_id: str | None = None) -> list[dict]:
    """恢复发布事务，并仅补交指定活动运行已经发布的结果。"""
    store = DocumentStore(root, config)
    recovered = store.recover()
    known = {item["publication_id"] for item in recovered}
    if run_id is not None:
        for path in (store.directory / "publications").glob("*/manifest.json"):
            manifest = _json(path, {})
            if manifest.get("run_id") != run_id or manifest["id"] in known:
                continue
            if manifest.get("status") == "waiting_review":
                recovered.append({
                    "status": "waiting_review",
                    "candidate_id": manifest["id"],
                    "publication_id": manifest["id"],
                    "task_id": manifest.get("task_id"),
                    "artifacts": [],
                    "settings": {},
                    "summary": "生成内容等待采纳",
                })
            elif manifest.get("status") == "published":
                recovered.append({
                    "status": "completed",
                    "publication_id": manifest["id"],
                    "task_id": manifest.get("task_id"),
                    "artifacts": [
                        entry["path"]
                        for entry in manifest["files"]
                        if entry["path"].endswith(".md")
                    ],
                    "settings": manifest.get("settings", {}),
                    "settings_base": manifest.get("settings_base", {}),
                })
    return recovered
