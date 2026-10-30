"""验证桌面正文并发编辑、发布恢复和独立个人记录。"""

import json
from pathlib import Path
import shutil

import pytest

from second_proprio import course
from second_proprio import desktop_content as content
from second_proprio import documents


@pytest.fixture(name="project")
def fixture_project(tmp_path: Path) -> tuple[Path, dict]:
    """复制公共课程与配置，绝不读取真实私密数据。"""
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(source / "config", tmp_path / "config")
    shutil.copytree(source / "courses", tmp_path / "courses")
    config = json.loads((tmp_path / "config/settings.example.json").read_text())
    config["runtime"]["notify"] = False
    return tmp_path, config


def test_read_interfaces_do_not_create_notes_or_state(project):
    """浏览课程和缺失笔记不得初始化模板或状态文件。"""
    root, config = project
    before = {str(path.relative_to(root)) for path in root.rglob("*")}
    lessons = content.dispatch(root, config, "content.list", {"kind": "course"})
    item = lessons["items"][0]
    result = content.dispatch(root, config, "content.read",
                              {"path": item["note_path"]})
    assert result["hash"] is None
    assert "我的答案" in result["content"]
    assert {str(path.relative_to(root)) for path in root.rglob("*")} == before


def test_save_conflict_preserves_external_change_and_draft(project):
    """磁盘版本发生变化时，保存不覆盖但保留用户编辑。"""
    root, config = project
    store = documents.DocumentStore(root, config)
    path = "courses/lessons/01-shared-model.md"
    before = store.read(path)
    (root / path).write_text("外部修改", encoding="utf-8")
    with pytest.raises(documents.ConflictError):
        store.save(path, "我的编辑", before["hash"])
    assert (root / path).read_text() == "外部修改"
    assert store.read(path)["draft"]["content"] == "我的编辑"
    saved = store.save(path, "我的编辑", store.read(path)["hash"])
    assert saved["manual_modified"]
    assert saved["draft"] is None
    history = store.history(path)["items"]
    assert len(history) == 2
    baseline = next(item for item in history if item["author"] == "baseline")
    restored = store.restore(path, baseline["id"], saved["hash"])
    assert restored["content"] == "外部修改"
    assert len(store.history(path)["items"]) == 3


def test_document_paths_reject_private_evidence_and_symlink(project, tmp_path):
    """内容接口不能读取凭据或通过软链接离开工作目录。"""
    root, config = project
    store = documents.DocumentStore(root, config)
    for path in ("config/settings.json", "private/chat-imports/chat.md",
                 "../outside.md"):
        with pytest.raises(ValueError):
            store.read(path)
    outside = tmp_path.parent / "outside.md"
    outside.write_text("outside")
    (root / "courses/linked.md").symlink_to(outside)
    with pytest.raises(ValueError):
        store.read("courses/linked.md")


def test_new_generation_publishes_and_manual_changes_produce_candidate(project):
    """新成果可发布，人工修订必须保留并生成候选。"""
    root, config = project
    path = "courses/lessons/new.md"
    stage = documents.create_workspace(root, config, "first")
    draft = Path(stage["workspace"])
    (draft / path).write_text("# AI 初稿", encoding="utf-8")
    result = documents.publish_workspace(root, config, "first", draft,
                                         stage["baseline"], {
                                             "task_id": "job-one",
                                             "settings": {
                                                 "course_day": "one"
                                             }
                                         })
    assert result["status"] == "completed"
    assert result["settings"] == {"course_day": "one"}
    store = documents.DocumentStore(root, config)
    saved = store.save(path, "# 我的改写", store.read(path)["hash"])
    stage = documents.create_workspace(root, config, "second")
    (Path(stage["workspace"]) / path).write_text("# AI 更新", encoding="utf-8")
    result = documents.publish_workspace(root, config, "second",
                                         stage["workspace"], stage["baseline"],
                                         {"task_id": "job-two"})
    assert result["status"] == "waiting_review"
    assert store.read(path)["content"] == "# 我的改写"
    candidate = store.candidates(path)["items"][0]
    assert candidate["files"][0]["content"] == "# AI 更新"
    accepted = store.accept(candidate["id"], saved["hash"], "# 合并后的版本", path)
    assert accepted["status"] == "completed"
    assert store.read(path)["content"] == "# 合并后的版本"
    assert store.read(path)["manual_modified"]


def test_candidate_accept_rechecks_all_file_versions(project):
    """候选产生后，关联目录变化也必须阻止旧候选覆盖。"""
    root, config = project
    path = "courses/lessons/01-shared-model.md"
    stage = documents.create_workspace(root, config, "candidate")
    draft = Path(stage["workspace"])
    (draft / path).write_text("candidate")
    (draft / "courses/syllabus.json").write_text('{"lessons":[]}')
    result = documents.publish_workspace(root, config, "candidate", draft,
                                         stage["baseline"], {})
    store = documents.DocumentStore(root, config)
    (root /
     "courses/syllabus.json").write_text('{"lessons":[],"external":true}')
    with pytest.raises(documents.ConflictError):
        store.accept(result["candidate_id"],
                     store.read(path)["hash"],
                     path=path)
    assert store.read(path)["content"] != "candidate"


def test_publication_recovers_after_partial_write(project, monkeypatch):
    """中断后的发布事务能够继续，状态文件在正文后提交。"""
    root, config = project
    stage = documents.create_workspace(root, config, "recovery")
    draft = Path(stage["workspace"])
    (draft / "courses/a.md").write_text("A")
    (draft / "courses/b.md").write_text("B")
    original = documents._atomic_bytes  # pylint: disable=protected-access
    calls = []

    def crash_after_one(path, data):
        calls.append(str(path))
        if len(calls) == 2:
            raise OSError("模拟发布中断")
        original(path, data)

    monkeypatch.setattr(documents, "_atomic_bytes", crash_after_one)
    with pytest.raises(OSError):
        documents.publish_workspace(root, config, "recovery", draft,
                                    stage["baseline"], {})
    monkeypatch.setattr(documents, "_atomic_bytes", original)
    recovered = documents.DocumentStore(root, config).recover()
    assert recovered[0]["status"] == "completed"
    assert (root / "courses/a.md").read_text() == "A"
    assert (root / "courses/b.md").read_text() == "B"
    assert not documents.DocumentStore(root, config).recover()


def test_rejected_fingerprint_not_republished(project):
    """被放弃的相同输入不会自动重新发布。"""
    root, config = project
    stage = documents.create_workspace(root, config, "reject")
    path = "courses/lessons/01-shared-model.md"
    (Path(stage["workspace"]) / path).write_text("candidate")
    result = documents.publish_workspace(root, config, "reject",
                                         stage["workspace"], stage["baseline"],
                                         {"input_fingerprint": "same"})
    store = documents.DocumentStore(root, config)
    store.reject(result["candidate_id"])
    again = documents.publish_workspace(root, config, "reject",
                                        stage["workspace"], stage["baseline"],
                                        {"input_fingerprint": "same"})
    assert again["candidate_id"] == result["candidate_id"]
    assert store.candidates()["items"] == []
    explicit = documents.publish_workspace(root, config, "reject",
                                           stage["workspace"],
                                           stage["baseline"], {
                                               "input_fingerprint": "same",
                                               "regenerate": True
                                           })
    assert explicit["candidate_id"] != result["candidate_id"]
    assert explicit["status"] == "waiting_review"


def test_course_completion_alone_does_not_trigger_feedback(project):
    """完成勾选与笔记语义分离，手动反馈不会安排新章节。"""
    root, config = project
    item = content.list_content(root, config, "course")["items"][0]
    content.mark_learned(root, config, item["lesson_id"], True)
    refreshed = content.list_content(root, config, "course")["items"][0]
    assert refreshed["learned"]
    assert not refreshed["feedback_pending"]
    store = documents.DocumentStore(root, config)
    note = store.read(item["note_path"])
    store.save(item["note_path"], note["content"] + "\n我的答案：策略输出动作。",
               note["hash"])
    prepared = course.prepare(root,
                              config,
                              feedback_only=True,
                              lesson_id=item["lesson_id"])
    assert prepared["next_lesson"] is None
    assert list(prepared["note_hashes"]) == [item["note_path"]]
    assert "不要生成新课程" in prepared["prompt"]
    digest = prepared["note_hashes"][item["note_path"]]
    content.mark_learned(root, config, item["lesson_id"], False)
    assert course.feedback_hash(
        (root / item["note_path"]).read_text()) == digest


def test_relationship_clear_and_manual_review_source(project):
    """关心表单支持清空和删除，手工复盘记录保留来源日期。"""
    root, config = project
    result = content.save_relationship(root, config, {
        "date": "2026-10-29",
        "mood": 5,
        "note": "一起吃饭",
        "actions": ["聊天"]
    })
    assert result["days"] == 973
    result = content.save_relationship(root, config, {
        "date": "2026-10-29",
        "mood": None,
        "note": "",
        "actions": []
    })
    assert result[
        "mood"] is None and not result["note"] and not result["actions"]
    assert content.relationship(root, config, "2026-10-30")["days"] == 974
    added = content.add_review_source(root, config, {
        "date": "2026-10-29",
        "text": "完成课程笔记"
    })
    evidence = json.loads((root / added["imported"]).read_text())
    assert added["dates"] == ["2026-10-29"]
    assert evidence["messages"][0]["timestamp"].startswith("2026-10-29")


def test_code_mirror_refreshes_from_source_and_rejects_divergence(project):
    """外部源码修改显示在正文预览，Markdown 不能另存不同算法。"""
    root, config = project
    path = "courses/lessons/01-shared-model.md"
    source = root / "courses/examples/shared_model.py"
    original_disk = (root / path).read_text()
    source.write_text(source.read_text() + "\n# 新增源码注释\n")
    store = documents.DocumentStore(root, config)
    read = store.read(path)
    assert "# 新增源码注释" in read["content"]
    assert (root / path).read_text() == original_disk
    with pytest.raises(ValueError, match="关联的可运行代码"):
        store.save(path, read["content"].replace("# 新增源码注释", "# 正文独立修改"),
                   read["hash"])


def test_published_crash_recovery_only_replays_active_run(project):
    """发布完成而队列未确认时，仅恢复指定运行，避免旧状态覆盖重试。"""
    root, config = project
    stage = documents.create_workspace(root, config, "published")
    (Path(stage["workspace"]) / "courses/fresh.md").write_text("new")
    documents.publish_workspace(root, config, "published", stage["workspace"],
                                stage["baseline"], {
                                    "run_id": "active-run",
                                    "task_id": "task"
                                })
    assert not documents.recover_publications(root, config, "another-run")
    recovered = documents.recover_publications(root, config, "active-run")
    assert recovered[0]["task_id"] == "task"


def test_legacy_completed_note_hash_ignores_checkbox_only():
    """旧版完整文件摘要也能识别只有完成勾选变化的同一笔记。"""
    import hashlib  # pylint: disable=import-outside-toplevel
    checked = "# 笔记\n- [x] 已学完本节\n答案：策略决定动作。"
    digest = hashlib.sha256(checked.encode()).hexdigest()
    unchecked = checked.replace("[x]", "[ ]")
    assert course.feedback_matches(unchecked, digest)
    assert not course.feedback_matches(unchecked + "新的疑问", digest)


def test_unpublished_candidate_visible_and_recovered_as_waiting(project):
    """候选登记后中断不会丢失入口或重新生成同一份内容。"""
    root, config = project
    stage = documents.create_workspace(root, config, "waiting")
    draft = Path(stage["workspace"])
    path = "courses/lessons/new-candidate.md"
    (draft / path).write_text("# 等待采纳的新章节")
    (draft / "courses/README.md").write_text("新的课程索引")
    result = documents.publish_workspace(root, config, "waiting", draft,
                                         stage["baseline"], {
                                             "task_id": "task",
                                             "run_id": "wait-run"
                                         })
    assert result["status"] == "waiting_review"
    item = next(
        item for item in content.list_content(root, config, "course")["items"]
        if item["path"] == path)
    assert item["status"] == "waiting_review"
    read = content.read_content(root, config, path)
    assert read["hash"] is None and read["candidates"]
    recovered = documents.recover_publications(root, config, "wait-run")
    assert recovered[0]["status"] == "waiting_review"
    assert recovered[0]["task_id"] == "task"


def test_workspace_has_no_write_through_symlinks(project, tmp_path):
    """生成目录复制普通文件，不能通过符号链接写回真实资料。"""
    root, config = project
    outside = tmp_path.parent / "outside-notes"
    outside.mkdir(exist_ok=True)
    (outside / "secret.md").write_text("private")
    (root / "courses/linked").symlink_to(outside, target_is_directory=True)
    (root / "courses/linked.md").symlink_to(outside / "secret.md")
    stage = documents.create_workspace(root, config, "isolated")
    workspace = Path(stage["workspace"])
    assert not (workspace / "courses/linked").exists()
    assert not (workspace / "courses/linked.md").exists()
    assert not any(path.is_symlink() for path in workspace.rglob("*"))
    assert not (workspace / ".state/queue.sqlite3").exists()


def test_workspace_private_inputs_are_scoped_by_task(project):
    """论文不接触私密记录，课程只接触学习笔记，复盘仅带当天证据。"""
    root, config = project
    for relative in ("private/notes/01-shared-model.md",
                     "private/feedback/01-shared-model.md",
                     "private/relationship/2026-10-29.json",
                     "private/daily/2026-10-29.md"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private")
    source = root / "private/chat-imports/export.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps({
            "messages": [{
                "timestamp": "2026-10-29T12:00:00+08:00",
                "role": "user",
                "text": "today"
            }, {
                "timestamp": "2026-10-28T12:00:00+08:00",
                "role": "user",
                "text": "other"
            }]
        }))
    paper = Path(
        documents.create_workspace(root, config, "papers-only",
                                   {"kind": "papers"})["workspace"])
    assert not (paper / "private").exists()
    lesson = Path(
        documents.create_workspace(root, config, "lesson-only",
                                   {"kind": "course"})["workspace"])
    assert (lesson / "private/notes/01-shared-model.md").exists()
    assert not (lesson / "private/relationship").exists()
    assert not (lesson / "private/chat-imports").exists()
    review = Path(
        documents.create_workspace(root, config, "review-only", {
            "kind": "review",
            "scheduled_at": "2026-10-29T22:30:00+08:00"
        })["workspace"])
    assert not (review / "private/notes").exists()
    messages = json.loads(
        (review / "private/chat-imports/export.json").read_text())["messages"]
    assert [item["text"] for item in messages] == ["today"]
    assert (review / "private/daily/2026-10-29.md").exists()


def test_existing_notes_never_published_even_after_external_deletion(project):
    """用户笔记不能由模型改写；用户删除后也不能被旧草稿复活。"""
    root, config = project
    path = "private/notes/01-shared-model.md"
    (root / path).parent.mkdir(parents=True)
    (root / path).write_text("用户原文")
    stage = documents.create_workspace(root, config, "preserve-notes",
                                       {"kind": "course"})
    (Path(stage["workspace"]) / path).write_text("模型改写")
    (root / path).unlink()
    documents.publish_workspace(root, config, "preserve-notes",
                                stage["workspace"], stage["baseline"], {})
    assert not (root / path).exists()


def test_feedback_only_cannot_publish_public_lesson(project):
    """只反馈模式即使意外产生课程正文也不能发布公开内容。"""
    root, config = project
    stage = documents.create_workspace(
        root, config, "feedback-scope", {
            "kind": "course",
            "payload": {
                "mode": "feedback",
                "lesson_id": "01-shared-model"
            }
        })
    path = Path(stage["workspace"]) / "courses/unexpected.md"
    path.write_text("不应发布")
    documents.publish_workspace(root, config, "feedback-scope",
                                stage["workspace"], stage["baseline"], {})
    assert not (root / "courses/unexpected.md").exists()


def test_external_code_edit_cannot_be_overwritten_by_generation(project):
    """外部编辑的可运行源码也应受到候选发布保护。"""
    root, config = project
    source = "courses/examples/new_example.py"
    stage = documents.create_workspace(root, config, "code-first")
    (Path(stage["workspace"]) / source).write_text("print('first')\n")
    published = documents.publish_workspace(root, config, "code-first",
                                            stage["workspace"],
                                            stage["baseline"], {})
    assert published["status"] == "completed"
    (root / source).write_text("print('user edit')\n")
    stage = documents.create_workspace(root, config, "code-second")
    (Path(stage["workspace"]) / source).write_text("print('AI update')\n")
    candidate = documents.publish_workspace(root, config, "code-second",
                                            stage["workspace"],
                                            stage["baseline"], {})
    assert candidate["status"] == "waiting_review"
    assert (root / source).read_text() == "print('user edit')\n"
