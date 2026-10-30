"""验证课程进度、用户笔记保护及反馈确认行为。"""

import json
from pathlib import Path

import pytest

from second_proprio import course


@pytest.fixture(name="project")
def project_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """建立包含一节已生成课程和一节计划课程的隔离目录。"""
    course_path = tmp_path / "courses"
    (course_path / "lessons").mkdir(parents=True)
    (course_path / "lessons" / "01-model.md").write_text("第一节",
                                                         encoding="utf-8")
    syllabus = {
        "lessons": [
            {
                "id": "01-model",
                "title": "统一建模",
                "path": "lessons/01-model.md",
                "status": "generated",
                "generated_on": "2026-10-28",
            },
            {
                "id": "02-bc",
                "title": "行为克隆",
                "path": "lessons/02-bc.md",
            },
        ]
    }
    (course_path / "syllabus.json").write_text(json.dumps(syllabus),
                                               encoding="utf-8")
    monkeypatch.setattr(course, "_today", lambda _: "2026-10-29")
    return tmp_path


def test_generated_is_not_learned(project: Path) -> None:
    """生成状态和空白笔记不得被当作用户学完。"""
    result = course.prepare(project, {})
    assert result["progress"][0]["generated"]
    assert not result["progress"][0]["learned"]
    assert not result["progress"][1]["learned"]
    assert not result["note_hashes"]
    assert result["next_lesson"]["id"] == "02-bc"


def test_preserves_notes_and_detects_explicit_completion(project: Path) -> None:
    """已有用户文字逐字保留，并且只认明确的完成勾选。"""
    course.prepare(project, {})
    note = project / "private" / "notes" / "01-model.md"
    content = "# 我的笔记\n- [x] 已学完本节\n策略会影响下一步的数据分布。\n"
    note.write_text(content, encoding="utf-8")
    result = course.prepare(project, {})
    assert note.read_text(encoding="utf-8") == content
    assert result["progress"][0]["learned"]
    assert list(result["note_hashes"]) == ["private/notes/01-model.md"]


def test_feedback_commits_once_and_reopens_after_edit(project: Path) -> None:
    """已确认的相同笔记不重复批改，修改后重新进入反馈队列。"""
    course.prepare(project, {})
    note = project / "private" / "notes" / "01-model.md"
    note.write_text("我认为状态等于相机图像。", encoding="utf-8")
    prepared = course.prepare(project, {})
    hashes = prepared["note_hashes"]
    with pytest.raises(ValueError, match="反馈正文"):
        course.commit_feedback(project, hashes)
    feedback = project / "private" / "feedback" / "01-model.md"
    feedback.parent.mkdir(parents=True)
    note_hash = hashes["private/notes/01-model.md"]
    feedback.write_text(
        f"note_hash: {note_hash}\n图像通常是观测；状态还可能包含不可见的接触信息。",
        encoding="utf-8",
    )
    course.commit_feedback(project, hashes, prepared["feedback_hashes"])
    assert not course.prepare(project, {})["note_hashes"]
    note.write_text("理解了；触觉观测是否足够？", encoding="utf-8")
    assert course.prepare(project, {})["note_hashes"]
    with pytest.raises(ValueError, match="笔记已变化"):
        course.commit_feedback(project, hashes)


def test_old_feedback_cannot_confirm_new_note(project: Path) -> None:
    """非空旧反馈不能被当作对新笔记的处理结果。"""
    course.prepare(project, {})
    note = project / "private" / "notes" / "01-model.md"
    note.write_text("这是新问题。", encoding="utf-8")
    feedback = project / "private" / "feedback" / "01-model.md"
    feedback.parent.mkdir(parents=True)
    feedback.write_text("这是旧答案。", encoding="utf-8")
    prepared = course.prepare(project, {})
    with pytest.raises(ValueError, match="当前笔记摘要"):
        course.commit_feedback(project, prepared["note_hashes"],
                               prepared["feedback_hashes"])
    note_hash = prepared["note_hashes"]["private/notes/01-model.md"]
    feedback.write_text(f"note_hash: {note_hash}\n这是已存在的答案。", encoding="utf-8")
    prepared = course.prepare(project, {})
    with pytest.raises(ValueError, match="反馈尚未更新"):
        course.commit_feedback(project, prepared["note_hashes"],
                               prepared["feedback_hashes"])


def test_same_day_does_not_generate_second_lesson(project: Path) -> None:
    """当天已有实际生成文件时，仅做反馈和维护。"""
    syllabus_path = project / "courses" / "syllabus.json"
    syllabus = json.loads(syllabus_path.read_text(encoding="utf-8"))
    syllabus["lessons"][0]["generated_on"] = "2026-10-29"
    syllabus_path.write_text(json.dumps(syllabus), encoding="utf-8")
    result = course.prepare(project, {})
    assert result["next_lesson"] is None
    assert not result["expected_paths"]


def test_offline_catchup_only_selects_next_lesson(project: Path) -> None:
    """离线多日只返回顺序中的下一节，不制造缺席天数的积压。"""
    result = course.prepare(project, {})
    assert result["expected_paths"] == ["courses/lessons/02-bc.md"]


@pytest.mark.parametrize("status,generated_on", [
    ("planned", "2026-10-29"),
    ("generated", None),
    ("generated", "日期尚未填写"),
])
def test_partial_lesson_is_selected_again(project: Path, status: str,
                                          generated_on: str | None) -> None:
    """正文已写出但完成元数据未验收时，仍选择该节补做。"""
    syllabus_path = project / "courses" / "syllabus.json"
    syllabus = json.loads(syllabus_path.read_text(encoding="utf-8"))
    syllabus["lessons"][0]["status"] = status
    syllabus["lessons"][0]["generated_on"] = generated_on
    syllabus_path.write_text(json.dumps(syllabus), encoding="utf-8")
    result = course.prepare(project, {})
    assert not result["progress"][0]["generated"]
    assert result["next_lesson"]["id"] == "01-model"


def test_rejects_path_escape(project: Path) -> None:
    """配置路径不得通过父目录逃离项目。"""
    with pytest.raises(ValueError, match="项目内"):
        course.prepare(project, {"paths": {"courses": "../other"}})
