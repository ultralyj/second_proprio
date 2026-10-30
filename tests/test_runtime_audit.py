"""独立审查任务恢复、成果确认和反馈版本之间的实际行为。"""

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
from zoneinfo import ZoneInfo

import jsonschema
import pytest

from second_proprio import course
from second_proprio import cli
from second_proprio import papers
from second_proprio import runner
from second_proprio import sync
from second_proprio.store import Store


@pytest.fixture(name="config")
def fixture_config():
    """使用项目正式模板并关闭测试通知。"""
    project = Path(__file__).resolve().parents[1]
    value = json.loads((project / "config/settings.example.json").read_text())
    value["runtime"]["notify"] = False
    return value


@pytest.fixture(name="now")
def fixture_now():
    """固定同一北京时间，避免测试依赖执行宿主时区。"""
    return dt.datetime(2026, 10, 29, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _queue(root, kind, now):
    """建立一个真实 SQLite 工作项。"""
    store = Store(root / ".state/queue.sqlite3")
    identifier = store.add(kind, now.isoformat(), "test-config")
    return store, identifier


def _syllabus(root, lessons):
    """写入有意简化的课程目录，验收只关注生成协议。"""
    folder = root / "courses"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "syllabus.json").write_text(json.dumps({"lessons": lessons}),
                                          encoding="utf-8")


def test_partial_paper_readme_is_retried(tmp_path, config, monkeypatch):
    """失败进程留下的 README 不能使论文永久跳过。"""
    record = {
        "id": "2601.00001",
        "version": 1,
        "topic_id": "dexterous-manipulation",
        "title": "Dexterous example",
        "abstract": "A test fixture, not a real paper.",
    }
    state = tmp_path / ".state"
    state.mkdir()
    (state / "paper-candidates.json").write_text(json.dumps([record]))
    folder = tmp_path / "papers/dexterous-manipulation/2601.00001/versions/v1"
    folder.mkdir(parents=True)
    (folder / "README.md").write_text("# 尚未写完的中断草稿\n")
    monkeypatch.setattr(
        papers,
        "_prepare_html",
        lambda *_args: {
            "source": "html",
            "body": "fixture"
        },
    )
    result = papers.prepare(tmp_path, config, limit=1)
    assert len(result) == 1, "未确认完成的中断论文必须继续进入解读"


def test_network_failure_is_not_completed_empty_scan(tmp_path, config, now,
                                                     monkeypatch):
    """检索全面网络失败不能伪装成成功但没有论文。"""

    def fail_fetch(*_args, **_kwargs):
        raise OSError("模拟网络不可用")

    monkeypatch.setattr(papers, "_fetch", fail_fetch)
    store, identifier = _queue(tmp_path, "papers", now)
    try:
        runner.consume(tmp_path, config, store, now)
        task = next(
            item for item in store.export_tasks() if item["id"] == identifier)
        assert task["status"] == "failed"
        assert task["retry_at"]
    finally:
        store.close()


def test_course_without_manifest_commit_is_not_accepted(tmp_path, config, now,
                                                        monkeypatch):
    """模型漏更新目录时，重试可以修复，但不能把草稿算作已生成课程。"""
    _syllabus(tmp_path, [
        {
            "id": "one",
            "title": "第一节",
            "path": "one.md"
        },
        {
            "id": "two",
            "title": "第二节",
            "path": "two.md"
        },
    ])
    monkeypatch.setattr(course, "_today", lambda _: str(now.date()))
    created = []

    def incomplete_manifest(root, _config, _prompt, _label):
        # 模型已经写出正文，却漏掉要求更新的目录日期。
        next_name = "one.md" if not (root /
                                     "courses/one.md").exists() else "two.md"
        (root / "courses" / next_name).write_text("一节完整正文占位，测试生成协议。")
        created.append(next_name)
        return {
            "status": "completed",
            "artifacts": [f"courses/{next_name}"],
            "summary": "完成正文但未提交目录日期",
        }

    monkeypatch.setattr(runner, "codex", incomplete_manifest)
    store, _ = _queue(tmp_path, "course", now)
    try:
        runner.consume(tmp_path, config, store, now)
        later = now + dt.timedelta(hours=1)
        store.add("course", later.isoformat(), "test-config")
        runner.consume(tmp_path, config, store, later)
        tasks = [
            item for item in store.export_tasks() if item["kind"] == "course"
        ]
        assert created, "测试必须实际执行生成路径"
        assert all(item["status"] != "completed" for item in tasks)
        syllabus = json.loads((tmp_path / "courses/syllabus.json").read_text())
        assert not any(
            item.get("status") == "generated" for item in syllabus["lessons"])
    finally:
        store.close()


def test_old_feedback_cannot_acknowledge_changed_note(tmp_path, config, now,
                                                      monkeypatch):
    """旧反馈文件仍存在，不意味着本次新笔记已经得到回应。"""
    _syllabus(tmp_path, [{
        "id": "one",
        "title": "第一节",
        "path": "one.md",
        "generated_on": str(now.date()),
    }])
    (tmp_path / "courses/one.md").write_text("已经生成的课程")
    note = tmp_path / "private/notes/one.md"
    note.parent.mkdir(parents=True)
    note.write_text("我的新问题：为什么这个梯度不能直接求期望？")
    feedback = tmp_path / "private/feedback/one.md"
    feedback.parent.mkdir(parents=True)
    feedback.write_text("这是上一版笔记的旧反馈。")
    monkeypatch.setattr(course, "_today", lambda _: str(now.date()))
    monkeypatch.setattr(
        runner, "codex", lambda *_args: {
            "status": "completed",
            "artifacts": ["private/feedback/one.md"],
            "summary": "声称处理完成但没有更新反馈",
        })
    store, _ = _queue(tmp_path, "course", now)
    try:
        runner.consume(tmp_path, config, store, now)
        state = tmp_path / ".state/course-feedback.json"
        hashes = json.loads(
            state.read_text())["note_hashes"] if state.exists() else {}
        actual = hashlib.sha256(note.read_bytes()).hexdigest()
        assert hashes.get("private/notes/one.md") != actual
    finally:
        store.close()


def test_invalid_model_schema_is_recorded_as_retryable_failure(
        tmp_path, config, now, monkeypatch):
    """模型结构化输出校验异常必须落库，不能遗留 running。"""

    def invalid_output(*_args):
        raise jsonschema.ValidationError("artifacts 字段不符合协议")

    monkeypatch.setattr(runner, "execute", invalid_output)
    store, identifier = _queue(tmp_path, "review", now)
    try:
        runner.consume(tmp_path, config, store, now)
        task = next(
            item for item in store.export_tasks() if item["id"] == identifier)
        assert task["status"] == "failed"
        assert task["retry_at"]
    finally:
        store.close()


def _copy_config(root, config):
    """为 CLI 测试提供正式 Schema 和隔离配置。"""
    project = Path(__file__).resolve().parents[1]
    (root / "config").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(project / "config/schema.json", root / "config/schema.json")
    (root / "config/settings.example.json").write_text(json.dumps(config))


def test_server_does_not_commit_failed_metadata_collection(
        tmp_path, config, now, monkeypatch):
    """服务器采集失败时不能登记采集完成游标。"""
    _copy_config(tmp_path, config)
    remote = sync.sanitized_config(config)
    (tmp_path / "config/server.json").write_text(json.dumps(remote))
    monkeypatch.setattr(cli.scheduler, "now_local",
                        lambda _: now.replace(hour=13))

    def fail_fetch(*_args, **_kwargs):
        raise OSError("模拟服务器网络不可用")

    monkeypatch.setattr(papers, "_fetch", fail_fetch)
    try:
        cli._server(tmp_path, False)  # pylint: disable=protected-access
    except (OSError, RuntimeError):
        pass
    store = Store(tmp_path / ".state/queue.sqlite3")
    try:
        paper_task = next(
            task for task in store.export_tasks() if task["kind"] == "papers")
        assert not store.get_setting(f"collected:{paper_task['id']}")
    finally:
        store.close()


def test_chat_import_only_reopens_affected_review_dates(tmp_path, config, now,
                                                        monkeypatch):
    """导入一天的补充记录不能重新生成所有旧日复盘。"""
    _copy_config(tmp_path, config)
    yesterday = now - dt.timedelta(days=1)
    earlier = now - dt.timedelta(days=2)
    store = Store(tmp_path / ".state/queue.sqlite3")
    older_id = store.add("review", earlier.isoformat(), "test")
    affected_id = store.add("review", yesterday.isoformat(), "test")
    store.finish(older_id, "completed",
                 {"artifacts": ["private/daily/older.md"]})
    store.finish(affected_id, "completed",
                 {"artifacts": ["private/daily/new.md"]})
    store.close()
    snapshot = tmp_path / "import.json"
    snapshot.write_text(
        json.dumps({
            "messages": [{
                "timestamp": yesterday.isoformat(),
                "role": "user",
                "text": "补充昨天已完成的实验记录。",
                "conversation_id": "fixture",
            }]
        }))
    monkeypatch.setattr(cli.scheduler, "now_local", lambda _: now)
    cli._dispatch(  # pylint: disable=protected-access
        argparse.Namespace(root=tmp_path,
                           command="import-chat",
                           source=snapshot))
    store = Store(tmp_path / ".state/queue.sqlite3")
    try:
        tasks = {task["id"]: task for task in store.export_tasks()}
        assert tasks[older_id]["status"] == "completed"
        assert tasks[affected_id]["status"] == "pending"
    finally:
        store.close()


def test_finished_curriculum_still_runs_daily_maintenance(
        tmp_path, config, now, monkeypatch):
    """课程目录全部生成后，日更应转入维护而不是永久空操作。"""
    _syllabus(tmp_path, [{
        "id": "one",
        "title": "第一节",
        "path": "one.md",
        "generated_on": str(now.date() - dt.timedelta(days=1)),
    }])
    (tmp_path / "courses/one.md").write_text("已有课程内容")
    monkeypatch.setattr(course, "_today", lambda _: str(now.date()))
    calls = []

    def maintain(*_args):
        calls.append(True)
        return {"status": "completed", "artifacts": [], "summary": "维护检查完成"}

    monkeypatch.setattr(runner, "codex", maintain)
    store, _ = _queue(tmp_path, "course", now)
    try:
        runner.consume(tmp_path, config, store, now)
        assert calls == [True]
    finally:
        store.close()


def test_fulltext_download_failure_keeps_paper_task_retryable(
        tmp_path, config, now, monkeypatch):
    """元数据成功但所有全文源失败时，不能报告任务已完成。"""
    record = {
        "id": "2601.00001",
        "version": 1,
        "topic_id": "dexterous-manipulation",
        "title": "Fixture",
    }
    state = tmp_path / ".state"
    state.mkdir()
    (state / "paper-candidates.json").write_text(json.dumps([record]))
    (state / "paper-collection-report.json").write_text(
        json.dumps({
            "complete": True,
            "errors": []
        }))
    monkeypatch.setattr(papers, "collect", lambda *_args: [record])

    def fail_prepare(*_args, **_kwargs):
        raise OSError("模拟全文下载失败")

    for name in ("_prepare_html", "_prepare_source", "_prepare_pdf"):
        monkeypatch.setattr(papers, name, fail_prepare)
    store, identifier = _queue(tmp_path, "papers", now)
    try:
        runner.consume(tmp_path, config, store, now)
        task = next(
            item for item in store.export_tasks() if item["id"] == identifier)
        assert task["status"] == "failed"
        assert task["retry_at"]
    finally:
        store.close()


def test_failed_lesson_can_be_repaired_on_the_same_day(tmp_path, config, now,
                                                       monkeypatch):
    """每日限额限制新增章节，但不能阻止修复当天同一节的中断正文。"""
    _syllabus(tmp_path, [
        {
            "id": "one",
            "title": "第一节",
            "path": "one.md",
            "example": "examples/one.py"
        },
        {
            "id": "two",
            "title": "第二节",
            "path": "two.md",
            "example": "examples/two.py"
        },
    ])
    monkeypatch.setattr(course, "_today", lambda _: str(now.date()))
    calls = []

    def generate(root, _config, _prompt, _label):
        calls.append(True)
        lesson = root / "courses/one.md"
        lesson.write_text("完整课程" if len(calls) > 1 else "中断草稿")
        if len(calls) == 1:
            raise RuntimeError("模拟写入正文后 CLI 中断")
        example = root / "courses/examples/one.py"
        example.parent.mkdir(parents=True, exist_ok=True)
        example.write_text('"""课程示例。"""\n')
        syllabus = root / "courses/syllabus.json"
        data = json.loads(syllabus.read_text())
        data["lessons"][0].update({
            "status": "generated",
            "generated_on": str(now.date()),
        })
        syllabus.write_text(json.dumps(data))
        return {
            "status": "completed",
            "artifacts": ["courses/one.md", "courses/examples/one.py"],
            "summary": "同一节已修复完成",
        }

    monkeypatch.setattr(runner, "codex", generate)
    store, identifier = _queue(tmp_path, "course", now)
    try:
        runner.consume(tmp_path, config, store, now)
        runner.consume(tmp_path, config, store, now + dt.timedelta(hours=1))
        task = next(
            item for item in store.export_tasks() if item["id"] == identifier)
        assert len(calls) == 2
        assert task["status"] == "completed"
        assert not (tmp_path / "courses/two.md").exists()
    finally:
        store.close()


def test_remote_metadata_survives_local_lookback_window(tmp_path, config, now,
                                                        monkeypatch):
    """Mac 离线较久后，服务器候选仍须进入解读，不能只依赖近期再检索。"""
    record = {
        "id": "2601.00001",
        "version": 1,
        "topic_id": "dexterous-manipulation",
        "title": "Archived fixture",
        "published": "2026-01-01T00:00:00+00:00",
        "categories": ["cs.RO"],
        "abstract": "dexterous manipulation fixture",
        "reason": "服务器已初筛",
        "url": "https://arxiv.org/abs/2601.00001v1",
    }
    state = tmp_path / ".state"
    state.mkdir()

    def current_scan(root, _config):
        # 当前窗口没有新论文；真实 collect 会合并已存在的本地候选。
        report = root / ".state/paper-collection-report.json"
        report.write_text(json.dumps({"complete": True, "errors": []}))
        candidate_file = root / ".state/paper-candidates.json"
        return json.loads(
            candidate_file.read_text()) if candidate_file.exists() else []

    monkeypatch.setattr(papers, "collect", current_scan)
    monkeypatch.setattr(papers, "_prepare_html", lambda *_args: {
        "source": "html",
        "body": "fixture",
    })
    calls = []

    def write_review(root, _config, _prompt, _label):
        calls.append(True)
        relative = ("papers/dexterous-manipulation/"
                    "2601.00001/versions/v1/README.md")
        (root / relative).write_text("归档候选的验收解读。")
        return {"status": "completed", "artifacts": [relative], "summary": "完成"}

    monkeypatch.setattr(runner, "codex", write_review)
    store = Store(state / "queue.sqlite3")
    store.add("papers", now.isoformat(), "remote-config",
              {"candidates": [record]})
    try:
        runner.consume(tmp_path, config, store, now)
        assert calls == [True], "已同步的旧候选必须进入解读流程"
    finally:
        store.close()
